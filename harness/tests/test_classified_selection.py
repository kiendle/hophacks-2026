"""Small selection fixtures: all-topic startup shares events and preserves replay semantics."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import classified_data as data


class CountedEvents(list):
    iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


class ClassifiedSelectionTest(unittest.TestCase):
    def setUp(self):
        for patcher in (
            patch.object(data, '_SELECTION_SOURCE', None),
            patch.object(data, '_ALL_SELECTED', None),
            patch.dict(data._SELECTED, {}, clear=True),
            patch.object(data, 'manifest', return_value={'taxonomy': {'categories': [
                {'id': 'alpha', 'label': 'Alpha', 'products': []},
                {'id': 'beta', 'label': 'Beta', 'products': []},
            ]}}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        def event(identifier, kind, post, text, company, t):
            return {'id': identifier, 'kind': kind, 'postId': post, 'text': text, 't': t,
                    'grades': [{'company': company, 'score': 8}] if company else []}

        self.events = CountedEvents([
            event('early-like', 'like', 'one', 'older unrelated content', 'alpha', 0),
            event('publication', 'post', 'one', 'needle', 'beta', 1),
            event('later-like', 'like', 'one', 'edited unrelated content', 'alpha', 2),
            event('standalone-like', 'like', 'two', 'needle', 'alpha', 3),
            event('ungraded-post', 'post', 'three', 'unrelated', None, 4),
        ])
        self.document = {'start': 0, 'end': 5, 'events': self.events,
                         'companies': [{'id': 'beta', 'name': 'Beta'},
                                       {'id': 'alpha', 'name': 'Alpha'},
                                       {'id': 'absent', 'name': 'Absent'}],
                         'counts': {'posts': 2, 'likes': 3}}

    def test_all_aliases_share_events_and_one_metadata_scan(self):
        with patch.object(data, 'matcher', side_effect=AssertionError('All topics need no matching')):
            selected = data.select(self.document, ['AI'])
            for words in (['ai'], [' Artificial Intelligence '], ['needle', 'AI']):
                self.assertIs(data.select(self.document, words), selected)
        events, companies, posts = selected
        self.assertIs(events, self.events)
        self.assertEqual(self.events.iterations, 1)
        self.assertEqual(companies, self.document['companies'][:2])
        self.assertEqual(posts, 2)

    def test_all_metadata_stays_cached_when_filtered_queries_are_evicted(self):
        selected = data.select(self.document, ['AI'])
        for index in range(6):
            data.select(self.document, [f'other-{index}'])
        iterations = self.events.iterations
        self.assertIs(data.select(self.document, ['artificial intelligence']), selected)
        self.assertEqual(self.events.iterations, iterations)
        self.assertLessEqual(len(data._SELECTED), 4)

    def test_all_counts_still_come_from_events_for_legacy_metadata(self):
        self.document['counts'] = {'posts': 999, 'likes': 999}
        with patch.object(data, 'load', return_value=self.document):
            result = data.scan(['AI'], [])
        self.assertEqual(result['dataset']['counts'], {'posts': 2, 'likes': 3})
        self.assertEqual(result['kept'], 2)
        self.assertIs(result['dataset']['events'], self.events)

    def test_filtered_publication_keeps_all_its_activity_and_independent_matches(self):
        selected = data.select(self.document, ['needle'])
        self.assertEqual([event['id'] for event in selected[0]],
                         ['early-like', 'publication', 'later-like', 'standalone-like'])
        self.assertEqual(selected[1], self.document['companies'][:2])
        self.assertEqual(selected[2], 1)
        for actual, expected in zip(selected[0], self.events):
            self.assertIs(actual, expected)
        self.assertIs(data.select(self.document, ['needle']), selected)

    def test_replaced_dataset_drops_all_old_selections(self):
        data.select(self.document, ['AI'])
        data.select(self.document, ['needle'])
        replacement = {**self.document, 'events': []}
        selected = data.select(replacement, ['AI'])
        self.assertEqual(selected, ([], [], 0))
        self.assertIs(selected[0], replacement['events'])
        self.assertEqual(data._SELECTED, {})
        self.assertIs(data._SELECTION_SOURCE[0], replacement)

    def test_appended_event_invalidates_all_and_filtered_metadata(self):
        data.select(self.document, ['AI'])
        data.select(self.document, ['needle'])
        self.events.append({'id': 'added', 'kind': 'post', 'postId': 'four',
                            'text': 'needle', 't': 4, 'grades': []})
        self.assertEqual(data.select(self.document, ['AI'])[2], 3)
        self.assertEqual(data.select(self.document, ['needle'])[2], 2)


if __name__ == '__main__':
    unittest.main()
