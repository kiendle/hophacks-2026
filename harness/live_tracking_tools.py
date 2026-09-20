"""Read the exact persisted observations that feed a live automation chart."""
import json
from contextlib import closing
import math
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

import steps

DATABASE = Path(__file__).resolve().parent / 'state/live-automations/live.sqlite'


def get_live_tracking_data(reason: str, automation_id: str, date_from: str = '', date_to: str = '',
                           target_ids: list[str] | None = None, limit: int = 10) -> dict:
    """Read a live CLI automation, its exact schema, pending counts and chart evidence.

    Use automation_id from chart context. This reads only that tracker's saved live observations,
    never the Twitter archive or a new Bluesky preview. Dates are UTC observation times, with
    an exclusive end. Missing dates mean all captured history. Likes are observed net changes,
    not all-time totals. Does not start, resume, spend or change the automation.
    """
    if not reason.strip():
        return {'error': {'message': 'Explain why you are reading this tracker.'}}
    try:
        if not DATABASE.exists():
            raise ValueError('No live observations have been recorded yet.')
        if not 1 <= limit <= 30:
            raise ValueError('Choose 1 to 30 evidence posts.')
        with closing(sqlite3.connect(DATABASE.as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            auto = conn.execute('SELECT * FROM automations WHERE automation_id=?', (automation_id,)).fetchone()
            if auto is None:
                raise ValueError('Unknown live automation ID. Use the ID in the chart context.')
            config = json.loads(auto['config_json'])
            known = {target['id'] for target in config['targets']}
            targets = set(target_ids or known)
            if targets - known:
                raise ValueError('Use target IDs from this automation configuration.')
            def stamp(value):
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
            low, high = stamp(date_from) if date_from else -math.inf, stamp(date_to) if date_to else math.inf
            if low >= high:
                raise ValueError('Start must precede the exclusive end.')
            counts = dict(conn.execute('SELECT status,COUNT(*) FROM automation_work WHERE automation_id=? GROUP BY status', (automation_id,)))
            has_feed = conn.execute("SELECT 1 FROM sqlite_master WHERE name='visualization_events'").fetchone()
            rows = conn.execute('''SELECT e.*,v.text,v.published_at,w.sentiment_json
              FROM visualization_events e JOIN post_versions v ON v.post_uri=e.post_uri AND v.content_version=e.content_version
              JOIN automation_work w ON w.automation_id=e.automation_id AND w.content_version=e.content_version
              WHERE e.automation_id=? ORDER BY e.event_time,e.cursor''', (automation_id,)) if has_feed else []
            grouped = {}
            for row in rows:
                if not low <= stamp(row['event_time']) < high:
                    continue
                for target, grade in json.loads(row['sentiment_json']).items():
                    if target not in targets:
                        continue
                    key = (target, row['post_uri'])
                    post = grouped.setdefault(key, dict(target=target, id=row['post_uri'], likes=0, published=False))
                    p = grade['probabilities']
                    post.update(text=row['text'], time=row['event_time'], publication_time=row['published_at'],
                        sentiment=None if grade['choice'] == 'insufficient_evidence' else 5 * (1 + p['positive'] - p['negative']))
                    post['published'] |= bool(row['publication'])
                    post['likes'] += row['delta'] or 0
            summary = {target: dict(posts=0, observed_like_changes=0, weight=0, weighted_sum=0) for target in sorted(targets)}
            evidence = []
            for post in grouped.values():
                weight = int(post['published']) + math.log1p(max(0, post['likes']))
                post['weight'] = weight
                pieces = post['id'].split('/')
                post['url'] = f'https://bsky.app/profile/{pieces[2]}/post/{pieces[-1]}'
                row = summary[post['target']]
                row['posts'] += int(post['published'])
                row['observed_like_changes'] += post['likes']
                if post['sentiment'] is not None:
                    row['weight'] += weight
                    row['weighted_sum'] += weight * post['sentiment']
                evidence.append(post)
            for row in summary.values():
                row['sentiment'] = row.pop('weighted_sum') / row['weight'] if row['weight'] else None
            examples = sorted(evidence, key=lambda p: p['weight'], reverse=True)[:limit]
            unique = {post['id']: post for post in examples}
            card = dict(kind='preview', title='Recorded live observations',
                        total=len({post['id'] for post in evidence}),
                        note=f"Completed observations in this time window. {counts.get('pending', 0)} texts still await classification across this tracker. Likes are observed changes, not lifetime totals.",
                        examples=[dict(id=post['id'], day=post['time'], likes=None,
                                       body=post['text'][:240], fullText=post['text'], url=post['url']) for post in unique.values()])
            return dict(source='live_automation', automation_id=automation_id, configuration=config,
                        counts=counts, enabled=bool(auto['enabled']), worker_state=auto['worker_state'],
                        last_error=auto['last_error'], date_from=date_from, date_to=date_to,
                        targets=summary, examples=examples, _card=card,
                        note='Only completed recorded observations. Pending classifications are not evidence of neutral sentiment; likes are changes observed by this tracker.')
    except (ValueError, KeyError, sqlite3.Error) as error:
        return {'error': {'message': str(error)}}


def register(mcp):
    mcp.tool()(get_live_tracking_data)
    mcp.tool()(get_historical_data_contract)


def get_historical_data_contract(reason: str) -> dict:
    """Read the original schema, taxonomy and scoring policy of the active AI-company archive.

    This is the actual saved database contract, not the v2 template for a new live automation.
    Historical data exists only for AI-company sentiment. Never rescore it or claim arbitrary
    topics are covered. For other topics help build a new live automation.
    """
    import classified_data
    saved = classified_data.manifest()
    return {key: saved[key] for key in ('schema_version', 'configuration_key', 'policy', 'taxonomy', 'schemas', 'semantics', 'window', 'counts')}


steps.register_tool('get_live_tracking_data', lambda fields: 'Reading this live tracker',
                    lambda result, is_error: 'Live observations read.' if not is_error else '')
steps.register_tool('get_historical_data_contract', lambda fields: 'Reading the historical configuration',
                    lambda result, is_error: 'Original archive schema read.' if not is_error else '')
