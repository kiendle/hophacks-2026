"""Tools over the saved Jev labels; never call a scoring service."""
import classified_data
import steps


def classified_sentiment(reason: str, keywords: list[str], date_from: str | None = None, date_to: str | None = None,
                         company_ids: list[str] | None = None) -> dict:
    """Read sentiment for companies using the active, already Jev-classified Twitter export.

    Use for archive sentiment questions instead of rescoring posts. Dates may include UTC
    times, e.g. 2026-08-22T08:00:00Z; the end is exclusive. Omit both for the full export.
    Copy chart context dataset_keywords into keywords and scope.company_ids into company_ids.
    This preserves the chart's dataset filter while isolating the selected companies. With no
    chart, keywords=['AI'] searches the whole export; company/product keywords infer a company.
    Keywords match ANY company/product alias or whole-word text; AI means the entire export.
    Returns the activity-weighted aggregate over this exact window, not a smoothed chart point.
    For a chart point use its date_from/date_to. For "why" also call query_classified_posts.
    """
    if not reason.strip() or not 1 <= len(keywords) <= 20 or any(not isinstance(k, str) or not 1 <= len(k.strip()) <= 80 for k in keywords):
        return {'error': 'Provide a reason and 1 to 20 short keywords.'}
    if not classified_data.available():
        return {'error': 'The classified export is not installed.'}
    try:
        return classified_data.sentiment(keywords, date_from, date_to, company_ids)
    except (ValueError, TypeError) as error:
        return {'error': str(error)}


def query_classified_posts(reason: str, keywords: list[str], date_from: str | None = None, date_to: str | None = None,
                           company_ids: list[str] | None = None, text_terms: list[str] | None = None,
                           match: str = 'any', sort: str = 'influence', limit: int = 6) -> dict:
    """Read actual saved posts behind a chart movement or answer a post search.

    Copy chart dataset_keywords into keywords and scope.company_ids into company_ids. Pass
    exact ISO times, end exclusive. Default chart questions to the replay cutoff; explicitly
    requested dates may query the whole saved archive, even ahead of playback. Like activity in this
    window can bring an OLD post into these results. No model calls or fresh classification.
    sort: negative/positive = strongest weighted downward/upward contributions relative to 5;
    influence = highest chart weight; likes = recorded total likes; newest = publication time.
    text_terms optionally narrows the chart dataset by literal whole-word phrases; match='all'
    requires every term, e.g. ['Claude', 'ChatGPT']. Company aliases are NOT expanded here.
    Use the displayed low/high point windows for dip/spike evidence. Posts describe claims,
    not verified news or proven causes. Returns at most 12 posts with links and saved grades.
    """
    if not reason.strip() or not 1 <= len(keywords) <= 20 or any(not isinstance(k, str) or not 1 <= len(k.strip()) <= 80 for k in keywords):
        return {'error': 'Provide a reason and 1 to 20 short keywords.'}
    if not classified_data.available():
        return {'error': 'The classified export is not installed.'}
    try:
        return classified_data.query_posts(keywords, date_from, date_to, company_ids, text_terms, match, sort, limit)
    except (ValueError, TypeError) as error:
        return {'error': str(error)}


def register(mcp):
    mcp.tool()(classified_sentiment)
    mcp.tool()(query_classified_posts)


def facts(fields):
    return [('Dataset search', ', '.join(fields.get('keywords', []))),
            ('Companies', ', '.join(fields.get('company_ids') or [])),
            ('From', fields.get('date_from', 'Archive start')), ('Until', fields.get('date_to', 'Archive end'))]


steps.register_tool('classified_sentiment', lambda fields: 'Reading saved sentiment',
                    lambda result, error: 'Could not read sentiment.' if error or result.get('error') else
                    f"Read sentiment for {len(result.get('companies', []))} companies in the selected window.", facts)
steps.register_tool('query_classified_posts', lambda fields: 'Reading posts behind the chart',
                    lambda result, error: 'Could not search saved posts.' if error or result.get('error') else
                    f"Found {result.get('total', 0):,} matching posts and read {len(result.get('examples', []))} examples.", facts)
