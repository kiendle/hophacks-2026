"""Query scope, UTC boundaries and evidence from the same activity that drives replay."""
import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import classified_data as data


def grade(company, score):
    return {'company': company, 'score': score, 'choice': 'negative' if score < 5 else 'positive'}


def event(identifier, post, t, score, kind='post', **fields):
    return dict(id=identifier, postId='twitter:' + post, t=t, postTime=0 if post == '1' else t,
                kind=kind, grades=[grade('openai', score), grade('xai', 9)],
                text='OpenAI ChatGPT', **fields)


class QueryTest(unittest.TestCase):
    def setUp(self):
        store = patch.object(data, 'has_query_store', return_value=False)
        store.start()
        self.addCleanup(store.stop)
        self.events = [
            event('p1', '1', 0, 0),
            event('opening', '1', 1000, 0, 'like', opening=True, delta=10000),
            event('repeat', '1', 2000, 0, 'like', opening=True, delta=10000),
            event('change', '1', 3000, 0, 'like', opening=False, delta=100),
            event('p2', '2', 3500, 10),
            event('unknown-opening', '2', 3600, 10, 'like', opening=False, delta=5),
            event('future', '1', 4000, 0, 'like', opening=False, delta=1000000),
        ]
        self.events[4]['text'] = 'Claude and ChatGPT together'
        self.events[5]['text'] = self.events[4]['text']
        self.fixture = dict(start=0, end=5000, events=self.events,
                            companies=[{'id': 'openai', 'name': 'OpenAI'}, {'id': 'xai', 'name': 'xAI'}],
                            counts={'posts': 2, 'likes': 5})
        taxonomy = {'taxonomy': {'categories': [
            {'id': 'openai', 'label': 'OpenAI', 'products': ['ChatGPT']},
            {'id': 'xai', 'label': 'xAI', 'products': ['Grok']}]}}
        for name, value in [('load', self.fixture), ('manifest', taxonomy)]:
            patcher = patch.object(data, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.low, self.high = '1970-01-01T00:00:03Z', '1970-01-01T00:00:04Z'

    def test_company_selection_does_not_leak_co_mentions(self):
        self.assertEqual([c['id'] for c in data.sentiment(['ChatGPT'])['companies']], ['openai'])
        self.assertEqual([c['id'] for c in data.sentiment(['AI'], company_ids=['xai'])['companies']], ['xai'])
        with self.assertRaises(ValueError):
            data.sentiment(['AI'], company_ids=['unknown'])

    def test_exact_activity_window_matches_weighting_and_no_future(self):
        result = data.sentiment(['AI'], self.low, self.high, ['openai'])['companies'][0]
        expected = 10 * (1 + math.log(6)) / (math.log(101) + 1 + math.log(6))
        self.assertAlmostEqual(result['sentiment'], expected, places=4)
        self.assertEqual((result['posts'], result['active_posts'], result['scored_posts']), (1, 2, 2))
        query = data.query_posts(['AI'], self.low, self.high, ['openai'], sort='negative')
        post = query['examples'][0]
        self.assertEqual(post['id'], 'twitter:1')
        self.assertEqual(post['like_count'], 10100)
        self.assertEqual(post['period_likes'], 100)
        self.assertFalse(post['published_in_window'])
        self.assertEqual(post['url'], 'https://x.com/i/status/1')
        self.assertEqual(post['classifications'], [{'company': 'openai', 'score': 0, 'choice': 'negative'}])

    def test_matching_all_does_not_expand_company_aliases(self):
        posts = data.query_posts(['AI'], self.low, self.high, ['openai'], ['Claude', 'ChatGPT'], 'all')
        self.assertEqual([p['id'] for p in posts['examples']], ['twitter:2'])
        self.assertEqual(posts['total'], 1)
        self.assertIsNone(posts['examples'][0]['like_count'])
        self.assertEqual(data.query_posts(['AI'], text_terms=['Grok'], match='all')['total'], 0)

    def test_dataset_filter_keeps_linked_like_activity(self):
        self.events[0]['text'] = 'special searchword'
        result = data.sentiment(['searchword'], self.low, self.high, ['openai'])['companies'][0]
        self.assertEqual(result['sentiment'], 0)
        self.assertEqual(result['active_posts'], 1)
        self.assertEqual(result['posts'], 0)

    def test_repeated_opening_is_not_a_new_driver(self):
        posts = data.query_posts(['AI'], '1970-01-01T00:00:02Z', self.low)
        self.assertEqual(posts['total'], 0)

    def test_dates_are_utc_and_preview_handles_subday_intervals(self):
        self.assertEqual(data.timestamp('1970-01-01'), 0)
        self.assertEqual(data.timestamp('1970-01-01T00:00:03'), 3000)
        preview = data.preview(['AI'], self.low, self.high)
        self.assertEqual(preview['total'], 1)
        self.assertIsNone(preview['examples'][0]['like_count'])
        self.assertEqual(preview['examples'][0]['url'], 'https://x.com/i/status/2')
        with self.assertRaises(ValueError):
            data.preview(['AI'], self.high, self.low)


if __name__ == '__main__':
    unittest.main()
