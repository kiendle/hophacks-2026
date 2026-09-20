"""Read only a query's events from the same classified Parquet package as replay.

The browser's million-event JSON is a transport, not a per-chat query database.
DuckDB projects the saved labels and computes like history without decoding it all.
"""
import re
from datetime import datetime, timezone

import duckdb


def activities(package, words, start, end, company_ids, categories):
    connection = duckdb.connect(config={'threads': '2', 'memory_limit': '384MB', 'preserve_insertion_order': 'false'})
    try:
        connection.read_parquet(str(package / 'post-events.parquet')).create_view('posts')
        connection.read_parquet(str(package / 'like-events.parquet')).create_view('likes')
        normalize = lambda text: re.sub(r'[^a-z0-9]', '', text.lower())
        keys = {normalize(w.lstrip('#')) for w in words}
        aliases = [c['id'] for c in categories if keys.intersection(
            normalize(v) for v in [c['id'], c['label'], *c.get('products', [])])]
        # RE2 has no lookbehind. Consuming Unicode word boundaries gives the same
        # boolean whole-word match without constructing Python objects for every row.
        pattern = r'(^|[^\p{L}\p{N}_])(?:' + '|'.join(re.escape(w) for w in words) + r')($|[^\p{L}\p{N}_])'
        all_posts = any(w.lower().strip() in ('ai', 'artificial intelligence') for w in words)
        params = {'start': datetime.fromtimestamp(start / 1000, timezone.utc),
                  'end': datetime.fromtimestamp(end / 1000, timezone.utc), 'aliases': aliases,
                  'companies': list(company_ids) if company_ids is not None else None}
        match = 'TRUE' if all_posts else '(len(list_intersect(classification.companies, $aliases)) > 0 OR regexp_matches({text}, $pattern, \'i\'))'
        matching = {} if all_posts else {'aliases': aliases, 'pattern': pattern}
        connection.execute('CREATE TEMP TABLE matched_posts AS SELECT post_id FROM posts WHERE ' +
                           match.format(text='content'), matching)
        # The replay includes all changes linked to a matching publication, plus self-contained
        # matching likes. Compute history BEFORE clipping the window or selecting companies.
        predicate = 'TRUE' if all_posts else ('(post_id IN (SELECT post_id FROM matched_posts) OR ' + match.format(text='post_content') + ')')
        connection.execute('''CREATE TEMP TABLE like_history AS
            SELECT event_id, post_id, time, likes_delta, is_opening,
                sum(CASE WHEN NOT is_opening THEN likes_delta ELSE 0 END) OVER history AS changes,
                count(*) FILTER (WHERE is_opening) OVER history AS openings
            FROM likes WHERE time < $end AND ''' + predicate + '''
            WINDOW history AS (PARTITION BY post_id ORDER BY time, event_id ROWS UNBOUNDED PRECEDING)''',
            {'end': params['end'], **matching})
        balances = {}
        for identifier, changes, opening in connection.execute('''SELECT post_id,
                arg_max(changes, (time, event_id)),
                arg_max(struct_pack(value := likes_delta, changes := changes), (time, event_id)) FILTER (WHERE is_opening)
            FROM like_history GROUP BY post_id''').fetchall():
            balances[identifier] = ((opening['value'] + changes - opening['changes']) if opening else changes, opening is not None)
        grade_list = '''list_transform(list_filter(s.classification.companies,
            company -> $companies IS NULL OR list_contains($companies, company)), company -> struct_pack(
                company := company, choice := s.sentiment[company].choice,
                score := CASE WHEN s.sentiment[company].choice = 'insufficient_evidence' THEN NULL
                    ELSE round(5 * (1 + s.sentiment[company].probabilities['positive']
                                      - s.sentiment[company].probabilities['negative']), 10) END))'''
        result = connection.execute(f'''SELECT * FROM (
            SELECT s.event_id AS id, s.post_id AS postId, 'post' AS kind, epoch_ms(s.time) AS t,
                epoch_ms(s.time) AS postTime, s.content AS text, {grade_list} AS grades,
                0 AS activity_likes
            FROM posts s WHERE s.time >= $start AND s.time < $end
                AND s.post_id IN (SELECT post_id FROM matched_posts)
            UNION ALL
            SELECT s.event_id, s.post_id, 'like', epoch_ms(s.time), epoch_ms(p.time), s.post_content,
                {grade_list}, CASE WHEN s.is_opening AND h.openings > 1 THEN 0 ELSE s.likes_delta END
            FROM likes s JOIN like_history h ON h.event_id = s.event_id
                LEFT JOIN posts p ON p.post_id = s.post_id
            WHERE s.time >= $start AND s.time < $end
        ) WHERE len(grades) > 0 ORDER BY t, kind != 'post', id''',
            {key: params[key] for key in ('start', 'end', 'companies')})
        names = [column[0] for column in result.description]
        rows = []
        while batch := result.fetchmany(1024):
            for values in batch:
                event = dict(zip(names, values))
                rows.append((event, event.pop('activity_likes')))
        return rows, balances
    finally:
        connection.close()
