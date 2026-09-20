# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb==1.5.5"]
# ///
"""Parquet queries must agree with replay rules without loading replay JSON."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import classified_data as data


class StoreTest(unittest.TestCase):
    def test_parquet_matches_reference_for_history_filters_and_unknown_balances(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            companies = [{'id': 'openai', 'name': 'OpenAI'}, {'id': 'xai', 'name': 'xAI'}]
            categories = [{'id': c['id'], 'label': c['name'], 'products': ['ChatGPT'] if c['id'] == 'openai' else ['Grok']} for c in companies]
            def event(identifier, post, t, text, score, kind='post', opening=False, delta=0):
                return dict(id=identifier, postId=post, kind=kind, t=t, text=text,
                    postTime={'twitter:1': 0, 'twitter:2': 1500, 'twitter:3': 3000}.get(post),
                    grades=[{'company': c['id'], 'score': score, 'choice': 'positive' if score > 5 else 'negative'} for c in companies],
                    opening=opening, delta=delta)
            events = [event('p1', 'twitter:1', 0, 'special café', 0),
                event('l1', 'twitter:1', 1000, 'edited content', 0, 'like', True, 10000),
                event('p2', 'twitter:2', 1500, 'Claude and ChatGPT', 10),
                event('l2', 'twitter:1', 2000, 'edited content', 0, 'like', True, 9000),
                event('l3', 'twitter:1', 3000, 'edited content', 0, 'like', False, -200),
                event('p3', 'twitter:3', 3000, 'compare unrelated', 8),
                event('l4', 'twitter:2', 3500, 'Claude and ChatGPT', 10, 'like', False, 5),
                event('l5', 'twitter:4', 3600, 'self-contained Grok', 2, 'like', True, 10),
                event('l6', 'twitter:1', 4000, 'edited content', 0, 'like', False, 100000)]
            events.sort(key=lambda e: (e['t'], e['kind'] != 'post', e['id']))
            document = dict(start=0, end=5000, companies=companies, events=events, counts={'posts': 3, 'likes': 6})
            manifest = {'taxonomy': {'categories': categories}, 'window': {'start': data.iso(0), 'end': data.iso(5000)}}
            rows = {'post': [], 'like': []}
            for e in events:
                sentiment = {g['company']: {'choice': g['choice'], 'probabilities': {
                    'positive': g['score'] / 10, 'negative': 1 - g['score'] / 10}} for g in e['grades']}
                rows[e['kind']].append({'event_id': e['id'], 'post_id': e['postId'], 'time': data.iso(e['t']),
                    'content': e['text'], 'post_content': e['text'], 'likes_delta': e['delta'], 'is_opening': e['opening'],
                    'classification': {'companies': [c['id'] for c in companies]}, 'sentiment': sentiment})
            columns = {'event_id': 'VARCHAR', 'post_id': 'VARCHAR', 'time': 'TIMESTAMPTZ', 'content': 'VARCHAR',
                'post_content': 'VARCHAR', 'likes_delta': 'BIGINT', 'is_opening': 'BOOLEAN',
                'classification': 'STRUCT(companies VARCHAR[])',
                'sentiment': 'MAP(VARCHAR, STRUCT(choice VARCHAR, probabilities MAP(VARCHAR, DOUBLE)))'}
            connection = duckdb.connect()
            for kind in rows:
                source = package / f'{kind}.json'
                source.write_text(json.dumps(rows[kind]), encoding='utf-8')
                connection.read_json(str(source), columns=columns).write_parquet(str(package / f'{kind}-events.parquet'))
            connection.close()
            cases = [(['AI'], 0, 4000, ['openai']), (['AI'], 2000, 4000, ['openai', 'xai']),
                     (['AI'], 3000, 4000, None), (['special'], 1000, 4000, ['openai']),
                     (['café'], 0, 4000, ['openai']), (['Grok'], 0, 4000, None),
                     (['unrelated'], 0, 5000, None), (['nonexistent'], 0, 4000, None)]
            with patch.object(data, 'PACKAGE', package), patch.object(data, 'manifest', return_value=manifest):
                for words, start, end, selected in cases:
                    args = words, data.iso(start), data.iso(end), selected
                    with patch.object(data, 'has_query_store', return_value=False), patch.object(data, 'load', return_value=document):
                        expected = data.sentiment(*args)
                        expected_posts = data.query_posts(*args, sort='influence', limit=12)
                    with patch.object(data, 'load', side_effect=AssertionError('Never load replay JSON for a query')):
                        self.assertEqual(data.sentiment(*args), expected, args)
                        actual = data.query_posts(*args, sort='influence', limit=12)
                    # Equal-weight ties can differ in company ordering; compare the complete evidence by ID.
                    self.assertEqual(actual['total'], expected_posts['total'], args)
                    self.assertEqual(sorted(actual['examples'], key=lambda e: e['id']),
                                     sorted(expected_posts['examples'], key=lambda e: e['id']), args)


if __name__ == '__main__':
    unittest.main()
