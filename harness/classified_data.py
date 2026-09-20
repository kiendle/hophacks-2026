"""Read the active Jev-classified export without model calls."""
import functools
import gzip
import json
import math
import re
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'web/data/demo.json.gz'
PACKAGE = ROOT / 'harness/data/classified/processed-streams-20260920T063350Z'
_LOAD_LOCK = threading.Lock()
_SELECT_LOCK = threading.Lock()
_SELECTION_SOURCE = None  # dataset and event count shared by the selection caches
_ALL_SELECTED = None
_SELECTED = {}  # words -> selection, oldest first


@functools.lru_cache(maxsize=1)
def _load(stamp):
    with gzip.open(DATA, 'rt', encoding='utf-8') as stream:
        first_line = stream.readline()
        if not first_line.rstrip().endswith('"events":['):
            # Old exports are ordinary JSON, sometimes pretty-printed. Release
            # their first line before reading again so it is not held twice.
            del first_line
            stream.seek(0)
            return json.load(stream)

        def invalid_constant(value):
            raise ValueError(f'Invalid JSON constant in classified export: {value}')

        keys = {}

        def shared_keys(pairs):
            # A single json.load shares object keys internally. Preserve that
            # memory saving when decoding individual events across many lines.
            return {keys.setdefault(key, key): value for key, value in pairs}

        decoder = json.JSONDecoder(parse_constant=invalid_constant, object_pairs_hook=shared_keys)
        # The framed format is still valid JSON, with one complete event per
        # line. Parse the small header separately to avoid a dataset-sized string.
        data = decoder.decode(first_line.rstrip() + ']}')
        counts = data.get('counts')
        if not isinstance(counts, dict) or any(
            type(counts.get(kind)) is not int or counts[kind] < 0
            for kind in ('posts', 'likes')
        ):
            raise ValueError('Invalid classified export event counts')

        events = data['events']
        actual = {'posts': 0, 'likes': 0}
        needs_event = False
        for line_number, line in enumerate(stream, 2):
            line = line.strip()
            if not line:
                continue
            if line == ']}':
                if needs_event:
                    raise ValueError('Trailing comma in classified export events')
                if any(tail.strip() for tail in stream):
                    raise ValueError('Unexpected content after classified export')
                if any(actual[kind] != counts[kind] for kind in actual):
                    raise ValueError(f'Classified export event count mismatch: {actual}')
                return data
            if events and not needs_event:
                raise ValueError(f'Missing event separator at line {line_number}')
            needs_event = line.endswith(',')
            event = decoder.decode(line[:-1] if needs_event else line)
            if not isinstance(event, dict) or event.get('kind') not in ('post', 'like'):
                raise ValueError(f'Invalid classified event at line {line_number}')
            actual['posts' if event['kind'] == 'post' else 'likes'] += 1
            events.append(event)
        raise ValueError('Truncated classified export: missing closing events array')


def load():
    # lru_cache alone permits concurrent misses to decode the million-event file twice.
    with _LOAD_LOCK:
        return _load(DATA.stat().st_mtime_ns)


def has_query_store():
    return all((PACKAGE / name).exists() for name in ('post-events.parquet', 'like-events.parquet'))


def query_metadata():
    if not has_query_store():
        return load()
    saved = manifest()
    return {'start': timestamp(saved['window']['start']), 'end': timestamp(saved['window']['end']),
            'companies': [{'id': c['id'], 'name': c['label']} for c in saved['taxonomy']['categories']]}


def available():
    return DATA.exists() and (PACKAGE / 'manifest.json').exists()


def manifest():
    return json.loads((PACKAGE / 'manifest.json').read_text(encoding='utf-8'))


def info():
    m = manifest()
    return {'id': 'twitter_firehose', 'name': 'Jev-classified X/Twitter export',
            'covers': m['window'], 'posts': m['counts']['exported_post_events'],
            'like_events': m['counts']['exported_like_events'], 'preclassified': True,
            'partial': m['partial'], 'companies': [c['label'] for c in m['taxonomy']['categories']],
            'company_ids': [{'id': c['id'], 'name': c['label']} for c in m['taxonomy']['categories']],
            'caveats': ['Partial classified export, not the full Twitter archive. Existing Jev labels and probabilities are used without reclassification.',
                        'Post publication and like-change events are separate. Opening like balances are not new likes. Language labels are unavailable.']}


def matcher(words):
    if any(w.lower().strip() in ('ai', 'artificial intelligence') for w in words):
        return lambda event: True
    normalize = lambda text: re.sub(r'[^a-z0-9]', '', text.lower())
    keys = {normalize(w.lstrip('#')) for w in words}
    companies = {c['id'] for c in manifest()['taxonomy']['categories']
                 if keys.intersection(normalize(v) for v in [c['id'], c['label'], *c.get('products', [])])}
    # One pattern for every word. Its lookbehind stops the regex engine from skipping ahead, so a
    # million posts cost seconds per word; most contain none of them, and a substring test says so.
    pattern = re.compile(r'(?<!\w)(?:' + '|'.join(re.escape(w) for w in words) + r')(?!\w)', re.I)
    needles = [w.lower() for w in words] if all(w.isascii() for w in words) else None

    def in_text(text):
        if needles is not None:
            lowered = text.lower()
            # re.I also reads dotless i, dotted capital I and long s as ASCII letters; lower() does not.
            if not any(n in lowered for n in needles) and (lowered.isascii() or not any(c in lowered for c in '\u0131\u017f\u0307')):
                return False
        return pattern.search(text) is not None

    return lambda event: any(g['company'] in companies for g in event['grades']) or in_text(event['text'])


def timestamp(value):
    """Date-only and timezone-less archive dates mean UTC, including on Windows."""
    parsed = datetime.fromisoformat(value)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp() * 1000


def iso(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat().replace('+00:00', 'Z')


def window(data, low, high):
    start, end = timestamp(low) if low else data['start'], timestamp(high) if high else data['end']
    if start >= end:
        raise ValueError('The start must be before the exclusive end.')
    return start, end


def post_url(identifier):
    identifier = identifier.removeprefix('twitter:')
    return 'https://x.com/i/status/' + identifier if identifier.isdigit() else None


def company_scope(words, company_ids=None):
    categories = manifest()['taxonomy']['categories']
    if company_ids is not None:
        known = {c['id'] for c in categories}
        if not company_ids or set(company_ids) - known:
            raise ValueError('Use company IDs from the chart context or describe_sources.')
        return set(company_ids)
    if any(w.lower().strip() in ('ai', 'artificial intelligence') for w in words):
        return None
    normalize = lambda text: re.sub(r'[^a-z0-9]', '', text.lower())
    keys = {normalize(w.lstrip('#')) for w in words}
    return {c['id'] for c in categories if keys.intersection(
        normalize(v) for v in [c['id'], c['label'], *c.get('products', [])])} or None


def activities(words, end, start=None, company_ids=None):
    """Use exactly the replay's filtered events and opening-balance rules."""
    if has_query_store():
        import classified_store
        return classified_store.activities(PACKAGE, words, start if start is not None else query_metadata()['start'],
                                           end, company_ids, manifest()['taxonomy']['categories'])
    data = scan(words, [])['dataset']
    seen, balances, rows = set(), {}, []
    for event in data['events']:
        if event['t'] >= end:
            break
        if event['id'] in seen:
            continue
        seen.add(event['id'])
        likes = 0
        if event['kind'] == 'like':
            previous, known = balances.get(event['postId'], (0, False))
            likes = 0 if event['opening'] and known else event['delta']
            balances[event['postId']] = (event['delta'] if event['opening'] else previous + event['delta'],
                                          event['opening'] or known)
        rows.append((event, likes))
    return rows, balances


def groups(rows, start, company_ids):
    result = {}
    for event, likes in rows:
        if event['t'] < start:
            continue
        for grade in event['grades']:
            if company_ids is not None and grade['company'] not in company_ids:
                continue
            row = result.setdefault(grade['company'], {}).setdefault(event['postId'], {'published': False, 'likes': 0})
            row['published'] |= event['kind'] == 'post'
            row['likes'] += likes
            row.update(score=grade['score'], grade=grade, event=event)
    for posts in result.values():
        for row in posts.values():
            row['weight'] = int(row['published']) + math.log1p(max(0, row['likes']))
    return result


def select(data, words):
    """The events a replay of these words shows. Every open of the page and every chat query asks
    for the same few word lists, so the last ones are kept; a replaced export drops them."""
    global _SELECTION_SOURCE, _ALL_SELECTED
    key = tuple(words)
    with _SELECT_LOCK:
        if (_SELECTION_SOURCE is None or _SELECTION_SOURCE[0] is not data
                or _SELECTION_SOURCE[1] != len(data['events'])):
            _SELECTED.clear()
            _ALL_SELECTED = None
            _SELECTION_SOURCE = data, len(data['events'])
        if any(w.lower().strip() in ('ai', 'artificial intelligence') for w in words):
            if _ALL_SELECTED is None:
                # All-topic replays keep the original list. Count and discover
                # present companies together, once, without matching or copying.
                present, posts = set(), 0
                for event in data['events']:
                    posts += event['kind'] == 'post'
                    present.update(grade['company'] for grade in event['grades'])
                _ALL_SELECTED = (data['events'],
                                 [c for c in data['companies'] if c['id'] in present], posts)
            return _ALL_SELECTED
        saved = _SELECTED.get(key)
        if saved is not None:
            return saved
        matches = matcher(words)
        hits = [matches(e) for e in data['events']]
        post_ids = {e['postId'] for e, hit in zip(data['events'], hits) if hit and e['kind'] == 'post'}
        events = [e for e, hit in zip(data['events'], hits) if hit or e['postId'] in post_ids]
        present = {g['company'] for e in events for g in e['grades']}
        selected = events, [c for c in data['companies'] if c['id'] in present], sum(e['kind'] == 'post' for e in events)
        _SELECTED.pop(key, None)
        while len(_SELECTED) >= 4:
            _SELECTED.pop(next(iter(_SELECTED)))
        _SELECTED[key] = selected
        return selected


def scan(words, names):
    data = load()
    events, companies, posts = select(data, words)
    likes = len(events) - posts
    return {'format': 'jev-classified-events', 'dataset': {**data, 'events': events, 'companies': companies, 'counts': {'posts': posts, 'likes': likes}},
            'source': 'twitter_archive', 'now': None, 'streaming': False, 'read': data['counts']['posts'], 'kept': posts,
            'note': f'Jev-classified Twitter export · Aug 17–Sep 17, 2026 · {posts:,} matching posts · {likes:,} like events · {len(companies)} companies. Partial export; existing classifications, no new scoring.'}


def preview(words, low=None, high=None, language=None):
    if language:
        return {'error': {'code': 'language_unavailable', 'message': 'This classified export has no language labels.', 'hint': 'Search without a language filter.'}}
    data = query_metadata()
    start, end = window(data, low, high)
    activity, recorded = activities(words, end, start)
    posts = [e for e, _ in activity if e['kind'] == 'post' and start <= e['t'] < end]
    balances = {identifier: value for identifier, (value, known) in recorded.items() if known}
    day = lambda t: datetime.fromtimestamp(t / 1000, timezone.utc).strftime('%Y-%m-%d')
    counts = Counter(day(e['t']) for e in posts)
    examples = sorted(posts, key=lambda e: balances.get(e['postId'], 0), reverse=True)[:6]
    return {'keywords': words, 'date_from': iso(start), 'date_to': iso(end), 'total': len(posts), 'exact': True, 'seconds': 0,
            'per_day': [{'day': d, 'count': n} for d, n in sorted(counts.items())],
            'examples': [{'id': e['postId'], 'day': day(e['t']), 'like_count': balances.get(e['postId']), 'body': e['text'][:240], 'full_text': e['text'],
                          'url': post_url(e['postId']), 'classifications': e['grades']} for e in examples],
            'note': 'Counts cover the partial Jev-classified export only. Existing labels are preserved. Likes are recorded balances through the selected window end, and may be unknown.'}


def sentiment(words, low=None, high=None, company_ids=None):
    data = query_metadata()
    start, end = window(data, low, high)
    selected = company_scope(words, company_ids)
    activity, _ = activities(words, end, start, selected)
    grouped = groups(activity, start, selected)
    summaries = []
    for company in data['companies']:
        if selected is not None and company['id'] not in selected:
            continue
        rows = grouped.get(company['id'], {})
        weight = total = scored = 0
        for row in rows.values():
            w = row['weight']
            if row['score'] is not None and w > 0:
                weight += w
                total += w * row['score']
                scored += 1
        summaries.append({'id': company['id'], 'company': company['name'], 'posts': sum(r['published'] for r in rows.values()),
                          'active_posts': sum(r['weight'] > 0 for r in rows.values()),
                          'scored_posts': scored, 'sentiment': round(total / weight, 4) if weight else None})
    return {'source': 'Jev-classified Twitter export', 'from': iso(start), 'to': iso(end), 'keywords': words,
            'company_ids': sorted(selected) if selected is not None else None, 'companies': summaries,
            'method': 'Existing score = 5*(1+P(positive)-P(negative)); weight = publication baseline + log(1+net likes in window). No reclassification.',
            'note': 'Partial classified export. A post can belong to multiple companies; company counts must not be summed as unique posts.'}


def query_posts(words, low=None, high=None, company_ids=None, text_terms=None, match='any', sort='influence', limit=6):
    """Find evidence by activity time, including older posts receiving likes now."""
    start, end = window(query_metadata(), low, high)
    selected = company_scope(words, company_ids)
    if match not in ('any', 'all') or sort not in ('influence', 'negative', 'positive', 'likes', 'newest'):
        raise ValueError('Choose a supported matching and sorting mode.')
    if not isinstance(limit, int) or not 1 <= limit <= 12:
        raise ValueError('Request between 1 and 12 examples.')
    if text_terms and (len(text_terms) > 20 or any(not isinstance(t, str) or not 1 <= len(t.strip()) <= 80 for t in text_terms)):
        raise ValueError('Use at most 20 nonempty text terms of at most 80 characters.')
    activity, balances = activities(words, end, start, selected)
    grouped = groups(activity, start, selected)
    patterns = [re.compile(r'(?<!\w)' + re.escape(t.strip()) + r'(?!\w)', re.I) for t in text_terms or []]
    found = {}
    for company, posts in grouped.items():
        for identifier, row in posts.items():
            event, score, weight = row['event'], row['score'], row['weight']
            if not row['published'] and weight <= 0:
                continue
            if patterns and not (all if match == 'all' else any)(p.search(event['text']) for p in patterns):
                continue
            post = found.setdefault(identifier, {'event': event, 'published': row['published'], 'grades': [],
                                                 'weight': weight, 'period_likes': row['likes'], 'rank': 0})
            post['grades'].append({'company': company, 'score': score, 'choice': row['grade'].get('choice')})
            # Contribution relative to neutral, using the same post weight as the chart.
            direction = max(0, 5 - score) if sort == 'negative' and score is not None else (
                max(0, score - 5) if sort == 'positive' and score is not None else 0)
            post['rank'] = max(post['rank'], weight * direction if sort in ('negative', 'positive') else weight)
    posts = list(found.values())
    if sort in ('negative', 'positive'):
        posts = [post for post in posts if post['rank'] > 0]
    def rank(post):
        event = post['event']
        if sort == 'newest':
            return event.get('postTime') if event.get('postTime') is not None else -math.inf
        if sort == 'likes':
            balance, known = balances.get(event['postId'], (0, False))
            return balance if known else -math.inf
        return post['rank']
    posts.sort(key=rank, reverse=True)
    examples = []
    for post in posts[:limit]:
        event = post['event']
        published = event.get('postTime', event['t'] if event['kind'] == 'post' else None)
        balance, known = balances.get(event['postId'], (0, False))
        examples.append({'id': event['postId'], 'url': post_url(event['postId']),
                         'day': iso(published) if published is not None else 'Publication time unknown',
                         'activity_time': iso(event['t']), 'published_in_window': post['published'],
                         'body': event['text'][:240], 'full_text': event['text'],
                         'like_count': balance if known else None, 'period_likes': post['period_likes'],
                         'weight': round(post['weight'], 4), 'classifications': post['grades']})
    return {'source': 'Jev-classified Twitter export', 'from': iso(start), 'to': iso(end),
            'keywords': words, 'company_ids': sorted(selected) if selected is not None else None,
            'text_terms': text_terms or [], 'match': match, 'sort': sort, 'total': len(posts), 'exact': True,
            'examples': examples, 'note': 'Partial saved export. Results include posts published or receiving like activity in this window. '
            'Negative/positive sorting ranks weighted contributions relative to neutral, not proof of causation. '
            'Period likes include the first recorded opening balance; they are not necessarily new likes. '
            'Recorded total likes are null when the opening balance is unknown. Post claims are unverified.'}
