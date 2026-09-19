# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2", "scikit-learn", "numpy", "scipy", "pytz"]
# ///
"""Sample-only lexical discovery; candidates require full-corpus confirmation."""
import json
import re
from pathlib import Path
import duckdb
import numpy as np
from sklearn.feature_extraction.text import CountVectorizer, ENGLISH_STOP_WORDS

con=duckdb.connect()
con.execute("SET TimeZone='UTC'; SET threads=3; SET memory_limit='3GB'")
rows=con.execute("""SELECT id, body, cast(created_at AS DATE) AS day FROM read_parquet('twitter-analysis/tweet-id-sample.parquet')
WHERE lang='en' AND created_at IS NOT NULL AND created_at<'2026-09-17'::TIMESTAMPTZ
QUALIFY row_number() OVER(PARTITION BY id ORDER BY version DESC, added_at DESC)=1 ORDER BY id""").fetchall()
clean=[re.sub(r'https?://\S+|@[\w]+|&\w+;|^RT\s+', ' ',r[1]).lower() for r in rows]
stop=sorted(set(ENGLISH_STOP_WORDS)|{'just','like','know','don','amp','really','got','going','want','did','im','ve','ll','people','think','make','time','day','new','today','good','need','say','said','way','does','thing','things','look','let','come','right','love','lol','rt'})
vector=CountVectorizer(stop_words=stop,min_df=25,max_df=.08,max_features=50000,ngram_range=(1,3),binary=True,token_pattern=r'(?u)\b[a-z][a-z0-9_]{2,}\b')
x=vector.fit_transform(clean)
terms=vector.get_feature_names_out()
days=np.array([str(r[2]) for r in rows])
unique_days=sorted(set(days))
daily=np.array([np.asarray(x[days==day].sum(axis=0)).ravel() for day in unique_days])
counts=np.array([(days==day).sum() for day in unique_days])
result=[]
for j,term in enumerate(terms):
    best=None
    for d,day in enumerate(unique_days):
        same_regime=np.array([(s<'2026-09-01')==(day<'2026-09-01') and s!=day for s in unique_days])
        baseline=daily[same_regime,j].sum()
        denominator=counts[same_regime].sum()
        peak=int(daily[d,j])
        if peak<8 or int(daily[:,j].sum())<35:
            continue
        rr=((peak+.5)/(counts[d]+1))/((baseline+.5)/(denominator+1))
        score=np.log2(max(rr,1))*np.log1p(peak)
        if best is None or score>best['score']:
            best={'term':term,'peak_day':day,'peak_sample_tweets':peak,'total_sample_tweets':int(daily[:,j].sum()),'peak_per_10000_english_sample':float(10000*peak/counts[d]),'same_regime_baseline_tweets':int(baseline),'share_ratio':float(rr),'score':float(score)}
    if best:
        result.append(best)
result.sort(key=lambda r:r['score'],reverse=True)
Path('topic-analysis/text-burst-discovery.json').write_text(json.dumps({'method':'English 0.1% ID-sample lexical discovery only; complete days; within Aug/Sep regime; not confirmed full-corpus events. Ngrams formed after removing stopwords, so multiword candidates require evidence inspection.','candidates':result[:300]},indent=2)+'\n')
for r in result[:80]:print(json.dumps(r),flush=True)
