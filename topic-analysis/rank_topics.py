# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2", "numpy", "pytz"]
# ///
"""Rank full-corpus topic shares within Aug/Sep collection regimes.
This is descriptive retrospective outlier discovery, not causal event detection.
"""
import csv
import json
import math
import argparse
from pathlib import Path
import duckdb
import numpy as np

out=Path('topic-analysis')
parser=argparse.ArgumentParser()
parser.add_argument('--entities-only',action='store_true',help='Interim entity ranking while hashtag discovery runs; rerun without this flag for final outputs.')
args=parser.parse_args()
c=duckdb.connect()
c.execute("SET TimeZone='UTC'; SET threads=3; SET memory_limit='4GB'")
def records(sql):
 cur=c.execute(sql)
 keys=[x[0] for x in cur.description]
 return [dict(zip(keys,r)) for r in cur.fetchall()]
denoms=records("SELECT cast(day AS VARCHAR) AS day, tweets FROM read_csv_auto('topic-analysis/daily-denominators.csv') WHERE lang='<ALL>' AND day IS NOT NULL ORDER BY day")
N={r['day']:r['tweets'] for r in denoms}
days=sorted(N)
complete=[d for d in days if d<'2026-09-17']
regimes={d:[b for b in complete if (b<'2026-09-01')==(d<'2026-09-01') and b!=d] for d in complete}
language_denoms={(r['day'],r['lang']):r['tweets'] for r in records("SELECT cast(day AS VARCHAR) AS day,coalesce(lang,'<NULL>') AS lang,tweets FROM read_csv_auto('topic-analysis/daily-denominators.csv') WHERE lang!='<ALL>' AND day IS NOT NULL")}
entity_language={}
for r in records("SELECT topic,cast(day AS VARCHAR) AS day,lang,tweets FROM read_parquet('topic-analysis/entity-daily.parquet') WHERE lang!='<ALL>'"):
 entity_language.setdefault(r['topic'],{}).setdefault(r['lang'],{})[r['day']]=r['tweets']
rows=records("SELECT topic,cast(day AS VARCHAR) AS day,tweets,authors,originals,likes,views,retweets FROM read_parquet('topic-analysis/entity-daily.parquet') WHERE lang='<ALL>'")
if not args.entities_only:
 rows+=records("""WITH eligible AS (
 SELECT topic FROM read_parquet('topic-analysis/hashtag-daily.parquet') GROUP BY topic HAVING sum(tweets)>=1000
 ) SELECT h.topic,cast(h.day AS VARCHAR) AS day,h.tweets,h.authors,h.originals,h.likes,h.views,h.retweets
 FROM read_parquet('topic-analysis/hashtag-daily.parquet') h JOIN eligible USING(topic) WHERE h.day IS NOT NULL""")
series={}
for r in rows:series.setdefault(r['topic'],{})[r['day']]=r
config={t['topic']:t for t in json.loads((out/'entity-topics.json').read_text())}
ranked=[]
for topic,points in series.items():
 if topic.startswith('#') and (len(topic)<2 or not any(ch.isalpha() for ch in topic)):
  continue
 total=sum(p['tweets'] for p in points.values())
 best=None
 for day in complete:
  p=points.get(day)
  if not p or p['tweets']<200 or p['authors']<100:
   continue
  baseline_days=regimes[day]
  bn=sum(N[d] for d in baseline_days)
  bc=sum(points.get(d,{}).get('tweets',0) for d in baseline_days)
  br=(bc+.5)/(bn+1)
  rate=p['tweets']/N[day]
  ratio=((p['tweets']+.5)/(N[day]+1))/br
  if ratio<=1:continue
  score=math.log2(ratio)*math.log1p(p['tweets'])
  row={'topic':topic,'label':config.get(topic,{}).get('label',topic),'family':config.get(topic,{}).get('family','Hashtag / unclassified'),
       'total_tweets':int(total),'peak_day':day,'peak_tweets':int(p['tweets']),'peak_authors':int(p['authors']),
       'peak_originals':int(p['originals']),'peak_rt_fraction':1-p['originals']/p['tweets'],
       'peak_likes':int(p['likes'] or 0),'peak_views':int(p['views'] or 0),'peak_retweets':int(p['retweets'] or 0),
       'peak_per_100k':rate*100000,'baseline_per_100k':bc/bn*100000,'share_lift':ratio,'spike_score':score,
       'baseline_tweets':int(bc),'baseline_days':baseline_days,'low_baseline':bc<50,
       'window_boundary_peak':day in ['2026-08-17','2026-08-31','2026-09-01','2026-09-16']}
  if best is None or score>best['spike_score']:best=row
 if best:
  best['series']=[{'day':day,'tweets':int(points.get(day,{}).get('tweets',0)),
                   'originals':int(points.get(day,{}).get('originals',0)),
                   'per_100k':points.get(day,{}).get('tweets',0)/N[day]*100000,
                   'partial_day':day=='2026-09-17'} for day in days]
  best['language_adjusted_share_lift']=None
  if topic in entity_language:
   expected=0.
   for lang,language_points in entity_language[topic].items():
    denominator=sum(language_denoms.get((d,lang),0) for d in best['baseline_days'])
    numerator=sum(language_points.get(d,0) for d in best['baseline_days'])
    expected+=(numerator+.5)/(denominator+1)*language_denoms.get((best['peak_day'],lang),0)
   best['language_adjusted_share_lift']=(best['peak_tweets']+.5)/(expected+.5)
  ranked.append(best)
ranked.sort(key=lambda r:(not r['low_baseline'],r['spike_score']),reverse=True)
entities=[r for r in ranked if r['topic'].startswith('entity:')]
tags=[r for r in ranked if r['topic'].startswith('#')]
selected=list(entities)
selected+= [r for r in tags if r['peak_originals']>=80 and not r['low_baseline']][:15]
for r in selected:r['sentiment_selected']=True
result={'method':{'denominator':'All latest-state unique tweets created that UTC day.',
 'baseline':'Other complete days in the same Aug17-Aug31 or Sep1-Sep16 collection regime; retrospective, not strictly preceding.',
 'partial_day':'Sep17 shown but excluded from peak detection and baselines.',
 'support':'Topic total >=1000 for hashtags; peak >=200 tweets and >=100 authors; numeric-only hashtags excluded; short multilingual hashtags retained.',
 'lift':'Ratio of daily shares with Jeffreys-style +0.5 numerator smoothing; not statistical significance.',
 'score':'Topics with >=50 baseline posts rank before sparse-baseline/new topics; within each group log2(share_lift)*log1p(peak_tweets). Heuristic only; large ratios with near-zero baselines are not reliable comparative lifts.',
 'language_adjustment':'Entity topics additionally standardize baseline topic rates by language to the peak day corpus language mix. Hashtag lift is not language adjusted.',
 'limits':'Normalization cannot eliminate unknown collection-selection bias. Topic families overlap. Hashtag topics not automatically merged.'},
 'candidate_topics':len(ranked),'topics':ranked,'sentiment_candidates':selected}
(out/'topic-spikes.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
fields=[k for k in ranked[0] if k not in ['series','baseline_days','sentiment_selected']]
with (out/'topic-spikes.csv').open('w',newline='') as f:
 w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(ranked)
print(json.dumps({'candidate_topics':len(ranked),'sentiment_candidates':len(selected),'top_entities':[{k:r[k] for k in ['label','peak_day','peak_tweets','share_lift','peak_originals']} for r in entities[:20]],'top_hashtags':[{k:r[k] for k in ['label','peak_day','peak_tweets','share_lift','peak_originals']} for r in tags[:20]]},ensure_ascii=False,indent=2),flush=True)
