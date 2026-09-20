"""Offline adapter checks: scoring, honest counts, input validation and HTTP wiring."""
import asyncio
import math
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp.test_utils import TestClient, TestServer
import ui_server as ui
import ui_archive
import classified_data


def test_pack():
    found = dict(scanned=1000, matched=80, covered_fraction=.5, notes=[])
    post = dict(uri='at://did:plc:test/app.bsky.feed.post/1', created_iso='2026-09-19T12:01:00Z',
                handle='test.bsky.social', text='Good news', like_count=2, reply_count=1, repost_count=3)
    answer = dict(subtopic=dict(choice='topic-0'), feeling=dict(score=4), relevant=dict(noul=.9))
    result = ui.pack(found, [post, post, post], [answer, None, {**answer, 'subtopic': {'choice': 'other'}}], ['AI'], ['timeout'])
    bucket = result['series'][0]['buckets'][0]
    assert bucket['sentiment'] == 10 and math.isclose(bucket['weight'], 1 + math.log(3))
    assert math.isclose(ui.sentiment_score({'feeling': {'choice': 'positive', 'probabilities': {'positive': .7, 'negative': .1}}}), 8)
    assert ui.sentiment_score({'feeling': {'choice': 'insufficient_evidence'}}) is None
    assert bucket['traction'] == 9 and bucket['volume'] == 1
    assert result['kept'] == 1 and result['read'] == 1000
    assert bucket['snapshots'][0]['t'] == result['now'] > bucket['topPost']['time']
    assert '50%' in result['note'] and 'sample' in result['note'] and 'timeout' in result['note']
    for value in (None, [], [''], [1], ['a'] * 13):
        try:
            ui.terms(value, 'terms')
        except ValueError:
            pass
        else:
            raise AssertionError(f'Accepted invalid terms: {value}')


async def test_http():
    original = classified_data.scan
    calls = []
    def fake_archive(words, names):
        calls.append(words)
        time.sleep(.05)
        return {'series': [], 'read': 7, 'kept': 0, 'now': None, 'streaming': False}
    classified_data.scan = fake_archive
    try:
        async with TestClient(TestServer(ui.compose())) as client:
            response = await client.post('/api/ui/scan', json={'terms': ['AI'], 'subtopics': ['OpenAI']})
            assert response.status == 200, await response.text()
            assert (await response.json())['read'] == 7
            calls.clear()
            requests = [client.post('/api/ui/scan', json={'terms': ['AI'], 'subtopics': ['OpenAI']}) for _ in range(3)]
            results = await asyncio.gather(*requests)
            assert all(r.status == 200 for r in results)
            assert len(calls) == 1, 'Identical concurrent scans must share a job'
            response = await client.post('/api/ui/scan', json={'terms': ['AI'], 'subtopics': ['OpenAI'], 'source': 'unknown'})
            assert response.status == 400
            response = await client.post('/api/ui/scan', json={'terms': [], 'subtopics': ['OpenAI']})
            assert response.status == 400
            response = await client.post('/api/ui/scan', json={'terms': ['AI'], 'subtopics': ['OpenAI']}, headers={'Origin': 'https://example.com'})
            assert response.status == 403
            response = await client.get('/')
            assert response.status == 200 and 'assets/' in await response.text()
            policy = response.headers['Content-Security-Policy']
            assert "style-src-attr 'unsafe-inline'" in policy or "style-src 'self' 'unsafe-inline'" in policy
    finally:
        classified_data.scan = original


if __name__ == '__main__':
    test_pack()
    asyncio.run(test_http())
    print('UI adapter checks passed')
