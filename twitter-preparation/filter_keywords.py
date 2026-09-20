# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2"]
# ///
"""Screen observation rows for AI-company candidates; never invoke an LLM.

Output contains matching observations, not complete selected-post histories and
not replay-ready events. Recover all observations of selected IDs before building
like deltas. Use per-version content rather than projecting later hits backward.
"""
import argparse
import hashlib
import json
import re
import time
from pathlib import Path

import duckdb


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def term_pattern(term):
    escaped = re.escape(term.lower()).replace(r'\ ', r'\s+')
    if term.isascii():
        return r'(^|[^a-z0-9])(?:' + escaped + r')([^a-z0-9]|$)'
    return escaped


def union_pattern(terms, patterns=()):
    branches = [term_pattern(t) for t in sorted(set(terms))]
    branches += [r'(^|[^a-z0-9])(?:' + p + r')([^a-z0-9]|$)' for p in patterns]
    return '(?:' + '|'.join(branches) + ')' if branches else None


def matches(pattern):
    return 'FALSE' if pattern is None else 'regexp_matches(_keyword_text, ' + quote(pattern) + ')'


def compile_rules(config):
    companies = config['companies']
    identifiers = [company['id'] for company in companies]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError('Duplicate company identifier')
    direct_terms = [t for company in companies for t in company['direct']]
    ambiguous_terms = [t for company in companies for t in company['ambiguous']]
    contextual_terms = [t for company in companies for t in company['contextual']]
    direct_patterns = [p for company in companies for p in company.get('direct_patterns', [])]
    broad = union_pattern(direct_terms + ambiguous_terms + contextual_terms + config['discovery_terms'], direct_patterns)
    context = union_pattern(config['ai_context_terms'] + direct_terms + ambiguous_terms, direct_patterns)
    discovery = union_pattern(config['discovery_terms'])
    tiers = {}
    for tier in ['direct', 'ambiguous', 'contextual']:
        cases = []
        for company in companies:
            patterns = company.get('direct_patterns', []) if tier == 'direct' else []
            condition = matches(union_pattern(company[tier], patterns))
            if tier == 'contextual':
                condition = '_ai_context AND (' + condition + ')'
            cases.append('CASE WHEN ' + condition + ' THEN ' + quote(company['id']) + ' ELSE NULL END')
        tiers[tier] = 'list_filter([' + ','.join(cases) + '], x -> x IS NOT NULL)'
    return broad, context, discovery, tiers


def screen_sql(source, config):
    broad, context, discovery, tiers = compile_rules(config)
    return f"""WITH normalized AS (
        SELECT *, translate(lower(nfc_normalize(coalesce(body,''))), '‐‑‒–—−', '------') AS _keyword_text
        FROM {source}
    ), candidates AS MATERIALIZED (
        SELECT *, {matches(context)} AS _ai_context FROM normalized WHERE {matches(broad)}
    ), hits AS (
        SELECT * EXCLUDE (_keyword_text),
            {tiers['direct']} AS direct_companies,
            {tiers['ambiguous']} AS ambiguous_companies,
            {tiers['contextual']} AS contextual_companies,
            {matches(discovery)} AS discovery_match
        FROM candidates
    ), attributed AS (
        SELECT *, list_sort(list_distinct(list_concat(direct_companies, ambiguous_companies, contextual_companies))) AS company_candidates
        FROM hits
    ) SELECT * FROM attributed WHERE len(company_candidates)>0 OR discovery_match"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default='twitter-analysis/tweet-id-sample.parquet')
    parser.add_argument('--config', type=Path, default=Path('twitter-preparation/ai-keywords.json'))
    parser.add_argument('--output-dir', type=Path, default=Path('twitter-preparation/keyword-preview'))
    parser.add_argument('--scope', choices=['sample', 'full'], default='sample', help='Explicit provenance label; no sampling is performed by this command.')
    args = parser.parse_args()
    config_bytes = args.config.read_bytes()
    config = json.loads(config_bytes)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / 'matching-observations.parquet'
    report_path = args.output_dir / 'report.json'
    config_snapshot = args.output_dir / 'filter-config.json'
    if output.exists() or report_path.exists() or config_snapshot.exists():
        raise FileExistsError('Output already exists; select another output directory.')
    config_snapshot.write_bytes(config_bytes)
    c = duckdb.connect()
    c.execute("SET threads=4; SET memory_limit='4GB'; SET TimeZone='UTC'")
    source = 'read_parquet(' + quote(args.source) + ')'
    started = time.monotonic()
    c.execute('COPY (' + screen_sql(source, config) + ') TO ' + quote(output) + ' (FORMAT PARQUET, COMPRESSION ZSTD)')
    c.execute('CREATE VIEW matches AS SELECT * FROM read_parquet(' + quote(output) + ')')

    def records(sql):
        cur = c.execute(sql)
        return [dict(zip([col[0] for col in cur.description], row)) for row in cur.fetchall()]

    counts = records("""SELECT count(*) AS observations, count(DISTINCT id) AS post_ids,
        count(DISTINCT body) AS exact_texts,
        count(*) FILTER(WHERE starts_with(body,'RT @')) AS rt_observations,
        count(DISTINCT id) FILTER(WHERE NOT starts_with(body,'RT @')) AS non_rt_post_ids,
        count(DISTINCT id) FILTER(WHERE starts_with(body,'RT @')) AS rt_post_ids,
        count(DISTINCT body) FILTER(WHERE NOT starts_with(body,'RT @')) AS non_rt_exact_texts,
        count(*) FILTER(WHERE len(company_candidates)=0) AS discovery_only_observations,
        count(*) FILTER(WHERE len(direct_companies)=0 AND len(ambiguous_companies)>0) AS ambiguous_without_direct_observations,
        count(*) FILTER(WHERE len(company_candidates)>1) AS multi_company_observations
        FROM matches""")[0]
    by_company = records("""SELECT company,count(*) AS observations,count(DISTINCT id) AS post_ids,
        count(DISTINCT body) AS exact_texts FROM (SELECT id,body,unnest(company_candidates) AS company FROM matches)
        GROUP BY company ORDER BY post_ids DESC,company""")
    examples = records("""WITH expanded AS (
        SELECT id,body,lang,version,unnest(company_candidates) AS company FROM matches
    ), distinct_text AS (
        SELECT * FROM expanded QUALIFY row_number() OVER(PARTITION BY company,id,body ORDER BY version)=1
    ) SELECT company,id,lang,left(body,650) AS body_excerpt FROM distinct_text
        QUALIFY row_number() OVER(PARTITION BY company ORDER BY hash(id,body))<=4 ORDER BY company,id""")
    daily = records("""SELECT cast(created_at AS DATE) AS day,count(*) AS observations,
        count(DISTINCT id) AS post_ids,
        count(DISTINCT id) FILTER(WHERE NOT starts_with(body,'RT @')) AS non_rt_post_ids
        FROM matches GROUP BY day ORDER BY day""")
    text_workload = records("""SELECT count(*) AS exact_texts,sum(length(body)) AS characters,
        sum(octet_length(encode(body))) AS utf8_bytes
        FROM (SELECT DISTINCT body FROM matches)""")[0]
    report = {
        'scope': args.scope, 'source': args.source, 'config_version': config['version'],
        'config_sha256': hashlib.sha256(config_bytes).hexdigest(), 'duckdb_version': duckdb.__version__,
        'source_observations': c.execute('SELECT count(*) FROM ' + source).fetchone()[0],
        'selected': counts, 'company_candidates': by_company, 'examples': examples,
        'daily': daily, 'text_workload': text_workload, 'matching_parquet_bytes': output.stat().st_size,
        'elapsed_seconds': time.monotonic()-started,
        'limits': [
            'Keyword hits are not Jev company attribution or sentiment results.',
            'This file retains matching observations only; it is not complete post history or replay-ready data.',
            'No source language restriction. Exact-text dedup count does not imply those authors/posts are equivalent.',
            'Ambiguous and context-gated aliases trade recall against noise; no full-corpus recall guarantee.',
            'Sample is an existing deterministic ID sample; do not present sample counts as full-corpus totals.' if args.scope == 'sample' else 'All rows in the explicitly supplied source were screened.'
        ]
    }
    report_path.write_text(json.dumps(report,indent=2,ensure_ascii=False,default=str)+'\n')
    print(json.dumps({key:value for key,value in report.items() if key!='examples'},indent=2,ensure_ascii=False,default=str))


if __name__ == '__main__':
    main()
