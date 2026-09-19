# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2"]
# ///
"""Check delivered analysis invariants without rescanning tweet bodies."""
import json
from pathlib import Path
import duckdb

out = Path('topic-analysis')
c = duckdb.connect()
c.execute("SET threads=2; SET memory_limit='2GB'")
checks = {}
def check(name, actual, expected):
    checks[name] = {'actual': actual, 'expected': expected, 'passed': actual == expected}
    assert actual == expected, (name, actual, expected)
def scalar(sql):
    return c.execute(sql).fetchone()[0]
for name, filename in [('inputs','sentiment-input.parquet'),('scores','final-sentiment.parquet'),('members','sentiment-membership.parquet')]:
    c.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{out / filename}')")
n = scalar('SELECT count(*) FROM inputs')
check('input_ids_unique', scalar('SELECT count(DISTINCT id) FROM inputs'), n)
check('score_rows_complete', scalar('SELECT count(*) FROM scores'), n)
check('score_ids_unique', scalar('SELECT count(DISTINCT id) FROM scores'), n)
check('input_ids_missing_scores', scalar('SELECT count(*) FROM inputs ANTI JOIN scores USING(id)'), 0)
check('members_missing_inputs', scalar('SELECT count(*) FROM members ANTI JOIN inputs USING(id)'), 0)
check('invalid_probabilities', scalar('''SELECT count(*) FROM scores WHERE negative IS NULL OR neutral IS NULL OR positive IS NULL OR sentiment_score IS NULL
 OR NOT isfinite(negative+neutral+positive+sentiment_score)
 OR least(negative,neutral,positive)<0 OR greatest(negative,neutral,positive)>1
 OR abs(negative+neutral+positive-1)>0.00001 OR abs(sentiment_score-(positive-negative))>0.00001'''), 0)
check('retweets_in_sentiment', scalar('SELECT count(*) FROM members WHERE is_rt'), 0)
check('duplicate_author_strata', scalar('SELECT count(*) FROM (SELECT topic,period,author_id FROM members GROUP BY ALL HAVING count(*)>1)'), 0)
check('duplicate_template_strata', scalar('SELECT count(*) FROM (SELECT topic,period,template FROM members GROUP BY ALL HAVING count(*)>1)'), 0)
check('exceeded_sample_caps', scalar("SELECT count(*) FROM (SELECT topic,period,count(*) n FROM members GROUP BY ALL HAVING n>CASE WHEN period='peak' THEN 200 ELSE 300 END)"), 0)
check('partial_day_in_sentiment', scalar("SELECT count(*) FROM members WHERE day=DATE '2026-09-17'"), 0)
spikes = json.loads((out/'topic-spikes.json').read_text())
shifts = json.loads((out/'sentiment-shifts.json').read_text())
chosen = {r['topic']:r for r in spikes['sentiment_candidates']}
check('sampled_topic_coverage', scalar('SELECT count(DISTINCT topic) FROM members'), len(chosen))
check('analyzed_topic_coverage', len(shifts['topics']), len(chosen))
check('ranked_topic_count', len(spikes['topics']), spikes['candidate_topics'])
for topic, period, days in c.execute('SELECT topic,period,list(DISTINCT cast(day AS VARCHAR)) FROM members GROUP BY topic,period').fetchall():
    allowed = [chosen[topic]['peak_day']] if period == 'peak' else chosen[topic]['baseline_days']
    assert set(days) <= set(allowed), (topic,period,days)
checks['sample_periods_match_rankings'] = {'passed': True}
c.execute("CREATE VIEW denominators AS SELECT * FROM read_csv_auto('topic-analysis/daily-denominators.csv')")
inv = json.loads((out/'topic-inventory.json').read_text())
check('daily_counts_reconcile_latest', scalar("SELECT sum(tweets) FROM denominators WHERE lang='<ALL>'") + inv['latest_quality_nulls']['created_at_null'], inv['latest_output']['rows'])
check('language_counts_reconcile_daily', scalar("SELECT count(*) FROM (SELECT day,sum(CASE WHEN lang='<ALL>' THEN tweets ELSE -tweets END) balance FROM denominators GROUP BY day HAVING balance!=0)"), 0)
checks['runtime'] = {'duckdb': duckdb.__version__, 'input_posts': n, 'topic_memberships': scalar('SELECT count(*) FROM members')}
(out/'verification.json').write_text(json.dumps(checks,indent=2)+'\n')
print(json.dumps(checks,indent=2))
