# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2", "pytz"]
# ///
"""Confirm explicit lexical topic families against latest-state unique tweets.
Run only once the full latest-state corpus is complete. Topic families overlap;
counts are exact matches to declared terms, not exhaustive semantic recall.
"""
import argparse
import json
import re
import time
from pathlib import Path
import duckdb

p=argparse.ArgumentParser()
p.add_argument('--source', default='topic-analysis/latest/*.parquet')
p.add_argument('--prefix', default='entity')
p.add_argument('--sample-observations', action='store_true')
a=p.parse_args()
out=Path('topic-analysis')
def quote(s):return "'"+str(s).replace("'","''")+"'"
config=json.loads((out/'entity-topics.json').read_text())
term_map={term.lower():t['topic'] for t in config for term in t['terms']}
terms=sorted(term_map,key=len,reverse=True)
patterns=[r'\b'+re.escape(term)+r'\b' if term.isascii() else re.escape(term) for term in terms]
pattern='(?:'+'|'.join(patterns)+')'
mapping='MAP(['+','.join(map(quote,terms))+'],['+','.join(quote(term_map[t]) for t in terms)+'])'
c=duckdb.connect()
c.execute("SET TimeZone='UTC'; SET threads=4; SET memory_limit='6GB'; SET max_temp_directory_size='60GB'")
c.execute(f"SET temp_directory={quote(out/('.tmp-'+a.prefix))}")
source=f"SELECT * FROM read_parquet({quote(a.source)})"
if a.sample_observations:
 source="SELECT *, starts_with(body,'RT @') AS is_rt FROM ("+source+") QUALIFY row_number() OVER(PARTITION BY id ORDER BY version DESC,added_at DESC)=1"
c.execute('CREATE VIEW latest AS '+source)
start=time.monotonic()
match_path=out/(a.prefix+'-matches.parquet')
c.execute(f"""COPY (
 SELECT unnest(list_distinct(list_transform(regexp_extract_all(lower(body), {quote(pattern)}),
                term -> map_extract_value({mapping}, term)))) AS topic,
        id,author_id,body,cast(created_at AS DATE) AS day,created_at,version,lang,
        is_rt,like_count,reply_count,retweet_count,quote_count,views_count
 FROM latest WHERE created_at IS NOT NULL
) TO {quote(match_path)} (FORMAT PARQUET, COMPRESSION ZSTD)""")
print(f'Matched corpus in {time.monotonic()-start:.1f}s',flush=True)
c.execute(f'CREATE VIEW matches AS SELECT * FROM read_parquet({quote(match_path)})')
c.execute(f"""COPY (
 SELECT topic,day,CASE WHEN grouping(lang)=1 THEN '<ALL>' ELSE coalesce(lang,'<NULL>') END AS lang,
 count(*) AS tweets,count(DISTINCT author_id) AS authors,
 count(*) FILTER(WHERE NOT is_rt) AS originals,
 sum(like_count) FILTER(WHERE NOT is_rt) AS likes,
 sum(views_count) FILTER(WHERE NOT is_rt) AS views,
 sum(retweet_count) FILTER(WHERE NOT is_rt) AS retweets,
 count(like_count) FILTER(WHERE NOT is_rt) AS measured_likes,
 count(views_count) FILTER(WHERE NOT is_rt) AS measured_views,
 avg(like_count) FILTER(WHERE NOT is_rt) AS mean_likes,
 approx_quantile(like_count,[.5,.9]) FILTER(WHERE NOT is_rt) AS likes_p50_p90,
 max(like_count) FILTER(WHERE NOT is_rt) AS max_likes
 FROM matches GROUP BY GROUPING SETS ((topic,day),(topic,day,lang))
) TO {quote(out/(a.prefix+'-daily.parquet'))} (FORMAT PARQUET, COMPRESSION ZSTD)""")
cur=c.execute('SELECT topic,count(*) tweets,count(DISTINCT id) distinct_ids,count(DISTINCT author_id) authors,count(*) FILTER(WHERE NOT is_rt) originals FROM matches GROUP BY topic ORDER BY tweets DESC')
rows=[dict(zip([col[0] for col in cur.description],row)) for row in cur.fetchall()]
result={'method':'Exact lexical matches against latest-state unique tweets; topic families overlap; whole-word ASCII aliases and substring non-ASCII aliases; missing creation dates excluded.', 'source':a.source,'sample_only':a.sample_observations,'topics':rows,'elapsed_seconds':time.monotonic()-start}
(out/(a.prefix+'-inventory.json')).write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2),flush=True)
