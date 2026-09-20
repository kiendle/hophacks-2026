"""Archive-only scoring and cache regression test; no network or paid scoring."""
import asyncio
import math
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import duckdb
import ui_archive
import ui_server


def main():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        sample = root / 'sample.parquet'
        connection = duckdb.connect()
        connection.execute('''CREATE TABLE posts(id VARCHAR, author_id VARCHAR, body VARCHAR, created_at TIMESTAMPTZ,
            lang VARCHAR, like_count INTEGER, retweet_count INTEGER, reply_to_status_id VARCHAR, quoting_id VARCHAR)''')
        for index, text in enumerate(['OpenAI AI launch', 'AI from Anthropic', 'said nothing', 'RT @user AI launch']):
            connection.execute('INSERT INTO posts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                               [str(index), 'author', text, '2026-09-09T12:00:00Z', 'en', 3, 2, '', ''])
        connection.execute('COPY posts TO ? (FORMAT PARQUET)', [str(sample)])
        connection.close()
        calls = []
        async def fake_score(texts, questions):
            calls.append(texts)
            return [dict(relevant=dict(noul=.95), feeling=dict(choice='positive', probabilities=dict(positive=.6, negative=.1, neutral=.3)), subtopic=dict(choice='topic-0')) for text in texts], [], 0
        with patch.object(ui_archive, 'SAMPLE', sample), patch.object(ui_archive, 'CACHE', root / 'cache'), \
             patch.object(ui_archive.jev, 'score_texts', fake_score), patch.dict(os.environ, {'TYPESAFE_API_KEY': 'test'}), \
             patch.object(ui_server, 'scan', side_effect=AssertionError('Archive requests must never call Bluesky')):
            result = ui_archive.scan_archive(['AI'], ['OpenAI'])
            assert result['source'] == 'twitter_archive' and result['streaming'] is False and result['now'] is None
            assert result['read'] == 4 and result['kept'] == 2
            bucket = result['series'][0]['buckets'][0]
            assert bucket['volume'] == 2 and math.isclose(bucket['sentiment'], 7.5) and bucket['snapshots'] == []
            assert '1% sample' in result['note'] and 'No live collection' in result['note']
            assert ui_archive.scan_archive(['AI'], ['OpenAI']) == result
            assert len(calls) == 1, 'Cached requests must not pay for scoring again'
    print('Archive source, keyword boundaries, retweet exclusion, sentiment scaling and cache checks passed')


if __name__ == '__main__':
    main()
