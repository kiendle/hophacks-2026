"""Large exports must not become a single oversized WebSocket message."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from replay_api import MAX_BATCH_EVENTS, event_batches


class ReplayBatchTests(unittest.TestCase):
    def test_large_completion_delivers_all_events_before_completion(self):
        events = [{'id': str(i), 't': i // 3} for i in range(3 * MAX_BATCH_EVENTS + 7)]
        now = events[-1]['t'] + 100
        batches = list(event_batches(events, now, 'complete'))
        self.assertEqual([e for batch in batches for e in batch['events']], events)
        self.assertTrue(all(len(batch['events']) <= MAX_BATCH_EVENTS for batch in batches))
        times = [batch['now'] for batch in batches]
        self.assertEqual(times, sorted(times))
        self.assertEqual(times[-1], now)
        self.assertTrue(all(e['t'] <= batch['now'] for batch in batches for e in batch['events']))
        self.assertTrue(all(batch['status'] == 'playing' for batch in batches[:-1]))
        self.assertEqual(batches[-1]['status'], 'complete')

    def test_empty_flush_still_reports_clock_and_controls(self):
        for status in ('playing', 'paused', 'complete'):
            self.assertEqual(list(event_batches([], 123, status)),
                             [{'events': [], 'now': 123, 'status': status}])

    def test_exact_limit_emits_completion_once(self):
        events = [{'id': str(i), 't': 1} for i in range(MAX_BATCH_EVENTS)]
        self.assertEqual(list(event_batches(events, 2, 'complete')),
                         [{'events': events, 'now': 2, 'status': 'complete'}])


if __name__ == '__main__':
    unittest.main()
