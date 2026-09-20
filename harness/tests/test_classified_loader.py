"""Small gzip fixtures for legacy and framed classified data; no live dataset."""
import gzip
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import classified_data as data


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def dataset(events):
    return {
        'version': 1,
        'start': 0,
        'end': 4000,
        'companies': [{'id': 'openai', 'name': 'OpenAI'}],
        'counts': {
            'posts': sum(event['kind'] == 'post' for event in events),
            'likes': sum(event['kind'] == 'like' for event in events),
        },
        'events': events,
    }


def header(document):
    metadata = {key: value for key, value in document.items() if key != 'events'}
    return compact(metadata)[:-1] + ',"events":[\n'


def framed(document):
    rows = ',\n'.join(compact(event) for event in document['events'])
    return header(document) + (rows + '\n' if rows else '') + ']}\n'


class ClassifiedLoaderTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'demo.json.gz'
        patcher = patch.object(data, 'DATA', self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        data._load.cache_clear()
        self.addCleanup(data._load.cache_clear)
        self.post = {
            'id': 'post:1', 'postId': 'twitter:1', 'kind': 'post', 't': 1000,
            'text': 'Café 日本語 🌍\nsecond line\r\ntab\tquote " and footer ]}',
            'grades': [{'company': 'openai', 'score': 8.5, 'choice': 'positive'}],
        }
        self.like = {
            'id': 'like:1', 'postId': 'twitter:1', 'kind': 'like', 't': 2000,
            'opening': True, 'delta': 7, 'text': self.post['text'],
            'grades': self.post['grades'],
        }
        self.document = dataset([self.post, self.like])

    def write(self, content):
        with gzip.open(self.path, 'wt', encoding='utf-8', newline='\n') as stream:
            stream.write(content)

    def assert_invalid(self, content):
        data._load.cache_clear()
        self.write(content)
        with self.assertRaises(ValueError):
            data.load()

    def test_framed_round_trip_preserves_unicode_escaped_newlines_and_order(self):
        content = framed(self.document)
        self.assertEqual(len(content.splitlines()), 4)
        self.assertIn('\\n', content)
        self.write(content)
        self.assertEqual(data.load(), self.document)

    def test_framed_loader_reads_lines_without_materializing_entire_json(self):
        class LineOnlyStream(io.StringIO):
            def read(self, *args, **kwargs):
                raise AssertionError('Framed loading must use lines, not read()')

        content = framed(self.document)
        self.write(content)
        with patch.object(data.gzip, 'open', return_value=LineOnlyStream(content)):
            self.assertEqual(data.load(), self.document)

    def test_legacy_compact_and_pretty_json(self):
        for indent in (None, 2):
            with self.subTest(indent=indent):
                data._load.cache_clear()
                self.write(json.dumps(self.document, ensure_ascii=False, indent=indent))
                self.assertEqual(data.load(), self.document)

    def test_empty_events_in_framed_and_legacy_formats(self):
        empty = dataset([])
        for content in (framed(empty), compact(empty), json.dumps(empty, indent=2)):
            with self.subTest(content=content):
                data._load.cache_clear()
                self.write(content)
                self.assertEqual(data.load(), empty)

    def test_framed_single_event_needs_no_comma(self):
        document = dataset([self.post])
        self.write(framed(document))
        self.assertEqual(data.load(), document)

    def test_whitespace_after_footer_is_allowed(self):
        self.write(framed(self.document) + ' \t\r\n\n')
        self.assertEqual(data.load(), self.document)

    def test_missing_or_incomplete_footer_is_rejected(self):
        one = dataset([self.post])
        cases = {
            'empty-after-metadata': header(dataset([])),
            'after-last-event': header(one) + compact(self.post) + '\n',
            'after-event-comma': header(self.document) + compact(self.post) + ',\n',
            'missing-object-close': header(one) + compact(self.post) + '\n]\n',
            'missing-array-close': header(one) + compact(self.post) + '\n}\n',
            'partial-event': header(one) + '{"kind":"post"',
        }
        for name, content in cases.items():
            with self.subTest(name=name):
                self.assert_invalid(content)

    def test_invalid_json_event_is_rejected(self):
        for event in ('{"kind":"post",}', '{"kind":post}', '{"kind":"post"'):
            with self.subTest(event=event):
                self.assert_invalid(header(dataset([])) + event + '\n]}\n')

    def test_non_object_event_is_rejected(self):
        for event in (None, [], ['post'], 'post', 1, True):
            with self.subTest(event=event):
                self.assert_invalid(header(dataset([])) + compact(event) + '\n]}\n')

    def test_missing_or_unsupported_event_kind_is_rejected(self):
        for event in ({}, {'kind': 'share'}, {'kind': None}, {'kind': 1}):
            with self.subTest(event=event):
                self.assert_invalid(header(dataset([])) + compact(event) + '\n]}\n')

    def test_malformed_comma_separators_are_rejected(self):
        post, like = compact(self.post), compact(self.like)
        bodies = {
            'missing-comma': post + '\n' + like + '\n',
            'double-comma': post + ',,\n' + like + '\n',
            'leading-comma': ',' + post + ',\n' + like + '\n',
            'trailing-comma': post + ',\n' + like + ',\n',
            'comma-with-no-events': ',\n',
        }
        for name, body in bodies.items():
            with self.subTest(name=name):
                self.assert_invalid(header(self.document) + body + ']}\n')

    def test_non_whitespace_after_footer_is_rejected(self):
        for suffix in ('garbage', '{}\n', compact(self.post), ']}\n', ' \nnull\n'):
            with self.subTest(suffix=suffix):
                self.assert_invalid(framed(self.document) + suffix)

    def test_post_and_like_count_mismatches_are_rejected(self):
        for counts in (
            {'posts': 0, 'likes': 1},
            {'posts': 2, 'likes': 1},
            {'posts': 1, 'likes': 0},
            {'posts': 1, 'likes': 2},
            {'posts': 2, 'likes': 0},  # The total alone is insufficient.
        ):
            with self.subTest(counts=counts):
                document = {**self.document, 'counts': counts}
                self.assert_invalid(framed(document))

    def test_empty_events_still_validate_counts(self):
        for counts in ({'posts': 1, 'likes': 0}, {'posts': 0, 'likes': 1}):
            with self.subTest(counts=counts):
                self.assert_invalid(framed({**dataset([]), 'counts': counts}))

    def test_invalid_count_metadata_is_rejected(self):
        for counts in (
            None, [], {}, {'posts': 1}, {'likes': 1},
            {'posts': True, 'likes': 1}, {'posts': 1, 'likes': True},
            {'posts': 1.0, 'likes': 1}, {'posts': 1, 'likes': '1'},
            {'posts': -1, 'likes': 1}, {'posts': 1, 'likes': -1},
        ):
            with self.subTest(counts=counts):
                self.assert_invalid(framed({**self.document, 'counts': counts}))

    def test_non_json_numeric_constants_are_rejected(self):
        for constant in ('NaN', 'Infinity', '-Infinity'):
            with self.subTest(constant=constant):
                event = '{"kind":"post","t":' + constant + '}'
                self.assert_invalid(header(dataset([self.post])) + event + '\n]}\n')

    def test_repeated_load_reuses_cached_object_without_reopening_gzip(self):
        self.write(framed(self.document))
        with patch.object(data.gzip, 'open', wraps=gzip.open) as open_gzip:
            first = data.load()
            second = data.load()
        self.assertIs(second, first)
        self.assertEqual(open_gzip.call_count, 1)

    def test_changed_mtime_invalidates_cache(self):
        self.write(framed(self.document))
        first = data.load()
        stamp = self.path.stat().st_mtime_ns
        replacement = dataset([self.like])
        self.write(framed(replacement))
        os.utime(self.path, ns=(stamp + 2_000_000_000, stamp + 2_000_000_000))
        self.assertNotEqual(self.path.stat().st_mtime_ns, stamp)
        second = data.load()
        self.assertEqual(second, replacement)
        self.assertIsNot(second, first)
        self.assertIs(data.load(), second)


if __name__ == '__main__':
    unittest.main()
