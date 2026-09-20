"""Read the prepared, deduplicated Twitter archive sample for the React charts."""
import asyncio
import hashlib
import json
import threading
from pathlib import Path

import duckdb
import jev_tools as jev

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / 'harness/data/prepared/sample.parquet'
CACHE = ROOT / 'harness/state/cache/ui-archive'


def scan_archive(words, names):
    import demo_mcp_server as archive
    import ui_server
    if not SAMPLE.exists():
        raise ValueError('The prepared Twitter dataset is missing. Run harness/prepare_data.py first.')
    key = hashlib.sha256(json.dumps([words, names, SAMPLE.stat().st_mtime_ns, 'archive-v3']).encode()).hexdigest()
    cached = CACHE / f'{key}.json'
    if cached.exists():
        return json.loads(cached.read_text(encoding='utf-8'))
    parameters = {'file': str(SAMPLE)}
    parameters.update({f'k{i}': archive.search_pattern(word) for i, word in enumerate(words)})
    parameters.update({f't{i}': archive.search_pattern(name) for i, name in enumerate(names)})
    topic_pattern = ' OR '.join(f'regexp_matches(plain, $t{i})' for i in range(len(names)))
    pattern = ' OR '.join(f'regexp_matches(plain, $k{i})' for i in range(len(words)))
    connection = duckdb.connect()
    timer = threading.Timer(45, connection.interrupt)
    try:
        connection.execute("SET TimeZone='UTC'")
        connection.execute("SET memory_limit='2GB'")
        connection.execute('SET threads=2')
        timer.start()
        count, start, end = connection.execute('SELECT count(*), min(created_at)::VARCHAR, max(created_at)::VARCHAR FROM read_parquet(?)', [str(SAMPLE)]).fetchone()
        cursor = connection.execute(f"""
            WITH posts AS (
                SELECT *, {archive.LINKLESS} AS plain FROM read_parquet($file)
                WHERE NOT starts_with(body, 'RT @')
                AND coalesce(reply_to_status_id, '') = '' AND coalesce(quoting_id, '') = ''
            ), matched AS (SELECT * FROM posts WHERE {pattern})
            SELECT id, body, created_at, like_count, retweet_count, author_id, count(*) OVER () AS matched_count
            FROM matched
            ORDER BY CASE WHEN ({topic_pattern}) THEN 0 ELSE 1 END, row_number() OVER (PARTITION BY CAST(created_at AS DATE) ORDER BY hash(id)), hash(id)
            LIMIT 150
        """, parameters)
        rows = cursor.fetchall()
    finally:
        timer.cancel()
        connection.close()
    posts = [dict(uri=str(row[0]), text=row[1], full_text=row[1], created_iso=row[2].isoformat(), like_count=row[3] or 0,
                  repost_count=row[4] or 0, reply_count=0, handle=str(row[5] or ''), url=archive.post_url(row[0])) for row in rows]
    if posts and not jev.os.environ.get('TYPESAFE_API_KEY'):
        raise ValueError('The Twitter data is available, but Jev scoring needs TYPESAFE_API_KEY configured.')
    answers, reasons, _ = asyncio.run(jev.score_texts([post['text'] for post in posts], ui_server.questions_for(words, names)))
    found = dict(scanned=count, matched=rows[0][6] if rows else 0, covered_fraction=1, notes=[])
    result = ui_server.pack(found, posts, answers, names, reasons)
    result.update(source='twitter_archive', now=None, streaming=False, read=count, archive_range=[str(start), str(end)])
    for series in result['series']:
        for bucket in series['buckets']:
            # Saved engagement totals are a static archive measurement, not a time-series of likes.
            bucket['snapshots'] = []
    result['note'] = (f"X/Twitter archive · {str(start)[:10]} to {str(end)[:10]} · {count:,} posts in the saved 1% sample · "
                      f"{found['matched']:,} keyword matches in that sample · {result['kept']} scored posts shown for your subtopics. "
                      'Engagement uses saved totals, not historical changes. No live collection.')
    if reasons:
        result['note'] += f" {len(reasons)} posts could not be scored."
    if not posts:
        result['note'] += ' No matching posts in the saved sample; try broader search terms.'
    if not posts or result['kept']:
        CACHE.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(result), encoding='utf-8')
    return result
