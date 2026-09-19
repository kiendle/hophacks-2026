# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2", "numpy", "scipy", "pytz"]
# ///
"""Author-cluster uncertainty and language-adjusted topic polarity comparisons."""
import csv
import json
import math
from pathlib import Path
import duckdb
import numpy as np
from scipy.stats import norm, false_discovery_control

out=Path('topic-analysis')
supported={'ar','de','en','es','fr','hi','it','pt'}
c=duckdb.connect()
c.execute("SET TimeZone='UTC'; SET threads=3; SET memory_limit='3GB'")
cur=c.execute("""SELECT m.*,s.negative,s.neutral,s.positive,s.sentiment_score,s.predicted_label
FROM read_parquet('topic-analysis/sentiment-membership.parquet') m
JOIN read_parquet('topic-analysis/final-sentiment.parquet') s USING(id)""")
cols=[r[0] for r in cur.description]
rows=[dict(zip(cols,row)) for row in cur.fetchall()]
by_topic={}
for row in rows:by_topic.setdefault(row['topic'],[]).append(row)
spikes=json.loads((out/'topic-spikes.json').read_text())
selected={r['topic']:r for r in spikes['sentiment_candidates']}
rng=np.random.default_rng(20260919)

def summary(data):
 a=np.array([r['sentiment_score'] for r in data if r['sentiment_score'] is not None],dtype=float)
 if not len(a):return {'n':0,'mean':None,'negative_fraction':None,'positive_fraction':None}
 return {'n':len(a),'authors':len(set(r['author_id'] for r in data)), 'mean':float(a.mean()),
         'negative_fraction':float(np.mean([r['predicted_label']=='negative' for r in data])),
         'positive_fraction':float(np.mean([r['predicted_label']=='positive' for r in data])),
         'mean_max_probability':float(np.mean([max(r['negative'],r['neutral'],r['positive']) for r in data])),
         'languages':{lang:sum((r['lang'] or '<NULL>')==lang for r in data) for lang in sorted(set(r['lang'] or '<NULL>' for r in data))}}

def bootstrap_difference(peak,base):
 authors=sorted({r['author_id'] for r in peak+base})
 if len(peak)<10 or len(base)<10:return None
 index={a:i for i,a in enumerate(authors)}
 p=np.zeros(len(authors));b=np.zeros(len(authors));pn=np.zeros(len(authors));bn=np.zeros(len(authors))
 for r in peak:p[index[r['author_id']]]+=r['sentiment_score'];pn[index[r['author_id']]]+=1
 for r in base:b[index[r['author_id']]]+=r['sentiment_score'];bn[index[r['author_id']]]+=1
 draws=rng.multinomial(len(authors),np.full(len(authors),1/len(authors)),size=1000)
 diff=(draws@p)/(draws@pn)-(draws@b)/(draws@bn)
 return np.quantile(diff,[.025,.975]).tolist()

results=[]
for topic in sorted(by_topic):
 group=by_topic[topic]
 group=[r for r in group if r['sentiment_score'] is not None]
 peak_all=[r for r in group if r['period']=='peak']
 base_all=[r for r in group if r['period']=='baseline']
 peak=[r for r in peak_all if r['lang'] in supported]
 base=[r for r in base_all if r['lang'] in supported]
 ps,bs=summary(peak),summary(base)
 row={k:v for k,v in selected[topic].items() if k not in ['series','baseline_days','sentiment_selected']}
 row.update({'peak_sentiment':ps,'baseline_sentiment':bs,'all_language_peak':summary(peak_all),
             'all_language_baseline':summary(base_all),'unsupported_peak_posts':len(peak_all)-len(peak),
             'unsupported_baseline_posts':len(base_all)-len(base), 'mean_shift':None,'shift_ci95':None,
             'language_adjusted_shift':None,'language_adjusted_p':None,'language_adjusted_q':None,
             'language_adjusted_strata':[], 'strong_sentiment_shift':False})
 if peak and base:
  row['mean_shift']=ps['mean']-bs['mean']
  row['negative_fraction_shift']=ps['negative_fraction']-bs['negative_fraction']
  row['shift_ci95']=bootstrap_difference(peak,base)
  strata=[]
  for lang in sorted(supported):
   p=np.array([r['sentiment_score'] for r in peak if r['lang']==lang])
   b=np.array([r['sentiment_score'] for r in base if r['lang']==lang])
   if len(p)>=10 and len(b)>=10:
    strata.append({'lang':lang,'peak_n':len(p),'baseline_n':len(b),'shift':float(p.mean()-b.mean()),
                   'variance':float(p.var(ddof=1)/len(p)+b.var(ddof=1)/len(b)), 'weight_raw':min(len(p),len(b))})
  if strata:
   total_weight=sum(s['weight_raw'] for s in strata)
   difference=sum(s['shift']*s['weight_raw']/total_weight for s in strata)
   variance=sum(s['variance']*(s['weight_raw']/total_weight)**2 for s in strata)
   se=math.sqrt(variance)
   row['language_adjusted_shift']=difference
   row['language_adjusted_ci95']=[difference-1.96*se,difference+1.96*se]
   row['language_adjusted_p']=float(2*norm.sf(abs(difference)/se)) if se>0 else (0.0 if difference else 1.0)
   row['language_adjusted_strata']=strata
   row['language_adjusted_peak_n']=sum(s['peak_n'] for s in strata)
   row['language_adjusted_baseline_n']=sum(s['baseline_n'] for s in strata)
 results.append(row)
valid=[r for r in results if r['language_adjusted_p'] is not None]
for row,q in zip(valid,false_discovery_control([r['language_adjusted_p'] for r in valid],method='bh')):
 row['language_adjusted_q']=float(q)
 ci=row['shift_ci95']
 row['strong_sentiment_shift']=bool(
   row['peak_sentiment']['n']>=80 and row['baseline_sentiment']['n']>=80 and
   row.get('language_adjusted_peak_n',0)>=80 and row.get('language_adjusted_baseline_n',0)>=80 and
   abs(row['mean_shift'])>=.15 and ci and (ci[0]>0 or ci[1]<0) and q<.05 and
   row['language_adjusted_shift']*row['mean_shift']>0)
for r in results:
 r['combined_outlier_score']=math.log2(max(r['share_lift'],1))*abs(r['language_adjusted_shift'] or 0)*math.log1p(r['peak_tweets'])
results.sort(key=lambda r:(r['strong_sentiment_shift'],r['combined_outlier_score']),reverse=True)
metadata={'polarity':'Whole-post positive-minus-negative model probability, [-1,+1]. Not sentiment directed at named topic and not verified public opinion.',
 'primary_languages':sorted(supported),'unsupported_languages':'Scored exploratorily and reported separately; excluded from primary shift tests.',
 'sampling':'Author- and normalized-template-capped non-RT posts, at most200 peak/300 baseline. Template normalization lowercases, removes URLs/mentions, collapses whitespace.',
 'confidence':'1000 author-cluster bootstrap draws (same author across periods shares resampling weight), conditional on sample/design/model; not model-calibration uncertainty.',
 'language_adjustment':'Common trained-language strata with >=10 observations per period; weight proportional to min(peak_n,baseline_n); normal mean-difference SE; BH FDR across all valid selected-topic tests.',
 'strong_shift_rule':'Both supported and common-language samples >=80 per period, abs(mean shift)>=0.15, bootstrap95% CI excludes0, adjusted BH q<0.05, raw/adjusted shift signs agree.',
 'selection':'Topics selected by volume/share bursts, not sentiment. Boundaries and collection selection bias remain.',
 'tested_topics':len(valid),'selected_topics':len(results),'strong_shifts':sum(r['strong_sentiment_shift'] for r in results)}
(out/'sentiment-shifts.json').write_text(json.dumps({'method':metadata,'topics':results},indent=2,ensure_ascii=False)+'\n')
fields=['topic','label','peak_day','peak_tweets','share_lift','peak_n','baseline_n','peak_mean','baseline_mean','mean_shift','language_adjusted_shift','language_adjusted_q','strong_sentiment_shift','unsupported_peak_posts','unsupported_baseline_posts']
with (out/'sentiment-shifts.csv').open('w',newline='') as f:
 w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader()
 for r in results:
  w.writerow({**r,'peak_n':r['peak_sentiment']['n'],'baseline_n':r['baseline_sentiment']['n'],'peak_mean':r['peak_sentiment']['mean'],'baseline_mean':r['baseline_sentiment']['mean']})
print(json.dumps(metadata,indent=2),flush=True)
for r in results[:25]:
 print(json.dumps({k:r.get(k) for k in ['label','peak_day','peak_tweets','share_lift','mean_shift','language_adjusted_shift','language_adjusted_q','strong_sentiment_shift']},ensure_ascii=False),flush=True)
