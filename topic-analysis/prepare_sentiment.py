# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2", "pyarrow>=20", "pytz"]
# ///
"""Extract full-corpus evidence and deterministic author/template-capped samples."""
import json
from pathlib import Path
import duckdb
import pyarrow as pa

out=Path('topic-analysis')
c=duckdb.connect()
c.execute("SET TimeZone='UTC'; SET threads=4; SET memory_limit='6GB'; SET max_temp_directory_size='60GB'")
c.execute("SET temp_directory='topic-analysis/.tmp-sampling'")
def quote(s):return "'"+str(s).replace("'","''")+"'"
spikes=json.loads((out/'topic-spikes.json').read_text())
selected=spikes['sentiment_candidates']
c.register('chosen_input',pa.Table.from_pylist([{'topic':r['topic'],'peak_day':r['peak_day'],'baseline_days':r['baseline_days']} for r in selected]))
c.execute('CREATE TABLE chosen AS SELECT * FROM chosen_input')
tags=[r['topic'] for r in selected if r['topic'].startswith('#')]
if tags:
 taglist='['+','.join(map(quote,tags))+']'
 c.execute(f"""COPY (
 SELECT unnest(list_intersect(list_distinct(regexp_extract_all(lower(body),'#[\\p{{L}}\\p{{M}}\\p{{N}}_]+')), {taglist})) AS topic,
 id,author_id,body,cast(created_at AS DATE) AS day,created_at,version,lang,is_rt,
 like_count,reply_count,retweet_count,quote_count,views_count
 FROM read_parquet('topic-analysis/latest/*.parquet')
 WHERE NOT is_rt AND created_at IS NOT NULL AND trim(body)!=''
 ) TO 'topic-analysis/selected-hashtag-matches.parquet' (FORMAT PARQUET, COMPRESSION ZSTD)""")
 source="SELECT * FROM read_parquet(['topic-analysis/entity-matches.parquet','topic-analysis/selected-hashtag-matches.parquet'])"
else:
 source="SELECT * FROM read_parquet('topic-analysis/entity-matches.parquet')"
c.execute('CREATE VIEW matches AS '+source)
c.execute("""CREATE TABLE eligible AS
 SELECT m.*, CASE WHEN cast(m.day AS VARCHAR)=ch.peak_day THEN 'peak' ELSE 'baseline' END AS period,
 trim(regexp_replace(regexp_replace(lower(m.body),'https?://\\S+|@[A-Za-z0-9_]+',' ','g'),'\\s+',' ','g')) AS template
 FROM matches m JOIN chosen ch USING(topic)
 WHERE NOT is_rt AND trim(body)!='' AND
 (cast(m.day AS VARCHAR)=ch.peak_day OR list_contains(ch.baseline_days,cast(m.day AS VARCHAR)))
""")
c.execute("""COPY (
 WITH author_capped AS (
   SELECT * FROM eligible WHERE template!='' QUALIFY row_number() OVER(PARTITION BY topic,period,author_id ORDER BY hash(id))=1
 ), template_capped AS (
   SELECT * FROM author_capped QUALIFY row_number() OVER(PARTITION BY topic,period,template ORDER BY hash(author_id))=1
 ) SELECT * FROM template_capped
 QUALIFY row_number() OVER(PARTITION BY topic,period ORDER BY hash(author_id),id)<=CASE WHEN period='peak' THEN 200 ELSE 300 END
) TO 'topic-analysis/sentiment-membership.parquet' (FORMAT PARQUET, COMPRESSION ZSTD)""")
c.execute("""COPY (
 SELECT id,any_value(body) AS body,any_value(lang) AS lang
 FROM read_parquet('topic-analysis/sentiment-membership.parquet') GROUP BY id
) TO 'topic-analysis/sentiment-input.parquet' (FORMAT PARQUET, COMPRESSION ZSTD)""")
c.execute("""COPY (
 SELECT * FROM eligible WHERE period='peak'
 QUALIFY row_number() OVER(PARTITION BY topic ORDER BY like_count DESC NULLS LAST,views_count DESC NULLS LAST,id)<=8
) TO 'topic-analysis/topic-evidence.parquet' (FORMAT PARQUET, COMPRESSION ZSTD)""")
cur=c.execute("SELECT topic,period,count(*) sampled_authors,count(DISTINCT body) unique_bodies,string_agg(DISTINCT coalesce(lang,'<NULL>'),',') languages FROM read_parquet('topic-analysis/sentiment-membership.parquet') GROUP BY topic,period ORDER BY topic,period")
keys=[col[0] for col in cur.description]
summary=[dict(zip(keys,row)) for row in cur.fetchall()]
meta={'method':'Up to one hash-selected non-RT post per author per topic/period; then cap templates after lowercasing, removing URLs/mentions and collapsing whitespace to one author; select up to 200 peak and 300 baseline authors by deterministic hash(author_id). Exclude empty normalized text. Not tweet-prevalence weighted. Baseline is same-regime other complete days. One unique ID scored once globally.',
      'unique_inference_posts':c.execute("SELECT count(*) FROM read_parquet('topic-analysis/sentiment-input.parquet')").fetchone()[0],
      'strata':summary}
(out/'sentiment-sampling.json').write_text(json.dumps(meta,indent=2,ensure_ascii=False)+'\n')
print(json.dumps(meta,indent=2,ensure_ascii=False),flush=True)
