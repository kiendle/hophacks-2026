"""Tools for asking questions about a finished project: what exists, the chart numbers, the posts.

register(mcp) adds them to the harness MCP server, so the chat can go from "why did the feeling
drop" to the numbers behind the move and the real posts from that day. Everything is read through
pipeline.FakePipeline, which owns the DuckDB reads, the timeouts and the page cap, and through
charts.py, which owns the packaged numbers. Nothing here computes a statistic of its own.

Every chart result also carries a "_card": the page draws the picture while the model reads the
numbers. The caption on that card is written here, in code, from the chart's own summary and
annotations, so the picture can never be captioned with something the model made up.

Errors are returned, never raised, and every word a person can read is plain: days like "Sep 9",
groups by their own names, and a feeling as "minus 0.31", never a field name.
"""
import functools
import inspect
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import charts
import pipeline
import steps

HARNESS = Path(__file__).resolve().parent
PROJECTS = HARNESS / "demo" / "projects"
STATE = HARNESS / "state" / "pipeline"
PROJECT_ID = re.compile(r"[a-z0-9_-]{1,60}")
CHART_ID = re.compile(r"[a-z0-9_-]{1,60}")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
REASON_LIMIT = 240
MAX_POSTS = pipeline.MAX_POSTS_PER_PAGE  # 50: a page size, not a budget
DEFAULT_POSTS = 20  # get_posts' own default, so an omitted limit and the step title agree
NAME_LIMIT, LABEL_LIMIT = 80, 60
# Invisible characters: a project name a user typed could otherwise carry a right to left override
# and turn the rest of a card line, or of a step title, backwards on the page.
UNSEEN = re.compile("[" + "".join(f"{chr(low)}-{chr(high)}" for low, high in (
    (0x00, 0x08), (0x0B, 0x1F), (0x7F, 0x9F), (0xAD, 0xAD), (0x200B, 0x200F),
    (0x202A, 0x202E), (0x2060, 0x2064), (0x2066, 0x2069), (0xFEFF, 0xFEFF))) + "]")
SORTS = {"most liked": "engagement", "random": "random", "most negative": "most_negative",
         "most positive": "most_positive", "least certain": "low_confidence"}
STATUS_WORDS = {"READY": "finished", "RUNNING": "still running", "QUEUED": "waiting to start",
                "FAILED": "not finished"}
CHART_TITLES = {"volume_daily": "How many posts each day", "sentiment_daily": "How the feeling changed each day",
                "share_daily": "Which groups people posted each day", "totals": "How many posts in each group",
                "top_posts": "The most liked posts"}
CHART_WORDS = {"volume_daily": "the chart of how many posts", "sentiment_daily": "the feeling chart",
               "share_daily": "the chart of groups by day", "totals": "the chart of group totals",
               "top_posts": "the most liked posts"}
POSTS_NOTE = ("Feeling runs from minus 1, very negative, to plus 1, very positive. "
              "These are posts people wrote themselves. Nothing anybody claims inside a post has been checked.")

REASON_DOC = """
    reason: one short sentence that starts with a verb, written for the user in the user's language,
    saying why you are doing this right now. Everyday words only. No tool names, no field names, no
    dashes, no semicolons. The user reads it exactly as you wrote it.
    """


def fail(code, message, hint):
    return {"error": {"code": code, "message": message, "hint": hint}}


def guarded(function):
    """A tool answers with a sentence, never with an exception.

    The model can pass anything, and a project on disk can be half written, so every way out of a
    tool is a dict: a PipelineError keeps its own words, and anything else becomes one plain line.
    """
    @functools.wraps(function)
    def wrapper(*arguments, **keywords):
        try:
            return function(*arguments, **keywords)
        except pipeline.PipelineError as error:
            return error.as_dict()
        except Exception as error:  # noqa: BLE001  (a bad argument or a half written file is not a crash)
            print(f"analysis_tools: {function.__name__} failed: {error!r}", flush=True)  # the log, never the user
            return fail("no_results", "Something went wrong reading this project.",
                        "Say plainly that this one could not be read, and offer another project.")
    return wrapper


# ------------------------------------------------------------------ plain words
def _safe_text(value, limit):
    """Anything a person wrote that a reader will see: no invisible characters, and never endless."""
    text = steps.plain(UNSEEN.sub("", " ".join(str(value or "").split())))
    return text[:limit].rstrip(" ,")


def _day_words(value):
    """2026-09-09 to "Sep 9". The year appears only when it is not the year the data is from."""
    try:
        year, month, day = (int(part) for part in UNSEEN.sub("", str(value)).split("-"))
        if not 1 <= month <= 12 or not 1 <= day <= 31:
            return ""
    except (TypeError, ValueError):
        return ""
    return f"{steps.MONTHS[month - 1]} {day}" + ("" if year == steps.BASE_YEAR else f" {year}")


def _before(value):
    """The last day a window really covers: its `to` is the first day NOT included."""
    try:
        return (date.fromisoformat(str(value)) - timedelta(days=1)).isoformat()
    except (TypeError, ValueError):
        return ""


def _span_words(date_from, date_to):
    """The days a person reads: "Sep 9", "Sep 6 to Sep 13", or the whole project when nothing is set."""
    low, high = _day_words(date_from), _day_words(_before(date_to))
    if low and high:
        return low if low == high else f"{low} to {high}"
    if low:
        return f"{low} onwards"
    return f"up to {high}" if high else "the whole project"


def _score_words(value):
    """minus 0.31, plus 0.12, zero: a feeling score said out loud."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ""
    if round(value, 2) == 0:
        return "zero"
    return ("minus " if value < 0 else "plus ") + f"{abs(value):.2f}"


def _count_words(value, word="post"):
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):  # a count of infinity is not a count
        return ""
    return f"{count:,} {word}" + ("" if count == 1 else "s")


def _label_words(value):
    """A label from the project's own spec, said out loud: neutral_news reads "neutral news"."""
    return _safe_text(str(value or "").replace("_", " "), LABEL_LIMIT)


def _page_size(value, default=DEFAULT_POSTS):
    """How many posts a call really returns. A page size is not worth an error, so junk means the default."""
    try:
        wanted = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(1, min(wanted, MAX_POSTS))


def _day_value(value, which):
    """A day the caller asked for, checked here so a huge or odd argument never becomes a long message."""
    if value is None or value == "":
        return None
    text = _safe_text(value, 40)
    hint = "Pass a day as 2026-09-09. Leave both days out for the whole project."
    if not DAY.fullmatch(text):
        raise pipeline.PipelineError("bad_request", f"The {which} day is {text!r}, which is not a date.", hint)
    try:
        date.fromisoformat(text)
    except ValueError as error:
        raise pipeline.PipelineError("bad_request", f"The {which} day is {text!r}, which is not a real day.", hint) from error
    return text


def _sort_words(value):
    """The sort the caller meant. Nothing at all means the default, anything else has to be one of ours."""
    text = _safe_text(value, 40).lower() if value is not None else ""
    if not text:
        return "most liked"
    if text not in SORTS:
        raise pipeline.PipelineError("bad_sort", f'"{text}" is not a way of sorting posts.',
                                     f"Use one of: {', '.join(SORTS)}.")
    return text


def _kind(chart_id):
    """Which of the five standard charts this is, whatever question it was built for."""
    name = str(chart_id or "")
    for prefix in ("share_daily", "totals"):
        if name.startswith(prefix):
            return prefix
    return name


def _chart_title(chart_id):
    return CHART_TITLES.get(_kind(chart_id)) or "A chart"


# ------------------------------------------------------------------ the projects
def _project_dir(project_id):
    if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
        raise pipeline.PipelineError("unknown_project", f"There is no project called {str(project_id)[:60]!r}.",
                                     "Call list_projects and use one of the ids it gives you.")
    directory = PROJECTS / project_id
    if not (directory / "spec.json").exists():
        raise pipeline.PipelineError("unknown_project", f"There is no project called {project_id!r}.",
                                     "Call list_projects and use one of the ids it gives you.")
    return directory


def _spec(project_id):
    return json.loads((_project_dir(project_id) / "spec.json").read_text(encoding="utf-8"))


def _run(project_id):
    path = _project_dir(project_id) / "run.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _pipe(project_id):
    """One FakePipeline per project: it serves a single id, and it owns every read of the results."""
    return pipeline.FakePipeline(PROJECTS, STATE, project_id=project_id)


def _run_async(coroutine):
    """The tools are plain functions, so the event loop lives in a worker thread for the call."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_async_result, coroutine).result()


def _async_result(coroutine):
    import asyncio

    return asyncio.run(coroutine)


def _choice_question(spec):
    for question in spec.get("classification") or []:
        if question.get("type") == "choice" and question.get("name"):
            return question
    return None


def _groups(spec):
    question = _choice_question(spec) or {}
    names = [option if isinstance(option, str) else (option or {}).get("name") for option in question.get("options") or []]
    return [name for name in names if name]


def _group_words(spec):
    return [_label_words(name) for name in _groups(spec)]


def _match_group(spec, group):
    """The group the user or the model meant. "neutral news" and "neutral_news" are the same group."""
    wanted = str(group or "").strip().lower().replace("_", " ")
    return next((name for name in _groups(spec) if name.lower().replace("_", " ") == wanted), None)


def _known_projects():
    """Every finished project on this laptop: the demo ones, plus what the pipeline reports READY."""
    ids = sorted(path.parent.name for path in PROJECTS.glob("*/spec.json")) if PROJECTS.is_dir() else []
    try:
        extra = getattr(pipeline.get_pipeline(HARNESS.parent), "project_id", None)
        if extra and extra not in ids and _run_async(_pipe(extra).status(extra)).get("status") == "READY":
            ids.append(extra)
    except Exception:  # noqa: BLE001  (a pipeline that is not wired up must not hide the demo projects)
        pass
    return ids


def _describe(project_id):
    """One plain line about a project, built from its own spec and run."""
    spec, run = _spec(project_id), _run(project_id)
    window = (spec.get("observation") or {}).get("window") or {}
    days = _span_words(window.get("from"), window.get("to"))
    language = steps.language_words(((spec.get("filter") or {}).get("languages") or [None])[0])
    words = steps.word_list((spec.get("filter") or {}).get("any_terms") or [], 6)
    read = _count_words(run.get("n_scored") or 0)
    source = steps.source_words((spec.get("observation") or {}).get("source"))
    name = _safe_text(spec.get("name") or project_id, NAME_LIMIT) or project_id
    line = f'"{name}". {read} from {source}, {days}, in {language}'
    line += f", about {words}." if words else "."
    return {
        "project_id": project_id,
        "name": name,
        "about": steps.plain(line),
        "days": days,
        "posts_read": run.get("n_scored") or 0,
        "groups": _group_words(spec),
        "status": STATUS_WORDS.get(str(run.get("status") or "READY").upper(), "finished"),
    }


# ------------------------------------------------------------------ the card on a chart
def _points_of(chart):
    return [(series.get("name"), point) for series in chart.get("series") or [] for point in series.get("points") or []]


def _pick(chart):
    """The three points worth saying out loud: the change point, the lowest, the highest, the last."""
    summary, multi = chart.get("summary") or {}, len(chart.get("series") or []) > 1
    change = next((a for a in chart.get("annotations") or [] if a.get("kind") == "largest_change"), None)
    if chart.get("type") in ("bar", "table"):
        ranked = sorted((pair for pair in _points_of(chart) if pair[1].get("y") is not None),
                        key=lambda pair: -pair[1]["y"])
        return ranked[:3]
    wanted, seen = [], set()
    marks = [change] + [summary.get(key) for key in ("min", "max", "end", "start")]
    for mark in marks:
        if not isinstance(mark, dict) or mark.get("x") is None:
            continue
        key = (mark.get("series"), mark["x"])
        if key in seen:
            continue
        seen.add(key)
        wanted.append(key)
    chosen = []
    for series_name, x in wanted[:3]:
        for name, point in _points_of(chart):
            if point.get("x") == x and (series_name is None or not multi or name == series_name) and point.get("y") is not None:
                chosen.append((name, point))
                break
    return sorted(chosen, key=lambda pair: str(pair[1].get("x")))


def _line(chart, series_name, point):
    """One plain line for the card: "Sep 9: minus 0.31, 412 posts"."""
    kind, x = _kind(chart.get("chart_id")), point.get("x")
    day, y = _day_words(x), point.get("y")
    multi = len(chart.get("series") or []) > 1
    if kind == "sentiment_daily":
        return {"label": day or _label_words(x), "value": _score_words(y), "note": _count_words(point.get("n"))}
    if kind == "volume_daily":
        return {"label": day or _label_words(x), "value": _count_words(y), "note": ""}
    if kind == "share_daily":
        label = f"{day}, {_label_words(series_name)}" if multi and series_name else (day or _label_words(x))
        return {"label": _safe_text(label, LABEL_LIMIT), "value": f"{round((y or 0) * 100):g} out of 100 posts",
                "note": _count_words(point.get("count"))}
    if kind == "totals":
        share = point.get("share")
        return {"label": _label_words(x), "value": _count_words(y),
                "note": f"{round((share or 0) * 100):g} out of 100 posts" if share is not None else ""}
    if kind == "top_posts":
        return {"label": _day_words(point.get("day")) or _label_words(x), "value": _count_words(y, "like"), "note": ""}
    return {"label": _label_words(x), "value": _count_words(y), "note": ""}


def caption(chart):
    """One plain sentence about the chart, from its own summary and annotations. Never the model's."""
    kind = _kind(chart.get("chart_id"))
    summary = chart.get("summary") or {}
    change = next((a for a in chart.get("annotations") or [] if a.get("kind") == "largest_change"), None)
    top, low = summary.get("max") or {}, summary.get("min") or {}
    if kind == "sentiment_daily" and change:
        moved = "dropped" if (change.get("delta") or 0) < 0 else "rose"
        return steps.plain(f"Feeling {moved} the most on {_day_words(change.get('x'))}.")
    if kind == "volume_daily" and top.get("x") is not None:
        return steps.plain(f"The busiest day was {_day_words(top['x'])} with {_count_words(top.get('y'))}.")
    if kind == "share_daily" and change:
        series = _label_words(change.get("series")) or "One group"
        return steps.plain(f"{series[:1].upper()}{series[1:]} changed the most on {_day_words(change.get('x'))}.")
    if kind == "totals" and top.get("x") is not None:
        return steps.plain(f'The biggest group was "{_label_words(top["x"])}" with {_count_words(top.get("y"))}.')
    if kind == "top_posts" and top.get("y") is not None:
        return steps.plain(f"The most liked post got {_count_words(top['y'], 'like')}.")
    if top.get("x") is not None and low.get("x") is not None:
        return steps.plain(f"The highest point was {_day_words(top['x']) or _label_words(top['x'])} and the lowest "
                           f"was {_day_words(low['x']) or _label_words(low['x'])}.")
    return "There is nothing to show in this chart yet."


def card(project_id, chart):
    """What the page draws: the picture, one sentence, and the three points that matter."""
    chart_id = chart.get("chart_id") or ""
    return {
        "kind": "chart",
        "title": _chart_title(chart_id),
        "caption": caption(chart),
        "png_url": f"/api/projects/{project_id}/charts/{chart_id}.png",
        "points": [_line(chart, name, point) for name, point in _pick(chart)],
    }


# ------------------------------------------------------------------ the tools
@guarded
def list_projects() -> dict:
    """The finished projects on this laptop, the ones the user can ask questions about.

    Start here whenever the user asks about results, a topic we watched, or what we know. Each
    project comes with a plain line saying what was read and when. Use its `project_id` with the
    other tools, and tell the user the name, never the id.
    """
    projects = []
    for project_id in _known_projects():
        try:
            projects.append(_describe(project_id))
        except (pipeline.PipelineError, OSError, ValueError):
            continue
    if not projects:
        return fail("no_projects", "There are no finished projects on this laptop yet.",
                    "Say so plainly and offer to start a new one instead.")
    return {"projects": projects, "note": "These are finished projects. The numbers are already counted."}


@guarded
def list_charts(project_id: str) -> dict:
    """What we can show about one project: its charts, with a plain title for each.

    Call it before get_chart so you use a chart that exists. For a question about mood or feeling the
    chart is sentiment_daily. For how loud a day was it is volume_daily.
    """
    described = _describe(project_id)
    try:
        found = charts.list_charts(_project_dir(project_id))
    except (OSError, ValueError):  # a half written project: the reason is for the log, not for a reader
        return fail("no_results", f'The charts for "{described["name"]}" could not be read.',
                    "Say plainly that this one could not be read, and offer another project.")
    if not found:
        return fail("no_charts", f'"{described["name"]}" has no charts yet.',
                    "Say the project has no charts yet and offer to look at its posts instead.")
    return {"project_id": project_id, "project": described["name"],
            "charts": [{"chart_id": chart["chart_id"], "title": _chart_title(chart["chart_id"]),
                        "shows": "one line per day" if chart["type"] == "line" else "one bar for each group"
                        if chart["type"] == "bar" else "a list of posts"} for chart in found]}


@guarded
def get_chart(project_id: str, chart_id: str) -> dict:
    """The numbers behind one chart, and the picture for the user at the same time.

    Read `summary` first: it gives the first day, the last day, the highest and the lowest with their
    dates. `annotations` are computed in code: largest_change marks the biggest move between two days
    and is usually the answer to "when did it change", and low_n marks a day with fewer than 100
    posts, which is too thin to narrate. Every point carries `n`, the posts behind it, and the
    feeling chart also carries `y_unweighted`: `y` counts likes, so when `y` moves and `y_unweighted`
    does not, one very popular post carried the day and you should go and find it.
    The user already sees the picture and its caption, so do not describe the picture. Explain it.
    """
    directory = _project_dir(project_id)
    try:
        chart = _run_async(_pipe(project_id).chart(project_id, _safe_text(chart_id, 60)))
    except pipeline.PipelineError as error:
        # The project's own id would reach the activity row, so the sentence names the project instead.
        if error.code != "unknown_chart":
            raise
        known = [found["chart_id"] for found in charts.list_charts(directory)]
        return fail("unknown_chart", f'There is no chart like that for "{_named(project_id)}".',
                    f"Call list_charts for this project. It has: {', '.join(known) or 'no charts yet'}.")
    if isinstance(chart, dict) and "error" in chart:
        return chart
    return {**chart, "project_id": project_id, "_card": card(project_id, chart)}


@guarded
def get_posts(project_id: str, date_from: str | None = None, date_to: str | None = None,
              group: str | None = None, sort: str = "most liked", limit: int = DEFAULT_POSTS) -> dict:
    """Real posts from the project, to read and to quote.

    date_from is the first day and date_to is the day AFTER the last one, so one day is
    date_from="2026-09-09", date_to="2026-09-10". Leave both out for the whole project. `group` keeps
    only posts the project put in that group, and list_projects names the groups. `sort` is one of
    "most liked", "random", "most negative", "most positive" or "least certain": pick it on purpose,
    because it decides what you see. "most liked" finds the posts that carried a likes weighted day,
    "most negative" shows what a drop is made of, and "random" checks whether the loud posts are
    typical. At most 50 posts come back at a time, and that is a page, not the evidence: the numbers
    in the charts were counted from every post, so never recompute a share from what you read here.
    Quote two or three posts with their day and their likes, and say plainly that what a post claims
    has not been checked.
    """
    spec = _spec(project_id)
    question = _choice_question(spec)
    asked = _safe_text(group, LABEL_LIMIT) if group is not None else ""
    wanted = None
    if asked:  # nothing at all means every group, which is what an empty argument reads like
        if not question:
            return fail("no_groups", f'"{_describe(project_id)["name"]}" has no groups.', "Ask again without a group.")
        wanted = _match_group(spec, asked)
        if wanted is None:
            return fail("unknown_group", f'"{asked}" is not one of the groups.',
                        f"The groups are: {', '.join(_group_words(spec))}.")
    sort, size = _sort_words(sort), _page_size(limit)
    first, last = _day_value(date_from, "first"), _day_value(date_to, "last")
    found = _run_async(_pipe(project_id).posts(
        project_id, date_from=first, date_to=last,
        question=question["name"] if wanted is not None else None,
        label=wanted, sort=SORTS[sort], limit=size))
    name = question["name"] if question else None
    posts = [{
        "id": post.get("id"), "day": post.get("day"), "likes": post.get("like_count") or 0,
        "group": _label_words(post.get(name)) if name else None,
        "feeling": round(post["sentiment"], 3) if isinstance(post.get("sentiment"), (int, float)) else None,
        "text": post.get("body") or "",
    } for post in found]
    return {"project_id": project_id, "days": _span_words(first, last), "sort": sort,
            "group": _label_words(wanted) if wanted else None, "found": len(posts), "limit": size,
            "posts": posts, "note": POSTS_NOTE}


@guarded
def project_status(project_id: str) -> dict:
    """Whether one project is finished, and how much it read.

    Use it when the user asks whether something is done, or when a chart looks empty and you need to
    know whether the run failed before you explain anything.
    """
    described = _describe(project_id)
    status = _run_async(_pipe(project_id).status(project_id))
    counts = status.get("counts") or {}
    state = STATUS_WORDS.get(str(status.get("status") or "").upper(), "finished")
    answer = {"project_id": project_id, "project": described["name"], "state": state,
              "posts_read": counts.get("n_scored"), "posts_found": counts.get("n_matched"),
              "days": described["days"], "groups": described["groups"]}
    if status.get("error"):
        # The run's own message carries ids, file names and a raw list of reasons, so it is logged
        # and the model is told the one thing it has to act on instead.
        print(f"analysis_tools: {project_id} did not finish: {(status['error'] or {}).get('message')!r}", flush=True)
        answer["problem"] = "This project did not finish, so its numbers are not complete."
        answer["hint"] = "Say plainly that this project did not finish, and do not report its numbers as findings."
    return answer


TOOLS = (list_projects, list_charts, get_chart, get_posts, project_status)


def with_reason(function):
    """The tool the model sees takes `reason` first, so every step can tell the user why it happened.

    The function keeps its own signature, so our own code and the tests call it directly; the schema
    the model sees is built from __signature__, which has reason first and required.
    """
    @functools.wraps(function)
    def wrapper(reason=None, *arguments, **keywords):
        if not (" ".join(reason.split())[:REASON_LIMIT] if isinstance(reason, str) else ""):
            return fail("no_reason", "Every step needs a reason, one short plain sentence for the user.",
                        'Call it again with reason="…", saying in the user\'s language why you are doing this right now.')
        return function(*arguments, **keywords)

    first = inspect.Parameter("reason", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str)
    wrapper.__signature__ = inspect.Signature([first, *inspect.signature(function).parameters.values()])
    wrapper.__annotations__ = {"reason": str, **getattr(function, "__annotations__", {})}
    wrapper.__doc__ = (function.__doc__ or "").rstrip() + "\n" + REASON_DOC
    return wrapper


def register(mcp):
    """Add these tools to an MCP server object."""
    for tool in TOOLS:
        mcp.tool()(with_reason(tool))
    return TOOLS


# ----------------------------------------------------- the words the activity list shows
# The title comes from the call's own arguments and the outcome from the real result, both plain.
# Returning "" hands the sentence back to steps.py, which writes the failure line itself.
def _named(project_id):
    try:
        return _safe_text(_spec(project_id).get("name"), NAME_LIMIT) or "the project"
    except (pipeline.PipelineError, OSError, ValueError):
        return "the project"


def _answered(result):
    return isinstance(result, dict) and "error" not in result


def _projects_title(fields):
    return "Checking which finished projects we have"


def _projects_words(result, is_error):
    if not _answered(result):
        return ""
    names = [str(project.get("name") or "") for project in result.get("projects") or []]
    quoted = [f'"{name}"' for name in names if name]
    if not quoted:
        return "There are no finished projects yet."
    listed = quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + " and " + quoted[-1]
    return f"We have {len(quoted)} finished " + ("project" if len(quoted) == 1 else "projects") + f": {listed}."


def _charts_title(fields):
    return f'Looking at what we can show about "{_named(fields.get("project_id"))}"'


def _charts_words(result, is_error):
    if not _answered(result):
        return ""
    found = result.get("charts") or []
    return f"We can show {len(found)} " + ("chart." if len(found) == 1 else "charts.") if found else ""


def _chart_title_words(fields):
    return f'Looking at {CHART_WORDS.get(_kind(fields.get("chart_id")), "a chart")} for "{_named(fields.get("project_id"))}"'


def _chart_words(result, is_error):
    if not _answered(result):
        return ""
    return str(((result.get("_card") or {}) if isinstance(result.get("_card"), dict) else {}).get("caption") or "")


def _posts_title(fields):
    sort = _safe_text(fields.get("sort"), 40).lower()
    sort = sort if sort in SORTS else "most liked"
    # The number said here is the number that really comes back, so the title and "Found 50 posts."
    # can never contradict each other, whatever the call asked for.
    how_many = _page_size(fields.get("limit"))
    when = _span_words(fields.get("date_from"), fields.get("date_to"))
    where = f' in the "{_label_words(fields.get("group"))}" group' if fields.get("group") else ""
    head = (f"Reading {how_many} posts picked at random" if sort == "random"
            else f"Reading {how_many} of the {sort} posts")
    return steps.plain(f"{head}{where} from {when}")


def _posts_words(result, is_error):
    if not _answered(result):
        return ""
    found = result.get("found")
    if not isinstance(found, int):
        return ""
    return f"Found {_count_words(found)}." if found else "There were no posts in that part of the project."


def _status_title(fields):
    return f'Checking how "{_named(fields.get("project_id"))}" is doing'


def _status_words(result, is_error):
    if not _answered(result):
        return ""
    state, read = str(result.get("state") or ""), result.get("posts_read")
    if not state:
        return ""
    line = "This project did not finish." if state == "not finished" else f"It is {state}."
    return line + (f" We read {_count_words(read)}." if isinstance(read, int) and read else "")


def _project_facts(fields):
    return [{"label": "Project", "value": _named(fields.get("project_id"))}]


WORDING = {
    "list_projects": (_projects_title, _projects_words, None),
    "list_charts": (_charts_title, _charts_words, _project_facts),
    "get_chart": (_chart_title_words, _chart_words,
                  lambda fields: [*_project_facts(fields),
                                  {"label": "Chart", "value": _chart_title(fields.get("chart_id"))}]),
    "get_posts": (_posts_title, _posts_words,
                  lambda fields: [*_project_facts(fields),
                                  {"label": "Days", "value": _span_words(fields.get("date_from"), fields.get("date_to"))},
                                  {"label": "Group", "value": _label_words(fields.get("group")) or "every group"},
                                  {"label": "Sorted by", "value": str(fields.get("sort") or "most liked")}]),
    "project_status": (_status_title, _status_words, _project_facts),
}

for _tool, (_title, _outcome, _facts) in WORDING.items():
    steps.register_tool(_tool, _title, _outcome, _facts)
