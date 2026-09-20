"""Durable completion feed. Its cursor tracks deliveries, never source sequence.

Late classifications and newly resolved likes are inserted once, including rows older
than the latest-results API window. Observation timestamps remain unchanged.
"""
import json
from datetime import datetime, timedelta, timezone


def ingestion(store, now=None):
    """Durable receipts before keyword filtering, measured by local arrival time.

    Source timestamps may be old during replay. Likes, edits and deletes aren't
    new posts, and a reconnect cannot count an already persisted event twice.
    """
    now = now or datetime.now(timezone.utc)
    stamp = lambda value: value.isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    end, start = stamp(now), stamp(now - timedelta(minutes=5))
    conn = store._ensure_open()
    posts = conn.execute('''SELECT COUNT(DISTINCT p.post_uri)
      FROM source_events s CROSS JOIN post_mutations p ON p.source_seq=s.seq
      WHERE s.received_at>? AND s.received_at<=? AND p.operation='create' AND p.deleted=0''',
      (start, end)).fetchone()[0]
    events = conn.execute('SELECT COUNT(*) FROM source_events WHERE received_at>? AND received_at<=?',
                          (start, end)).fetchone()[0]
    last_post = conn.execute('''SELECT s.received_at FROM source_events s
      WHERE s.received_at<=? AND EXISTS(SELECT 1 FROM post_mutations p
        WHERE p.source_seq=s.seq AND p.operation='create' AND p.deleted=0)
      ORDER BY s.received_at DESC LIMIT 1''', (end,)).fetchone()
    last_event = conn.execute('SELECT MAX(received_at) FROM source_events WHERE received_at<=?', (end,)).fetchone()[0]
    return dict(window_seconds=300, posts_last_5m=posts, events_last_5m=events,
                last_post_at=last_post[0] if last_post else None, last_event_at=last_event, as_of=end)


def milliseconds(value):
    return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000) if value else None


def project(store, identifier):
    auto = store.get_automation(identifier)
    conn = store._ensure_open()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS visualization_events (
          cursor INTEGER PRIMARY KEY AUTOINCREMENT, automation_id TEXT NOT NULL,
          event_key TEXT NOT NULL, kind TEXT NOT NULL, post_uri TEXT NOT NULL,
          content_version TEXT NOT NULL, event_time TEXT NOT NULL, delta INTEGER,
          publication INTEGER NOT NULL DEFAULT 0,
          UNIQUE(automation_id,event_key));
        CREATE INDEX IF NOT EXISTS visualization_cursor ON visualization_events(automation_id,cursor);
    ''')
    # Use mutation IDs, rather than text hashes: editing back to an earlier text is a
    # distinct observation. A content edit never becomes another publication.
    conn.execute('''INSERT OR IGNORE INTO visualization_events
      (automation_id,event_key,kind,post_uri,content_version,event_time,publication)
      SELECT m.automation_id,'post:' || p.mutation_id,'post',p.post_uri,p.content_version,
             COALESCE(p.event_time,p.observed_at),p.operation='create'
      FROM post_mutations p JOIN automation_matches m
        ON m.post_uri=p.post_uri AND m.content_version=p.content_version
      JOIN automation_work w ON w.automation_id=m.automation_id AND w.content_version=m.content_version
      WHERE m.automation_id=? AND w.status='ready' AND p.deleted=0
        AND p.source_seq>? AND p.source_seq<=?
        AND NOT EXISTS(SELECT 1 FROM visualization_events e WHERE e.automation_id=m.automation_id
                       AND e.event_key='post:' || p.mutation_id)
      ORDER BY p.source_seq,p.mutation_id''', (identifier, auto['start_cursor'], auto['routing_cursor']))
    conn.execute('''INSERT OR IGNORE INTO visualization_events
      (automation_id,event_key,kind,post_uri,content_version,event_time,delta)
      SELECT m.automation_id,'like:' || d.delta_id,'like',d.subject_uri,d.content_version,
             COALESCE(d.event_time,d.observed_at),d.delta
      FROM like_deltas d JOIN automation_matches m
        ON m.post_uri=d.subject_uri AND m.content_version=d.content_version
      JOIN automation_work w ON w.automation_id=m.automation_id AND w.content_version=m.content_version
      WHERE m.automation_id=? AND w.status='ready' AND d.resolved=1
        AND d.source_seq>? AND d.source_seq<=?
        AND NOT EXISTS(SELECT 1 FROM visualization_events e WHERE e.automation_id=m.automation_id
                       AND e.event_key='like:' || d.delta_id)
      ORDER BY d.source_seq,d.delta_id''', (identifier, auto['start_cursor'], auto['routing_cursor']))
    return auto


def changes(store, identifier, after=0, limit=1000):
    if after < 0:
        raise ValueError('after must be nonnegative')
    auto = project(store, identifier)
    from budgeting import usage
    auto['budget_usage'] = usage(store.path.parent / 'inference.sqlite', identifier)
    conn = store._ensure_open()
    rows = conn.execute('''SELECT e.*,v.text,v.published_at,v.observed_at,w.sentiment_json
      FROM visualization_events e
      JOIN post_versions v ON v.post_uri=e.post_uri AND v.content_version=e.content_version
      JOIN automation_work w ON w.automation_id=e.automation_id AND w.content_version=e.content_version
      WHERE e.automation_id=? AND e.cursor>? ORDER BY e.cursor LIMIT ?''',
      (identifier, after, limit)).fetchall()
    events = []
    for row in rows:
        grades = []
        for target, answer in json.loads(row['sentiment_json']).items():
            p = answer['probabilities']
            score = None if answer['choice'] == 'insufficient_evidence' else 5 * (1 + p['positive'] - p['negative'])
            grades.append(dict(company=target, choice=answer['choice'], confidence=answer['confidence'],
                               probabilities=p, score=score))
        events.append(dict(id=row['event_key'], kind=row['kind'], t=milliseconds(row['event_time']),
                           postId=row['post_uri'], postTime=milliseconds(row['published_at']),
                           text=row['text'], authorId=row['post_uri'].split('/')[2],
                           contentVersion=row['content_version'], observedAt=milliseconds(row['observed_at']),
                           grades=grades, delta=row['delta'], opening=False, publication=bool(row['publication'])))
    counts = dict(conn.execute('SELECT status,COUNT(*) FROM automation_work WHERE automation_id=? GROUP BY status', (identifier,)).fetchall())
    source = dict(store.source_status(), ingestion=ingestion(store))
    return dict(automation={key: value for key, value in auto.items() if key != 'compiled'},
                source=source, counts=counts, events=events,
                cursor=rows[-1]['cursor'] if rows else after, more=len(rows) == limit)
