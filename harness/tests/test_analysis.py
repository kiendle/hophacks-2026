# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.11,<4", "mcp>=2", "duckdb>=1.4,<2", "pyarrow>=20", "matplotlib>=3.9", "pytz"]
# ///
"""Run: python -m uv run harness/tests/test_analysis.py   (HARNESS_TEST_STEPS=1 skips the model turn)

The analysis stream, end to end: the tools against a tiny project built here from scratch and
against the two real demo projects, the card the page draws, the wording a person reads, the PNG
route on a real bridge, and one real Claude Code turn asking why the feeling about Anthropic dropped.

The bridge is started the way it is really started, with uv, whose environment has no matplotlib, so
the PNG route is proved on the path that has to work tonight and not only on this test's own
interpreter.
"""
import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
PORT = 5198
BASE = f"http://127.0.0.1:{PORT}"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "demo"))
os.environ.setdefault("HARNESS_REPO", str(REPO))
sys.stdout.reconfigure(errors="replace")  # the Windows console is cp1252

import analysis_api  # noqa: E402
import analysis_tools as at  # noqa: E402
import build_demo_project as builder  # noqa: E402
import charts  # noqa: E402
import steps  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

OUTCOMES = []
TEMP = Path(tempfile.mkdtemp(prefix="signal-analysis-"))
BANNED = "‒–—―·•‣▪・←→↔⇒⇨➡;"
WORDS = ("json", "sentiment", "parquet", "utc", "keyword", "null", "none", "mcp", "tool", "id")
INSIDE = ("_", "chart_id", "project_id", "mcp__")  # a field name never reaches a reader
STEPS = {step for step in (os.environ.get("HARNESS_TEST_STEPS") or "1,2").split(",")}
SHOWCASE = "Why did the feeling about Anthropic drop around Sep 9?"
PNG_URL = re.compile(r"^/api/projects/[a-z0-9_-]{1,60}/charts/[a-z0-9_-]{1,60}\.png$")
TINY = "tiny-project"


def check(name, ok, detail=""):
    OUTCOMES.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""), flush=True)  # ASCII: the console is cp1252


def punctuation_ok(text):
    value = str(text)
    return not any(mark in value for mark in BANNED) and not re.search(r"\S +- +\S", value)


def plain_enough(text):
    """Every word we write for a reader: no symbol punctuation, no field names, no jargon."""
    low = str(text).lower()
    if not punctuation_ok(text) or any(part in low for part in INSIDE):
        return False
    return not any(re.search(rf"\b{word}\b", low) for word in WORDS)


def speaks_plainly(text):
    """The model's own answer: our punctuation rule, and nothing out of the machine room in it."""
    low = str(text).lower()
    return punctuation_ok(text) and not any(part in low for part in ("json", "parquet", "chart_id", "project_id", "mcp__", ".py"))


# ------------------------------------------------------------------ a tiny project of our own
def tiny_project():
    """A project built here, so the tools are proved on data this file wrote, not only on the demos.

    Three days, two groups, one very popular negative post on the middle day: the same shape as the
    real finding, small enough to assert on exactly.
    """
    directory = TEMP / "projects" / TINY
    directory.mkdir(parents=True, exist_ok=True)
    spec = {
        "spec_version": 1, "name": "Tiny test project",
        "observation": {"intent": "A test.", "questions_to_answer": ["Did it move?"],
                        "source": "twitter_firehose", "window": {"from": "2026-09-06", "to": "2026-09-09"}},
        "filter": {"any_terms": ["testword"], "languages": ["en"], "min_likes": 0, "congress": None},
        "sampling": {"max_posts": 10, "strategy": "stratified_by_day", "seed": 7},
        "classification": [
            {"name": "relevant", "type": "noul", "instructions": "Is it about the test?", "options": None},
            {"name": "group", "type": "choice", "instructions": "Which group?",
             "options": [{"name": "worry", "description": "worried"}, {"name": "calm_news", "description": "reporting"}]},
        ],
        "relevance_gate": {"question": "relevant", "min_probability": 0.5},
        "sentiment": {"type": "score", "instructions": "Rate it.", "criteria": ["a", "b", "c", "d", "e"]},
        "budget": {"max_usd": 0.01},
    }
    rows, counts = [], {}
    plan = [("2026-09-06", 4, 0.2, "worry", 3), ("2026-09-07", 4, 0.1, "calm_news", 5), ("2026-09-08", 4, -0.8, "worry", 9000)]
    for day, many, feeling, group, likes in plan:
        counts[day] = many
        for index in range(many):
            rows.append({
                "id": f"{day}-{index}",
                "created_at": datetime.fromisoformat(f"{day}T12:00:00").replace(tzinfo=timezone.utc),
                "day": date.fromisoformat(day),
                "body": f"testword post {index} on {day}", "lang": "en",
                "like_count": likes if index == 0 else 1, "retweet_count": 0, "views_count": 10,
                "relevant_p": 0.9, "group": group, "group_confidence": 0.8, "group_probs": None,
                "sentiment_raw": 2.0, "sentiment": feeling, "sentiment_confidence": 0.7, "scored": True})
    builder.write_posts(directory / "posts.parquet", rows, ["group"])
    (directory / "spec.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    (directory / "run.json").write_text(json.dumps({
        "project_id": TINY, "status": "READY", "n_matched": sum(counts.values()), "n_scored": len(rows),
        "n_failed": 0, "matched_by_day": counts,
        "sampled_fraction_by_day": {day: 1.0 for day in counts}}, indent=2), encoding="utf-8")
    charts.build_all(directory, REPO)
    return directory.parent


def on_the_fixture():
    original = at.PROJECTS
    at.PROJECTS = tiny_project()
    try:
        listed = at.list_projects()
        ids = [project["project_id"] for project in listed.get("projects", [])]
        about = (listed.get("projects") or [{}])[0].get("about", "")
        check("1a list_projects finds a project built by this test and describes it in plain words",
              ids == [TINY] and "12 posts" in about and "Sep 6 to Sep 8" in about and plain_enough(about), about)
        found = at.list_charts(TINY)
        titles = [chart["title"] for chart in found.get("charts", [])]
        check("1b list_charts gives a plain title for each chart, and no chart is named after a field",
              len(titles) == 5 and all(plain_enough(title) for title in titles), " | ".join(titles))
        chart = at.get_chart(TINY, "sentiment_daily")
        card = chart.get("_card") or {}
        check("1c get_chart carries the numbers AND the card the page draws",
              chart["series"][0]["points"][2]["y"] < -0.5 and card["kind"] == "chart"
              and PNG_URL.match(card["png_url"] or "") and card["caption"] == "Feeling dropped the most on Sep 8."
              and 1 <= len(card["points"]) <= 3,
              f'{card["caption"]} / {card["png_url"]} / {card["points"]}')
        posts = at.get_posts(TINY, "2026-09-08", "2026-09-09", sort="most liked", limit=2)
        first = (posts.get("posts") or [{}])[0]
        check("1d get_posts returns plain field names and the most liked post first",
              set(first) == {"id", "day", "likes", "group", "feeling", "text"} and first["likes"] == 9000
              and first["day"] == "2026-09-08" and posts["days"] == "Sep 8", json.dumps(first)[:200])
        check("1e a group written the way a person would say it still matches",
              at.get_posts(TINY, group="calm news")["found"] == 4
              and at.get_posts(TINY, group="calm_news")["found"] == 4
              and at.get_posts(TINY, group="worry")["found"] == 8,
              "calm news, calm_news and worry")
        status = at.project_status(TINY)
        check("1f project_status says plainly that it is finished",
              status["state"] == "finished" and status["posts_read"] == 12 and status["groups"] == ["worry", "calm news"],
              json.dumps(status))
    finally:
        at.PROJECTS = original


# ------------------------------------------------------------------ the real demo projects
def on_the_demos():
    listed = at.list_projects()
    ids = [project["project_id"] for project in listed.get("projects", [])]
    check("2a the real demo projects are both offered", "demo-anthropic" in ids and "demo-playstation" in ids, ", ".join(ids))
    chart = at.get_chart("demo-anthropic", "sentiment_daily")
    points = {point["x"]: point for point in chart["series"][0]["points"]}
    change = next(a for a in chart["annotations"] if a["kind"] == "largest_change")
    check("2b the Anthropic project shows the drop on Sep 9 that earlier work found",
          change["x"] == "2026-09-09" and change["delta"] < -0.5 and points["2026-09-09"]["y"] < -0.6
          and points["2026-09-08"]["y"] > 0 and points["2026-09-09"]["n"] > 300,
          " ".join(f'{day[5:]}={point["y"]:+.3f}/n={point["n"]}' for day, point in sorted(points.items())))
    card = chart["_card"]
    check("2c its card says the drop in one plain sentence",
          card["caption"] == "Feeling dropped the most on Sep 9." and card["title"] == "How the feeling changed each day"
          and any(line["label"] == "Sep 9" and line["value"] == "minus 0.63" for line in card["points"]),
          f'{card["caption"]} / {card["points"]}')
    posts = at.get_posts("demo-anthropic", "2026-09-09", "2026-09-10", sort="most liked", limit=3)
    top = (posts.get("posts") or [{}])[0]
    check("2d the most liked post of that day is the one that carried it",
          top["likes"] > 500_000 and top["feeling"] < -0.5 and "resigned" in top["text"].lower(),
          f'{top["likes"]:,} likes, feeling {top["feeling"]}, {top["text"][:80]}')
    other = at.get_chart("demo-playstation", "totals__stance")["_card"]
    check("2e a group written with an underscore in the spec is still read out plainly",
          "_" not in other["caption"] and all("_" not in line["label"] for line in other["points"]),
          f'{other["caption"]} / {[line["label"] for line in other["points"]]}')
    return points


def cards_read_plainly():
    bad = []
    for project_id in ("demo-anthropic", "demo-playstation"):
        for chart in charts.list_charts(at.PROJECTS / project_id):
            card = at.get_chart(project_id, chart["chart_id"])["_card"]
            texts = [card["title"], card["caption"], *[f'{line["label"]} {line["value"]} {line["note"]}' for line in card["points"]]]
            if not all(plain_enough(text) for text in texts) or not card["caption"].endswith("."):
                bad.append(f'{project_id}/{chart["chart_id"]}: {card["caption"]}')
            if not PNG_URL.match(card["png_url"] or "") or len(card["points"]) > 3:
                bad.append(f'{project_id}/{chart["chart_id"]}: {card["png_url"]}')
    check("3a every caption and every line on every card is one plain sentence with no jargon", not bad, " | ".join(bad)[:300])


def posts_and_limits():
    over = at.get_posts("demo-anthropic", limit=999)
    check("4a a page of posts is capped at 50 whatever is asked for",
          len(over["posts"]) == 50 and over["limit"] == 50, f'{len(over["posts"])} posts')
    sorted_posts = {}
    for sort in ("most liked", "random", "most negative", "most positive", "least certain"):
        answer = at.get_posts("demo-anthropic", "2026-09-09", "2026-09-10", sort=sort, limit=5)
        sorted_posts[sort] = [post["id"] for post in answer.get("posts", [])]
    likes = [post["likes"] for post in at.get_posts("demo-anthropic", "2026-09-09", "2026-09-10", sort="most liked", limit=5)["posts"]]
    feelings = [post["feeling"] for post in at.get_posts("demo-anthropic", "2026-09-09", "2026-09-10", sort="most negative", limit=5)["posts"]]
    check("4b every sort works and sorts what it says",
          all(len(ids) == 5 for ids in sorted_posts.values()) and likes == sorted(likes, reverse=True)
          and feelings == sorted(feelings) and len(set(map(tuple, sorted_posts.values()))) > 1,
          f"likes {likes}, feelings {feelings}")
    check("4c a sort we do not have comes back as advice, not a crash",
          at.get_posts("demo-anthropic", sort="funniest")["error"]["code"] == "bad_sort"
          and "most liked" in at.get_posts("demo-anthropic", sort="funniest")["error"]["hint"],
          at.get_posts("demo-anthropic", sort="funniest")["error"]["message"])
    check("4d a day that is not a date is refused with a plain hint",
          at.get_posts("demo-anthropic", "yesterday")["error"]["code"] == "bad_request",
          at.get_posts("demo-anthropic", "yesterday")["error"]["message"])
    check("4e an unknown group names the groups that exist",
          at.get_posts("demo-anthropic", group="angry")["error"]["code"] == "unknown_group"
          and "jokes" in at.get_posts("demo-anthropic", group="angry")["error"]["hint"],
          at.get_posts("demo-anthropic", group="angry")["error"]["hint"])


def ids_are_checked():
    outside = []
    for project_id, chart_id in (("../demo-anthropic", "sentiment_daily"), ("demo-anthropic", "../../../secret"),
                                 ("demo anthropic", "sentiment_daily"), ("demo-anthropic", "sentiment_daily; rm -rf"),
                                 ("DEMO-ANTHROPIC", "sentiment_daily"), ("", "sentiment_daily"),
                                 ("demo-anthropic", "..\\..\\windows\\win"), ("demo-anthropic", "")):
        answer = at.get_chart(project_id, chart_id)
        if "error" not in answer:
            outside.append(f"{project_id}/{chart_id} was served")
        try:
            analysis_api._chart_json(project_id, chart_id)
            outside.append(f"{project_id}/{chart_id} built a path")
        except Exception as error:  # noqa: BLE001  (anything but a served path is fine)
            if type(error).__name__ != "PipelineError":
                outside.append(f"{project_id}/{chart_id} raised {type(error).__name__}")
    check("5a an id with a path, a space, a capital or a semicolon in it is refused before any path is built",
          not outside, " | ".join(outside)[:300])
    before = {path for path in analysis_api.CACHE.rglob("*")} if analysis_api.CACHE.exists() else set()
    at.get_chart("demo-anthropic", "../../../etc/passwd")
    after = {path for path in analysis_api.CACHE.rglob("*")} if analysis_api.CACHE.exists() else set()
    stray = [str(path) for pattern in ("*passwd*", "*secret*", "*win.ini*") for path in REPO.rglob(pattern)]
    check("5b a refused id writes nothing, anywhere", before == after and not stray, str(stray)[:200])


def drawing():
    cached = analysis_api._cached("demo-anthropic", "sentiment_daily")
    cached.unlink(missing_ok=True)
    started = time.monotonic()
    body = analysis_api._bytes("demo-anthropic", "sentiment_daily")
    drawn = time.monotonic() - started
    check("6a a chart is drawn into the cache as a real PNG",
          body[:8] == b"\x89PNG\r\n\x1a\n" and len(body) > 5_000 and cached.exists(),
          f"{len(body):,} bytes in {drawn:.1f}s")
    again = time.monotonic()
    same = analysis_api._bytes("demo-anthropic", "sentiment_daily")
    check("6b the second read comes from the cache", same == body and time.monotonic() - again < drawn,
          f"{time.monotonic() - again:.2f}s against {drawn:.1f}s")
    source = analysis_api._chart_json("demo-anthropic", "sentiment_daily")
    os.utime(source, None)  # the numbers were rebuilt: the old picture must not be served
    check("6c a rebuilt chart is drawn again", not analysis_api._fresh(cached, source)
          and analysis_api._bytes("demo-anthropic", "sentiment_daily")[:8] == b"\x89PNG\r\n\x1a\n",
          "the cached picture is older than its numbers")


async def two_at_once():
    for chart_id in ("volume_daily", "totals__group"):
        analysis_api._cached("demo-anthropic", chart_id).unlink(missing_ok=True)
    first, second = await asyncio.gather(
        asyncio.to_thread(analysis_api._bytes, "demo-anthropic", "volume_daily"),
        asyncio.to_thread(analysis_api._bytes, "demo-anthropic", "totals__group"))
    leftovers = [path.name for path in (analysis_api.CACHE / "demo-anthropic").glob("*.tmp*")]
    check("6d two charts drawn at the same time both come out whole, and no half file is left behind",
          first[:8] == second[:8] == b"\x89PNG\r\n\x1a\n" and len(first) > 5_000 and len(second) > 5_000 and not leftovers,
          f"{len(first):,} and {len(second):,} bytes, leftovers {leftovers}")


# ------------------------------------------------------------------ the tool surface and the wording
async def tool_surface():
    server = MCPServer("test-analysis")
    at.register(server)
    listed = {tool.name: tool for tool in await server.list_tools()}
    wanted = {"list_projects", "list_charts", "get_chart", "get_posts", "project_status"}
    first = [(name, list((tool.input_schema.get("properties") or {}))[:1], tool.input_schema.get("required") or [])
             for name, tool in listed.items()]
    check("7a every tool is registered with reason as its first and required argument",
          wanted <= set(listed) and "run_sql" not in listed
          and all(props == ["reason"] and "reason" in required for _, props, required in first),
          " | ".join(f"{name}{props}" for name, props, _ in first))
    called = at.with_reason(at.list_projects)("")
    check("7b a call with no reason does nothing and says what is missing",
          called["error"]["code"] == "no_reason" and at.with_reason(at.list_projects)("Checking what we have")["projects"],
          called["error"]["message"])


def wording():
    chart = at.get_chart("demo-anthropic", "sentiment_daily")
    posts = at.get_posts("demo-anthropic", "2026-09-09", "2026-09-10", sort="most liked", limit=20)
    lines = {
        "get_chart title": steps.title("mcp__harness__get_chart", {"project_id": "demo-anthropic", "chart_id": "sentiment_daily"}),
        "get_chart outcome": steps.outcome("mcp__harness__get_chart", chart),
        "get_posts title": steps.title("mcp__harness__get_posts", {"project_id": "demo-anthropic", "date_from": "2026-09-09",
                                                                   "date_to": "2026-09-10", "sort": "most liked", "limit": 20}),
        "get_posts outcome": steps.outcome("mcp__harness__get_posts", posts),
        "list_projects title": steps.title("mcp__harness__list_projects", {}),
        "list_projects outcome": steps.outcome("mcp__harness__list_projects", at.list_projects()),
        "project_status outcome": steps.outcome("mcp__harness__project_status", at.project_status("demo-anthropic")),
    }
    check("8a the activity list describes these steps the way the brief asks",
          lines["get_chart title"] == 'Looking at the feeling chart for "Anthropic and AI safety"'
          and lines["get_chart outcome"] == "Feeling dropped the most on Sep 9."
          and lines["get_posts title"] == "Reading 20 of the most liked posts from Sep 9"
          and lines["get_posts outcome"] == "Found 20 posts.",
          " | ".join(f"{name}: {text}" for name, text in lines.items())[:400])
    check("8b no step title or result carries a symbol or a field name",
          all(plain_enough(text) for text in lines.values()), " | ".join(t for t in lines.values() if not plain_enough(t))[:200])
    failed = steps.outcome("mcp__harness__get_chart", {"error": {"code": "unknown_chart", "message": "There is no chart called 'x'."}}, True)
    check("8c a failed step still says one short line", failed.startswith("That did not work"), failed)
    facts = steps.facts("mcp__harness__get_posts", {"project_id": "demo-anthropic", "date_from": "2026-09-09",
                                                    "date_to": "2026-09-10", "group": "jokes", "sort": "random"})
    check("8d the details panel gets plain labelled lines",
          {line["label"]: line["value"] for line in facts} ==
          {"Project": "Anthropic and AI safety", "Days": "Sep 9", "Group": "jokes", "Sorted by": "random"}, json.dumps(facts))


def browser():
    source = (ROOT / "web/analysis.js").read_text(encoding="utf-8")
    style = (ROOT / "web/analysis.css").read_text(encoding="utf-8")
    imported = re.search(r"import \{([^}]+)\} from '\./chat\.js'", source)
    names = sorted(name.strip() for name in (imported.group(1) if imported else "").split(","))
    check("9a the page module builds on the contract and nothing else",
          names == ["appendCard", "el", "onEvent", "plainText"] and "innerHTML" not in source
          and "card.kind !== 'chart'" in source, ", ".join(names))
    check("9b it only puts one of our own chart addresses on an image",
          "PNG.test(card.png_url)" in source and r"^\/api\/projects\/[a-z0-9_-]{1,60}\/charts\/[a-z0-9_-]{1,60}\.png$" in source,
          "the address is checked before it becomes a src")
    check("9c the card shows the picture, the sentence and the three lines",
          all(part in source for part in ("chart-picture", "chart-caption", "chart-points", "alt: caption"))
          and all(part in style for part in (".chart-picture", ".chart-caption", ".chart-point")),
          f"{len(source)} characters of module, {len(style)} of style")
    check("9d the stylesheet is linked by the module, because the page forbids inline styles",
          "document.head.append(el('link', { rel: 'stylesheet', href: STYLESHEET }))" in source, "one link element")


# ------------------------------------------------------------------ a careless caller, a half written project
UNSEEN = re.compile(r"[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u00ad\u200b-\u200f\u202a-\u202e"
                    r"\u2060-\u2064\u2066-\u2069\ufeff]")
HOSTILE = "<script>x</script> \u202eback\u202c \u2014 loud; long " + "y" * 300


def junk_arguments():
    """Anything at all can arrive in a tool call, and a tool still has to answer with a sentence."""
    junk = {
        "a sort that is a list": lambda: at.get_posts("demo-anthropic", sort=["most liked"]),
        "a sort that is a dictionary": lambda: at.get_posts("demo-anthropic", sort={"a": 1}),
        "a limit of infinity": lambda: at.get_posts("demo-anthropic", limit=float("inf")),
        "a limit that is not a number": lambda: at.get_posts("demo-anthropic", limit="lots"),
        "a project that is a dictionary": lambda: at.get_posts({"a": 1}),
        "a day of ten thousand characters": lambda: at.get_posts("demo-anthropic", "9" * 10_000),
        "a chart that is a list": lambda: at.get_chart("demo-anthropic", ["sentiment_daily"]),
        "a project name of ten thousand characters": lambda: at.get_chart("a" * 10_000, "sentiment_daily"),
        "a chart asked for by nothing at all": lambda: at.get_chart("demo-anthropic", None),
    }
    broke, shouting = [], []
    for name, call in junk.items():
        try:
            answer = call()
        except Exception as error:  # noqa: BLE001  (that is the whole point of the check)
            broke.append(f"{name} raised {type(error).__name__}")
            continue
        message = ((answer.get("error") or {}) if isinstance(answer, dict) else {}).get("message") or ""
        if not isinstance(answer, dict):
            broke.append(f"{name} did not answer with a result")
        if len(message) > 200:  # a refusal is read on a step row, so it can never be a wall of text
            shouting.append(f"{name}: {len(message)} characters")
    check("12a junk in a tool call comes back as a sentence, never as a crash", not broke, " | ".join(broke))
    check("12b and no refusal is longer than a line a person could read", not shouting, " | ".join(shouting))
    blank = at.get_posts("demo-anthropic", group="", sort=None, limit=3)
    check("12c asking with no group and no sort means every group and the most liked posts",
          blank["found"] == 3 and blank["sort"] == "most liked" and blank["group"] is None
          and blank["posts"][0]["likes"] > 500_000, f'{blank["sort"]}, {blank["found"]} posts')
    marked = at.get_posts("demo-anthropic", "\u202e2026-09-09", "2026-09-10", limit=2)
    check("12d an invisible mark in a day does not make the days we report disagree with the posts we read",
          marked["days"] == "Sep 9" and {post["day"] for post in marked["posts"]} == {"2026-09-09"},
          f'{marked["days"]}, {sorted({post["day"] for post in marked["posts"]})}')


def title_matches_the_page():
    """The row says "Reading 50" and then "Found 50 posts.": those two numbers may never disagree."""
    rows, bad = [], []
    for asked in (999, None, -5, 7):
        fields = {"project_id": "demo-anthropic", "sort": "most liked"}
        if asked is not None:
            fields["limit"] = asked
        title = steps.title("mcp__harness__get_posts", fields)
        answer = at.get_posts("demo-anthropic") if asked is None else at.get_posts("demo-anthropic", limit=asked)
        said, many = steps.outcome("mcp__harness__get_posts", answer), len(answer["posts"])
        rows.append((asked, title, said, many))
        if f"Reading {many} " not in title or f"Found {many:,} post" not in said:
            bad.append(f"{asked}: {title} / {said}")
    check("12e the step title promises the number of posts that really comes back", not bad,
          " | ".join(f"asked {asked}, {title.split(' of ')[0]}, {said}" for asked, title, said, _ in rows))


def typed_by_a_person():
    """A project's name and its groups were typed by someone. They reach a card, a row and a title."""
    directory = TEMP / "projects" / "hostile"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "spec.json").write_text(json.dumps({
        "spec_version": 1, "name": HOSTILE,
        "observation": {"source": "twitter_firehose", "window": {"from": "2026-09-06", "to": "2026-09-08"}},
        "filter": {"any_terms": ["x"], "languages": ["en"]},
        "classification": [{"name": "group", "type": "choice", "instructions": "?",
                            "options": [{"name": "\u202ebad_group", "description": "d"}]}]}), encoding="utf-8")
    original = at.PROJECTS
    at.PROJECTS = directory.parent
    try:
        described = at.list_projects()["projects"][0]
        name, group = described["name"], described["groups"][0]
        title = steps.title("mcp__harness__get_chart", {"project_id": "hostile", "chart_id": "sentiment_daily"})
        check("12f a name a person typed cannot carry an invisible mark, run on forever or bring a symbol in",
              not UNSEEN.search(name) and not UNSEEN.search(group) and not UNSEEN.search(title)
              and len(name) <= 80 and len(group) <= 60 and punctuation_ok(name) and punctuation_ok(group),
              f"{len(name)} characters, {name[:48]!r}, group {group!r}")
    finally:
        at.PROJECTS = original
    page = (ROOT / "web/analysis.js").read_text(encoding="utf-8")
    check("12g the page takes the same marks out of every card line before it draws one",
          "UNSEEN" in page and "0x202a, 0x202e" in page and "value.replace(UNSEEN, '')" in page,
          "analysis.js cleans a label, a caption and a title")
    # Written after an editing tool turned these very escapes into the real characters: an invisible
    # character inside our own source is unreadable, unreviewable and survives every review.
    mine = ["analysis_tools.py", "analysis_api.py", "web/analysis.js", "web/analysis.css",
            "prompts/analysis.md", "tests/test_analysis.py"]
    carrying = [name for name in mine
                if UNSEEN.search((ROOT / name).read_text(encoding="utf-8").replace("\r", ""))]
    check("12l none of our own files carries an invisible character in its source", not carrying, ", ".join(carrying))


def half_written():
    """A build that stopped halfway leaves files behind. Reading them is a sentence, not a stack trace."""
    directory = TEMP / "projects" / "half-written"
    (directory / "charts").mkdir(parents=True, exist_ok=True)
    (directory / "spec.json").write_text('{"name": "Half written"', encoding="utf-8")  # the writer stopped here
    (directory / "charts" / "sentiment_daily.json").write_text("not a chart", encoding="utf-8")
    original = at.PROJECTS
    at.PROJECTS = directory.parent
    try:
        answers = {
            "get_posts": at.get_posts("half-written"),
            "project_status": at.project_status("half-written"),
            "list_charts": at.list_charts("half-written"),
            "get_chart": at.get_chart("half-written", "sentiment_daily"),
        }
        messages = [(answer.get("error") or {}).get("message") or "" for answer in answers.values()]
        check("12h a project whose files are half written answers in words, not with a crash",
              all("error" in answer for answer in answers.values()) and all(plain_enough(text) for text in messages),
              " | ".join(sorted(set(messages))))
        check("12i and the reason, which names a file, stays in the log",
              not any(mark in text for text in messages for mark in (".json", "\\", "/", "line 1", "column")),
              " | ".join(sorted(set(messages)))[:200])
    finally:
        at.PROJECTS = original


def a_run_that_stopped():
    directory = TEMP / "projects" / "stopped"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copy(at.PROJECTS / "demo-anthropic" / "spec.json", directory / "spec.json")
    (directory / "run.json").write_text(json.dumps({
        "project_id": "stopped", "status": "FAILED", "n_matched": 10, "n_scored": 0, "n_failed": 10,
        "failure_reasons": {"http_401": 10},
        "error": {"code": "boom", "message": r"C:\Users\someone\secret\key.txt could not be read"}}), encoding="utf-8")
    original = at.PROJECTS
    at.PROJECTS = directory.parent
    try:
        status = at.project_status("stopped")
        said = steps.outcome("mcp__harness__project_status", status)
        check("12j a project that did not finish says so in one plain sentence, with the machine room left out",
              status["state"] == "not finished" and said == "This project did not finish."
              and plain_enough(status["problem"]) and "key.txt" not in json.dumps(status)
              and "http_401" not in json.dumps(status) and "stopped" not in status["problem"],
              f'{said} / {status["problem"]}')
    finally:
        at.PROJECTS = original


async def one_picture_many_askers():
    """A page that asks for the same picture eight times draws it once, not eight times."""
    analysis_api._cached("demo-anthropic", "sentiment_daily").unlink(missing_ok=True)
    drawn, real = [], analysis_api.render

    def counted(source, out):
        drawn.append(out.name)
        return real(source, out)

    analysis_api.render = counted
    try:
        started = time.monotonic()
        bodies = await asyncio.gather(*[asyncio.to_thread(analysis_api._bytes, "demo-anthropic", "sentiment_daily")
                                        for _ in range(8)])
    finally:
        analysis_api.render = real
    leftovers = [path.name for path in (analysis_api.CACHE / "demo-anthropic").glob("*.tmp*")]
    check("12k eight askers for one picture at the same time all get it, and it is drawn once",
          len(drawn) == 1 and len({len(body) for body in bodies}) == 1 and not leftovers
          and all(body[:8] == b"\x89PNG\r\n\x1a\n" for body in bodies),
          f"{len(bodies)} answers, drawn {len(drawn)} time, {len(bodies[0]):,} bytes each, {time.monotonic() - started:.1f}s")


# ------------------------------------------------------------------ the real server, the real turn
async def sse(client, path, payload, timeout=600):
    events = []
    async with client.post(BASE + path, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
        if response.status != 200:
            return [{"type": "http_error", "status": response.status, "text": await response.text()}]
        async for raw in response.content:
            line = raw.decode("utf-8").strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    return events


async def wait_for_server(client, seconds=90):
    for _ in range(seconds * 2):
        try:
            async with client.get(BASE + "/") as response:
                if response.status < 500:
                    return True
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass
        await asyncio.sleep(0.5)
    return False


async def through_the_server(client):
    analysis_api._cached("demo-anthropic", "sentiment_daily").unlink(missing_ok=True)  # cold: the server draws it itself
    started = time.monotonic()
    async with client.get(BASE + "/api/projects/demo-anthropic/charts/sentiment_daily.png") as response:
        body, kind, cache = await response.read(), response.content_type, response.headers.get("Cache-Control")
        status = response.status
    check("10a the chart route answers with a real PNG on the server that really runs",
          status == 200 and kind == "image/png" and cache == "no-store" and body[:8] == b"\x89PNG\r\n\x1a\n" and len(body) > 5_000,
          f"{status} {kind} {cache} {len(body):,} bytes in {time.monotonic() - started:.1f}s")
    for chart_id in ("volume_daily", "totals__group"):
        analysis_api._cached("demo-anthropic", chart_id).unlink(missing_ok=True)

    async def png(chart_id):
        async with client.get(BASE + f"/api/projects/demo-anthropic/charts/{chart_id}.png") as response:
            return response.status, await response.read()
    pair = await asyncio.gather(png("volume_daily"), png("totals__group"))
    check("10b two pictures drawn at the same time both arrive whole",
          all(status == 200 and body[:8] == b"\x89PNG\r\n\x1a\n" and len(body) > 5_000 for status, body in pair),
          " and ".join(f"{status} {len(body):,} bytes" for status, body in pair))
    misses = []
    for path in ("/api/projects/demo-anthropic/charts/nope.png", "/api/projects/nope/charts/sentiment_daily.png",
                 "/api/projects/demo-anthropic/charts/../../../secret.png", "/api/projects/DEMO/charts/sentiment_daily.png"):
        async with client.get(BASE + path) as response:
            if response.status != 404:
                misses.append(f"{path} answered {response.status}")
    check("10c an address that names nothing we have is a plain 404", not misses, " | ".join(misses))
    served = {}
    for name in ("analysis.js", "analysis.css"):
        async with client.get(f"{BASE}/{name}") as response:
            served[name] = (response.status, response.content_type, len(await response.text()),
                            "img-src 'self'" in (response.headers.get("Content-Security-Policy") or ""))
    check("10d the page's own two files are served with the page's own headers",
          served["analysis.js"][:2] == (200, "text/javascript") and served["analysis.css"][:2] == (200, "text/css")
          and all(row[2] > 500 and row[3] for row in served.values()), json.dumps(served))


async def the_real_turn(client):
    async with client.post(BASE + "/api/sessions", json={}) as response:
        session_id = (await response.json())["session_id"]
    started = time.monotonic()
    events = await sse(client, f"/api/sessions/{session_id}/messages", {"text": SHOWCASE})
    titles = [event["title"] for event in events if event.get("type") == "step" and event.get("phase") == "start"]
    whys = [event["why"] for event in events if event.get("type") == "step" and event.get("phase") == "start"]
    ends = [event["outcome"] for event in events if event.get("type") == "step" and event.get("phase") == "end"]
    cards = [event["card"] for event in events if event.get("type") == "card"]
    answer = " ".join(event["text"] for event in events if event.get("type") == "message")
    print("\n--- one real turn -------------------------------------------------", flush=True)
    print(f"asked: {SHOWCASE}", flush=True)
    for index, (title, why) in enumerate(zip(titles, whys), 1):
        print(f"  step {index}: {title}\n          why: {why}\n          result: {ends[index - 1] if index <= len(ends) else '(open)'}", flush=True)
    for card in cards:
        print(f"  card: {json.dumps(card, ensure_ascii=False)}", flush=True)
    print(f"answer ({len(answer)} characters, {time.monotonic() - started:.0f}s):\n{answer[:600]}", flush=True)
    print("-------------------------------------------------------------------\n", flush=True)
    check("11a the real turn looked at the project and read its posts",
          any("chart" in title.lower() or "feeling" in title.lower() for title in titles)
          and any("post" in title.lower() for title in titles) and len(ends) == len(titles),
          " | ".join(titles)[:300])
    check("11b the page was given a chart card to draw",
          any(card.get("kind") == "chart" and PNG_URL.match(card.get("png_url") or "") and card.get("caption") for card in cards),
          json.dumps([card.get("caption") for card in cards], ensure_ascii=False)[:200])
    # The model answers in the person's own language and sometimes picks another one, and it rounds
    # 801,270 to "over 800,000" as often as not, so both are matched in any spelling and any grouping:
    # what matters is that it named the day and cited the post that carried it.
    check("11c the answer names the day, cites the post that carried it, and stays plain",
          re.search(r"(sep\w*\.?\s*9|9\s*sep\w*)", answer, re.I) and re.search(r"\b80[01]([ ,.]?\d{3})?\b", answer)
          and len(answer) > 200 and speaks_plainly(answer),
          answer[:200])


def port_free(port):
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def stop(server):
    """uv runs the server as its own child, so terminating uv alone leaves a port held for the next run."""
    if server.poll() is None and os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(server.pid)], capture_output=True)
    elif server.poll() is None:
        server.terminate()
    try:
        server.wait(timeout=20)
    except subprocess.TimeoutExpired:
        server.kill()


async def with_bridge():
    """The bridge as it is really started: uv, whose environment has no matplotlib of its own."""
    environment = {**os.environ, "PORT": str(PORT), "HARNESS_REPO": str(REPO)}
    log = TEMP / "bridge.log"
    uv = shutil.which("uv")
    if not uv or not port_free(PORT):
        check("10 the bridge starts on its own port with the analysis routes", False,
              "uv is not on the path" if not uv else f"something else is already listening on {PORT}")
        return
    with log.open("w", encoding="utf-8") as handle:
        server = subprocess.Popen([uv, "run", str(ROOT / "bridge.py")],
                                  cwd=str(REPO), env=environment, stdout=handle, stderr=subprocess.STDOUT)
        try:
            async with aiohttp.ClientSession(headers={"Origin": BASE}, timeout=aiohttp.ClientTimeout(total=60)) as client:
                if not await wait_for_server(client):
                    check("10 the bridge starts on its own port", False, log.read_text(encoding="utf-8")[-300:])
                    return
                check("10 the bridge starts on its own port with the analysis routes",
                      "analysis_api.py" in log.read_text(encoding="utf-8"), f"port {PORT}")
                await through_the_server(client)
                if "2" in STEPS:
                    await the_real_turn(client)
        finally:
            stop(server)
    check("10e the test leaves nothing running on its port", port_free(PORT), f"port {PORT} is free again")


async def main():
    on_the_fixture()
    feelings = on_the_demos()
    cards_read_plainly()
    posts_and_limits()
    ids_are_checked()
    drawing()
    await two_at_once()
    await tool_surface()
    wording()
    browser()
    junk_arguments()
    title_matches_the_page()
    typed_by_a_person()
    half_written()
    a_run_that_stopped()
    await one_picture_many_askers()
    await with_bridge()
    print("\nthe Anthropic project, feeling per day (likes weighted, plain average, posts):", flush=True)
    for day, point in sorted(feelings.items()):
        print(f"  {day}  {point['y']:+.3f}  {point['y_unweighted']:+.3f}  {point['n']:4d}", flush=True)
    shutil.rmtree(TEMP, ignore_errors=True)
    print(f"\n{sum(OUTCOMES)}/{len(OUTCOMES)} checks passed", flush=True)
    return 0 if all(OUTCOMES) and OUTCOMES else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
