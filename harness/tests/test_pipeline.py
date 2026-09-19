# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2", "pyarrow>=20", "aiohttp>=3.11,<4", "pytz"]
# ///
"""Run: python -m uv run harness/tests/test_pipeline.py

Tests the Pipeline port and the build's pure parts against a synthetic project written under
the scratchpad, so nothing here depends on the real Jev build or costs a Jev call. The last
case reads the real demo project if it exists, to prove the same code serves the file layout
build_demo_project.py writes.
"""
import asyncio
import contextlib
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "demo"))

import build_demo_project as builder  # noqa: E402
import pipeline as port  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
FIXTURE_ID = "demo-playstation"
DAYS = ["2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"]
OUTCOMES = []

SPEC = {
    "spec_version": 1, "name": "synthetic fixture",
    "observation": {"intent": "fixture", "questions_to_answer": ["?"], "source": "twitter_firehose",
                    "window": {"from": DAYS[0], "to": "2026-09-12"}},
    "filter": {"any_terms": ["playstation"], "all_terms": [], "none_terms": [], "hashtags": [],
               "languages": ["en"], "min_likes": 0, "congress": None},
    "sampling": {"max_posts": 3000, "strategy": "stratified_by_day", "seed": 7},
    "classification": [
        {"name": "relevant", "type": "noul", "instructions": "About PlayStation?", "options": None},
        {"name": "stance", "type": "choice", "instructions": "Stance?", "options": [
            {"name": "critical", "description": "against"}, {"name": "supportive", "description": "for"},
            {"name": "neutral_news", "description": "neutral"}, {"name": "unclear", "description": "unclear"}]},
    ],
    "relevance_gate": {"question": "relevant", "min_probability": 0.5},
    "sentiment": {"type": "score", "instructions": "Sentiment?", "criteria": ["a", "b", "c", "d", "e"]},
    "budget": {"max_usd": 1.0},
}


def check(name, ok, detail=""):
    OUTCOMES.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""), flush=True)  # ASCII: the console is cp1252


async def case(name, body):
    try:
        check(name, True, await body() or "")
    except AssertionError as error:
        check(name, False, str(error) or "assertion failed")
    except Exception as error:  # a crash is a failure, never a skipped case
        check(name, False, f"{type(error).__name__}: {error}")


def write_fixture(directory: Path) -> Path:
    """80 scored posts + 4 unscored ones, spread over 4 days, with every field the contract names."""
    project = directory / FIXTURE_ID
    (project / "charts").mkdir(parents=True, exist_ok=True)
    stances = ["critical", "supportive", "neutral_news", "unclear"]
    rows = []
    for index in range(84):
        day = DAYS[index % 4]
        scored = index < 80
        relevant = round(0.02 + (index % 25) * 0.04, 3)  # spans 0.02..0.98, so some sit inside |p-0.5|<0.2
        raw = index % 5
        rows.append({
            "id": f"{2_000_000_000_000_000_000 + index}", "created_at": datetime.fromisoformat(f"{day}T0{index % 10}:30:00+00:00"),
            "day": date.fromisoformat(day),  # 79 is the most-liked scored post, so its long body lands on the first page
            "body": ("x" * 400) if index == 79 else f"post {index} about playstation", "lang": "en",
            "like_count": index * 7, "retweet_count": index, "views_count": index * 100,
            "relevant_p": relevant if scored else None,
            "stance": stances[index % 4] if scored else None,
            "stance_confidence": round(0.3 + (index % 7) * 0.1, 3) if scored else None,
            "stance_probs": json.dumps({name: 0.25 for name in stances}) if scored else None,
            "sentiment_raw": float(raw) if scored else None,
            "sentiment": 2 * raw / 4 - 1 if scored else None,
            "sentiment_confidence": round(0.2 + (index % 8) * 0.1, 3) if scored else None,
            "scored": scored,
        })
    schema = pa.schema([("id", pa.string()), ("created_at", pa.timestamp("us", tz="UTC")), ("day", pa.date32()),
                        ("body", pa.string()), ("lang", pa.string()), ("like_count", pa.int64()),
                        ("retweet_count", pa.int64()), ("views_count", pa.int64()), ("relevant_p", pa.float64()),
                        ("stance", pa.string()), ("stance_confidence", pa.float64()), ("stance_probs", pa.string()),
                        ("sentiment_raw", pa.float64()), ("sentiment", pa.float64()),
                        ("sentiment_confidence", pa.float64()), ("scored", pa.bool_())])
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), project / "posts.parquet")
    (project / "spec.json").write_text(json.dumps(SPEC, indent=2), encoding="utf-8")
    (project / "run.json").write_text(json.dumps({
        "project_id": FIXTURE_ID, "status": "READY", "started_ms": 1, "finished_ms": 2,
        "n_matched": 648, "n_scored": 80, "n_failed": 4, "jev_input_tokens": 12345, "jev_cost_usd": 0.0005,
        "sampled_fraction_by_day": {day: 1.0 for day in DAYS},
        "matched_by_day": {day: 21 for day in DAYS}}, indent=2), encoding="utf-8")
    (project / "charts" / "sentiment_daily.json").write_text(json.dumps({
        "chart_id": "sentiment_daily", "type": "line", "title": "Mean sentiment per day",
        "series": [{"name": "all posts", "points": [{"x": DAYS[0], "y": -0.1, "n": 20}]}]}), encoding="utf-8")
    return project


def clone(project: Path, target: Path, *, spec=None, run=None, posts=None) -> Path:
    """The fixture again with single files swapped, so a case can test another spec or a failed run."""
    copied = target / FIXTURE_ID
    shutil.copytree(project, copied)
    if spec is not None:
        (copied / "spec.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    if run is not None:
        (copied / "run.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
    if posts is not None:
        (copied / "posts.parquet").write_text(posts, encoding="utf-8")
    return target


def two_days(directory: Path, relevant: dict[str, float]) -> Path:
    """A tiny project written with the build's own writer: 3 posts a day at a given relevance."""
    rows = []
    for day, probability in relevant.items():
        for index in range(3):
            rows.append({"id": f"{day}-{index}", "created_at": datetime.fromisoformat(f"{day}T01:00:00+00:00"),
                         "day": date.fromisoformat(day), "body": "playstation", "lang": "en",
                         "like_count": index, "retweet_count": 0, "views_count": 10, "relevant_p": probability,
                         "stance": "critical", "stance_confidence": 0.5, "stance_probs": "{}",
                         "sentiment_raw": 1.0, "sentiment": -0.5, "sentiment_confidence": 0.5, "scored": True})
    directory.mkdir(parents=True, exist_ok=True)
    builder.write_posts(directory / "posts.parquet", rows, ["stance"])
    return directory / "posts.parquet"


class Clock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


async def main():
    workspace = Path(tempfile.mkdtemp(prefix="test_pipeline_", dir=os.environ.get("TEMP") or None))
    projects = workspace / "projects"
    state = workspace / "state"
    write_fixture(projects)
    clock = Clock()
    fake = port.FakePipeline(projects, state, clock=clock)

    async def progression():
        submitted = await fake.submit(SPEC, "key-a")
        assert submitted == {"project_id": FIXTURE_ID, "status": "QUEUED"}, submitted
        at_zero = await fake.status(FIXTURE_ID)
        clock.now += 2.0
        queued = await fake.status(FIXTURE_ID)
        clock.now += 6.0
        running = await fake.status(FIXTURE_ID)
        clock.now += 8.0
        later = await fake.status(FIXTURE_ID)
        clock.now += 10.0
        ready = await fake.status(FIXTURE_ID)
        assert [at_zero["status"], queued["status"], running["status"], later["status"], ready["status"]] == \
               ["QUEUED", "QUEUED", "RUNNING", "RUNNING", "READY"], [s["status"] for s in (at_zero, queued, running, later, ready)]
        assert at_zero["progress"] == 0.0 and 0 < running["progress"] < later["progress"] < 1.0, \
            (running["progress"], later["progress"])
        assert ready["progress"] == 1.0 and ready["counts"]["n_matched"] == 648 and ready["counts"]["n_scored"] == 80
        assert running["counts"]["n_scored"] is None, "counts are only reported once the run is READY"
        assert ready["error"] is None and set(ready) == {"project_id", "status", "progress", "counts", "error"}
        return (f"progress {running['progress']} then {later['progress']}, "
                f"counts {ready['counts']['n_matched']}/{ready['counts']['n_scored']}")

    async def idempotent():
        first = await fake.submit(SPEC, "key-b")
        again = await fake.submit(SPEC | {"name": "edited, same key"}, "key-b")
        other = await fake.submit(SPEC, "key-c")
        records = sorted((state / "submissions").glob("*.json"))
        assert first["project_id"] == again["project_id"] == other["project_id"] == FIXTURE_ID
        assert len(records) == 3, f"key-a, key-b and key-c only: {[p.name for p in records]}"
        keys = {json.loads(path.read_text())["idempotency_key"] for path in records}
        assert keys == {"key-a", "key-b", "key-c"}, keys
        stored = json.loads(next(p for p in records if json.loads(p.read_text())["idempotency_key"] == "key-b").read_text())
        assert stored["name"] == "synthetic fixture", "the retry must not overwrite the first record"
        try:
            await fake.submit(SPEC, "")
            raise AssertionError("an empty idempotency key must be refused")
        except port.PipelineError as error:
            assert error.code == "bad_request", error.code
        return f"{len(records)} records, every spec maps to {first['project_id']}"

    async def capped():
        page = await fake.posts(FIXTURE_ID, limit=500)
        one = await fake.posts(FIXTURE_ID, limit=0)
        assert len(page) == port.MAX_POSTS_PER_PAGE == 50, len(page)
        assert len(one) == 1, len(one)
        assert all(row["relevant_p"] is not None and row["sentiment"] is not None for row in page), \
            "only scored posts are served: the 4 unscored fixture rows must never appear"
        cut = [row for row in page if row["body_truncated"]]
        assert cut and all(len(row["body"]) == port.BODY_CHARS for row in cut), [len(row["body"]) for row in cut]
        assert all(len(row["body"]) <= port.BODY_CHARS for row in page)
        return f"asked 500, got {len(page)}; longest body {max(len(r['body']) for r in page)} chars"

    async def sorts():
        got = {}
        for sort in port.SORTS:
            got[sort] = await fake.posts(FIXTURE_ID, sort=sort, limit=10)
            assert got[sort], f"{sort} returned nothing"
        likes = [row["like_count"] for row in got["engagement"]]
        negative = [row["sentiment"] for row in got["most_negative"]]
        positive = [row["sentiment"] for row in got["most_positive"]]
        confidences = [row["sentiment_confidence"] for row in got["low_confidence"]]
        assert likes == sorted(likes, reverse=True), likes
        assert negative == sorted(negative) and negative[0] == -1.0, negative[:3]
        assert positive == sorted(positive, reverse=True) and positive[0] == 1.0, positive[:3]
        assert confidences == sorted(confidences), confidences
        assert got["random"] == await fake.posts(FIXTURE_ID, sort="random", limit=10), "random must be stable across calls"
        assert {row["id"] for row in got["random"]} != {row["id"] for row in got["engagement"]}
        try:
            await fake.posts(FIXTURE_ID, sort="cheapest")
            raise AssertionError("an unknown sort must be refused")
        except port.PipelineError as error:
            assert error.code == "bad_sort", error.code
        return f"likes {likes[:3]}, most_negative {negative[:2]}, low_confidence {confidences[:3]}"

    async def filters():
        one_day = await fake.posts(FIXTURE_ID, date_from=DAYS[2], date_to=DAYS[3], limit=50)
        two_days = await fake.posts(FIXTURE_ID, date_from=DAYS[2], limit=50)
        critical = await fake.posts(FIXTURE_ID, question="stance", label="critical", limit=50)
        yes = await fake.posts(FIXTURE_ID, question="relevant", label="yes", limit=50)
        no = await fake.posts(FIXTURE_ID, question="relevant", label="no", limit=50)
        assert one_day and {row["day"] for row in one_day} == {DAYS[2]}, {row["day"] for row in one_day}
        assert {row["day"] for row in two_days} == {DAYS[2], DAYS[3]}, "date_to is exclusive, date_from inclusive"
        assert critical and {row["stance"] for row in critical} == {"critical"}
        assert yes and all(row["relevant_p"] >= 0.5 for row in yes)
        assert no and all(row["relevant_p"] < 0.5 for row in no)
        for arguments, code in (({"question": "vibe", "label": "x"}, "unknown_question"),
                                ({"label": "critical"}, "bad_request"),
                                ({"question": "relevant", "label": "critical"}, "bad_label")):
            try:
                await fake.posts(FIXTURE_ID, **arguments)
                raise AssertionError(f"{arguments} must be refused with {code}")
            except port.PipelineError as error:
                assert error.code == code, f"{arguments} gave {error.code}, expected {code}"
        return f"{len(one_day)} on {DAYS[2]}, {len(critical)} critical, {len(yes)} relevant / {len(no)} not"

    async def low_confidence():
        unsure = await fake.posts(FIXTURE_ID, question="relevant", sort="low_confidence", limit=50)
        distances = [abs(row["relevant_p"] - 0.5) for row in unsure]
        every = await fake.posts(FIXTURE_ID, sort="low_confidence", limit=50)
        by_choice = await fake.posts(FIXTURE_ID, question="stance", sort="low_confidence", limit=10)
        assert unsure and all(distance < port.LOW_CONFIDENCE_NOUL for distance in distances), distances[:4]
        assert distances == sorted(distances), distances[:4]
        assert len(every) == 50 and [r["sentiment_confidence"] for r in every] == sorted(r["sentiment_confidence"] for r in every), \
            "with no question, low_confidence falls back to the sentiment confidence"
        assert len(unsure) < len(every), f"the noul band must exclude confident posts: {len(unsure)} vs {len(every)}"
        confidences = [row["stance_confidence"] for row in by_choice]
        assert confidences == sorted(confidences), confidences
        return f"{len(unsure)} posts inside |p-0.5|<{port.LOW_CONFIDENCE_NOUL}, nearest {distances[:3]}"

    async def charts():
        listed = await fake.list_charts(FIXTURE_ID)
        packaged = await fake.chart(FIXTURE_ID, "sentiment_daily")
        assert listed == [{"chart_id": "sentiment_daily", "title": "Mean sentiment per day", "type": "line"}], listed
        assert packaged["series"][0]["points"][0]["n"] == 20, packaged
        hint = ""
        for chart_id in ("volume_daily", "../../../spec"):  # the second one must not escape charts/
            try:
                await fake.chart(FIXTURE_ID, chart_id)
                raise AssertionError(f"chart {chart_id!r} must be refused")
            except port.PipelineError as error:
                assert error.code == "unknown_chart", (chart_id, error.code)
                hint = hint or error.hint
        assert "sentiment_daily" in hint, hint
        return f"{len(listed)} chart, unknown id says: {hint[:48]}"

    async def missing_project():
        empty = port.FakePipeline(workspace / "nothing-here", state)
        errors = {}
        for name, call in (("status", empty.status(FIXTURE_ID)), ("posts", empty.posts(FIXTURE_ID)),
                           ("charts", empty.list_charts(FIXTURE_ID)), ("chart", empty.chart(FIXTURE_ID, "x"))):
            try:
                await call
                raise AssertionError(f"{name} on a missing project must raise, not return")
            except port.PipelineError as error:
                errors[name] = error
        assert {error.code for error in errors.values()} == {"no_results"}, {k: e.code for k, e in errors.items()}
        assert all("build_demo_project.py" in error.hint for error in errors.values()), "the error must say how to fix it"
        assert errors["status"].as_dict()["error"]["code"] == "no_results"
        try:
            await fake.status("demo-anthropic")
            raise AssertionError("an unknown project id must raise")
        except port.PipelineError as error:
            assert error.code == "unknown_project" and FIXTURE_ID in error.hint, (error.code, error.hint)
        half = workspace / "half-built"
        (half / FIXTURE_ID).mkdir(parents=True)
        try:
            await port.FakePipeline(half, state).status(FIXTURE_ID)
            raise AssertionError("a project directory without run.json must raise")
        except port.PipelineError as error:
            assert error.code == "no_results" and "run.json" in error.message, error.message
        return f"4 calls -> no_results: {errors['posts'].message}"

    async def selection():
        os.environ["PIPELINE"] = "team"
        try:
            port.get_pipeline(REPO)
            raise AssertionError("PIPELINE=team must raise NotImplementedError")
        except NotImplementedError as error:
            text = str(error)
        finally:
            del os.environ["PIPELINE"]
        assert all(word in text for word in ("(1)", "(2)", "(3)")), text
        assert all(word in text.lower() for word in ("start", "tables", "api")), text
        chosen = port.get_pipeline(REPO)
        assert isinstance(chosen, port.FakePipeline)
        assert chosen.projects == REPO / "harness" / "demo" / "projects", chosen.projects
        assert REPO / "harness" / "demo" not in chosen.state.parents and chosen.state.is_relative_to(REPO / "harness" / "state"), \
            f"state must live outside harness/demo: {chosen.state}"
        return f"team needs 3 things; default reads {chosen.projects.name}/ and writes {chosen.state.name}/"

    async def real_project():
        real = port.get_pipeline(REPO)
        if not (real.projects / port.DEMO_PROJECT_ID / "posts.parquet").exists():
            return "skipped: the real demo project is not built on this machine"
        status = await real.status(port.DEMO_PROJECT_ID)
        peak = await real.posts(port.DEMO_PROJECT_ID, date_from="2026-09-10", date_to="2026-09-11",
                                question="stance", label="critical", sort="most_negative", limit=50)
        assert status["status"] == "READY" and status["counts"]["n_matched"] > 0, status
        assert peak and all(row["day"] == "2026-09-10" and row["stance"] == "critical" for row in peak)
        assert peak[0]["sentiment"] <= peak[-1]["sentiment"] and -1.0 <= peak[0]["sentiment"] <= 1.0
        assert peak[0]["id"].isdigit() and peak[0]["created_at"].endswith("Z")
        return (f"n_matched {status['counts']['n_matched']}, n_scored {status['counts']['n_scored']}, "
                f"{len(peak)} critical posts on 2026-09-10, most negative {peak[0]['sentiment']}")

    async def sampling_cap():
        real = {"2026-09-06": 71, "2026-09-07": 91, "2026-09-08": 69, "2026-09-09": 51,  # the demo's own counts
                "2026-09-10": 176, "2026-09-11": 80, "2026-09-12": 52, "2026-09-13": 58}
        assert builder.allocate(real, 3000) == real, "everything is taken while it fits under the cap"
        for cap in (1000, 647, 500, 100):
            take = builder.allocate(real, cap)
            total = sum(take.values())
            assert total <= cap, f"cap {cap} sampled {total}: max_posts is what the budget gate was computed from"
            assert total == min(cap, sum(real.values())), (cap, total)
            assert all(take[day] <= real[day] for day in real), take
            assert min(take.values()) >= 1, f"cap {cap} starved a day: {take}"
            assert take["2026-09-10"] == max(take.values()), f"the peak day must not lose to a quiet one: {take}"
            assert take == builder.allocate(real, cap), "allocation must be deterministic"
        quiet = {f"2026-09-{day:02d}": 100 for day in range(1, 21)}
        huge = {f"2026-08-{day:02d}": 200_000 for day in range(20, 25)}
        fits = builder.allocate(quiet | huge, 3000)
        over = builder.allocate({day: 200 for day in quiet} | huge, 3000)  # quiet days alone are 4000 > cap
        for take in (fits, over):
            assert sum(take.values()) == 3000, sum(take.values())
            assert min(take.values()) >= 1, f"a day with matches was allocated 0: {sorted(take.items())[:3]}"
            assert min(take[day] for day in huge) > max(take[day] for day in quiet), \
                "the busiest days must get the most posts, never zero"
        assert builder.allocate({}, 3000) == {} and builder.allocate(real, 0) == {}
        return (f"cap 100 over 648 matches -> {sum(builder.allocate(real, 100).values())} posts, "
                f"peak day {builder.allocate(real, 100)['2026-09-10']}; 20 quiet + 5 huge days -> "
                f"{sum(over.values())} with min {min(over.values())}")

    async def report_gaps():
        posts = two_days(workspace / "gapped", {DAYS[1]: 0.9, DAYS[2]: 0.1})  # nothing passes the gate on day 2
        connection = builder.connect()
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                builder.report(connection, posts, 0.5, "stance", "critical")
                builder.report(connection, posts, None)  # a gate-less project reports every scored post
        finally:
            connection.close()
        lines = [line for line in buffer.getvalue().splitlines() if line.startswith("2026-")]
        gated_out = next(line for line in lines if line.startswith(DAYS[2]))
        assert len(lines) == 4, lines
        assert gated_out.split()[2] == "0" and gated_out.count("n/a") == 3, gated_out
        assert lines[0].count("n/a") == 0 and "-0.500" in lines[0], lines[0]
        assert all(line.count("n/a") == 1 and "-0.500" in line for line in lines[2:]), \
            f"with no gate every scored post counts, and only the unasked label share is n/a: {lines[2:]}"
        return f"gated-out day prints: {' '.join(gated_out.split())}"

    async def noul_naming():
        spec = copy.deepcopy(builder.SPEC)
        spec["classification"][0]["name"] = "on_topic"
        spec["relevance_gate"]["question"] = "on_topic"
        post = {"id": "1", "created_at": datetime.fromisoformat(f"{DAYS[0]}T01:00:00+00:00"),
                "day": date.fromisoformat(DAYS[0]), "body": "playstation", "lang": "en",
                "like_count": 1, "retweet_count": 0, "views_count": 5}
        full = {"stance": {"choice": "critical", "confidence": 0.8, "probabilities": {"critical": 0.8}},
                "sentiment": {"score": 1.0, "confidence": 0.7}}
        renamed = builder.build_rows([post], {"1": {"answers": full | {"on_topic": {"noul": 0.9}}}},
                                     builder.noul_name(spec), ["stance"], 5)[0]
        assert builder.noul_name(spec) == "on_topic", builder.noul_name(spec)
        assert renamed["relevant_p"] == 0.9 and renamed["scored"], renamed
        gateless = copy.deepcopy(builder.SPEC)
        gateless["classification"] = [gateless["classification"][1]]
        gateless["relevance_gate"] = None
        without = builder.build_rows([post], {"1": {"answers": full}}, builder.noul_name(gateless), ["stance"], 5)[0]
        assert builder.noul_name(gateless) is None
        assert without["relevant_p"] is None and without["scored"], \
            "the contract says relevant_p is NULL without a gate, and such a row is still scored"
        assert without["sentiment"] == -0.5, without["sentiment"]
        for answers, why in (({}, "no answers at all"),
                             ({"1": {"answers": {"sentiment": {"score": 1.0}}}}, "the choice question unanswered"),
                             ({"1": {"answers": {"stance": {"choice": "critical"}}}}, "no sentiment")):
            row = builder.build_rows([post], answers, builder.noul_name(gateless), ["stance"], 5)[0]
            assert not row["scored"], f"scored must stay false with {why}"
        assert builder.noul_name(builder.SPEC) == "relevant"
        two = copy.deepcopy(builder.SPEC)
        two["classification"].append({"name": "is_leak", "type": "noul", "instructions": "A leak?", "options": None})
        try:  # refused up front: a paid second noul that no column could hold would be dropped silently
            builder.noul_name(two)
            raise AssertionError("two nouls cannot both live in relevant_p")
        except ValueError as error:
            assert "relevant_p" in str(error), str(error)
        return "renamed noul lands in relevant_p; a gate-less project scores rows with relevant_p NULL"

    async def failed_run():
        assert builder.run_status(648, 648) == "READY", builder.run_status(648, 648)
        assert builder.run_status(0, 0) == "READY", "no matches at all is an empty result, not a failure"
        assert builder.run_status(648, 0) == "FAILED", "a run where nothing was scored is not a result"
        assert builder.run_status(100, 50) == "FAILED" and builder.run_status(100, 51) == "READY", \
            (builder.run_status(100, 50), builder.run_status(100, 51))
        broken = json.loads((projects / FIXTURE_ID / "run.json").read_text())
        broken |= {"status": "FAILED", "n_scored": 0, "n_failed": 84, "failure_reasons": {"HTTP 401": 84}}
        dead = port.FakePipeline(clone(projects / FIXTURE_ID, workspace / "dead", run=broken), state)
        status = await dead.status(FIXTURE_ID)
        assert status["status"] == "FAILED" and status["progress"] == 1.0, status
        assert status["error"]["code"] == "run_failed" and "401" in status["error"]["message"], status["error"]
        assert status["counts"]["n_failed"] == 84, "a failed run still reports its counts, so the model can say why"
        halfway = json.loads((projects / FIXTURE_ID / "run.json").read_text()) | {"status": "RUNNING"}
        crashed = port.FakePipeline(clone(projects / FIXTURE_ID, workspace / "crashed", run=halfway), state)
        stuck = await crashed.status(FIXTURE_ID)
        assert stuck["status"] == "FAILED" and "RUNNING" in stuck["error"]["message"], \
            f"a run.json that never claimed success must not be served as READY: {stuck}"
        healthy = await port.FakePipeline(projects, workspace / "unsubmitted").status(FIXTURE_ID)
        assert healthy["status"] == "READY" and healthy["error"] is None, healthy
        waiting = await fake.status(FIXTURE_ID)  # the injected clock still has the demo's own run in flight
        assert waiting["status"] in ("QUEUED", "RUNNING") and waiting["error"] is None, waiting
        return f"run.json FAILED surfaces as {status['status']}: {status['error']['message'][:60]}"

    async def bad_model_input():
        for arguments in ({"date_from": "yesterday"}, {"date_from": "2026-13-45"}, {"date_to": "soon"},
                          {"date_to": "2026-9-1"}, {"limit": "abc"}, {"limit": []}):
            try:
                await fake.posts(FIXTURE_ID, **arguments)
                raise AssertionError(f"{arguments} must be refused with bad_request")
            except port.PipelineError as error:
                assert error.code == "bad_request", f"{arguments} gave {error.code}"
                assert "YYYY-MM-DD" in error.hint or "integer" in error.hint, error.hint
        assert len(await fake.posts(FIXTURE_ID, limit=None)) == port.MAX_POSTS_PER_PAGE, "an unset limit is a page"
        assert len(await fake.posts(FIXTURE_ID, limit="7")) == 7, "a numeric string is still a number"
        assert await fake.posts(FIXTURE_ID, date_from="", date_to=None), "an empty bound means unset"
        corrupt = port.FakePipeline(clone(projects / FIXTURE_ID, workspace / "corrupt", posts="not a parquet file"), state)
        try:
            await corrupt.posts(FIXTURE_ID)
            raise AssertionError("an unreadable posts.parquet must be a PipelineError, not a duckdb traceback")
        except port.PipelineError as error:
            assert error.code == "read_failed" and "build_demo_project.py" in error.hint, (error.code, error.hint)
        return "bad dates and limits come back as bad_request; a corrupt parquet as read_failed"

    async def second_noul():
        spec = copy.deepcopy(SPEC)
        spec["classification"].append({"name": "is_leak", "type": "noul", "instructions": "A leak?", "options": None})
        two = port.FakePipeline(clone(projects / FIXTURE_ID, workspace / "two-nouls", spec=spec), state)
        gated = await two.posts(FIXTURE_ID, question="relevant", label="yes", limit=5)
        assert gated and all(row["relevant_p"] >= 0.5 for row in gated), gated[:1]
        try:
            await two.posts(FIXTURE_ID, question="is_leak", label="yes")
            raise AssertionError("the second noul has no column, so it must not be read as relevant_p")
        except port.PipelineError as error:
            assert error.code == "unknown_question" and "relevant" in error.hint, (error.code, error.hint)
        return "only the gated noul is served from relevant_p; a second one is refused"

    async def cache_fingerprint():
        base = builder.cache_fingerprint(builder.SPEC)
        reworded = copy.deepcopy(builder.SPEC)
        reworded["classification"][1]["options"][0]["description"] = "complains about PlayStation"
        levels = copy.deepcopy(builder.SPEC)
        levels["sentiment"]["criteria"] = levels["sentiment"]["criteria"] + ["Ecstatic."]
        renamed = copy.deepcopy(builder.SPEC)
        renamed["classification"][0]["name"] = "on_topic"
        changed = {name: builder.cache_fingerprint(spec) for name, spec in
                   (("reworded option", reworded), ("extra level", levels), ("renamed question", renamed))}
        assert base == builder.cache_fingerprint(copy.deepcopy(builder.SPEC)), "the same questions hash the same"
        assert all(value != base for value in changed.values()), changed
        assert builder.cache_path(workspace, base).name == f"jev_cache-{base}.jsonl"
        path = workspace / f"jev_cache-{base}.jsonl"
        path.write_text("".join(json.dumps(record) + "\n" for record in (
            {"id": "1", "fp": base, "answers": {"relevant": {"noul": 0.9}}, "usage": {"input_tokens": 10}},
            {"id": "2", "fp": changed["extra level"], "answers": {"relevant": {"noul": 0.1}}},
            {"id": "3", "answers": {"relevant": {"noul": 0.2}}})), encoding="utf-8")  # 3 is a pre-fingerprint record
        usable, stale = builder.read_cache(path, base)
        assert list(usable) == ["1"], list(usable)
        assert stale == 2, stale
        assert builder.read_cache(workspace / "absent.jsonl", base) == ({}, 0)
        return f"{base} != {sorted(changed.values())}; cache keeps 1 of 3 records after a rubric change"

    try:
        await case("FakePipeline walks QUEUED -> RUNNING -> READY on an injected clock", progression)
        await case("submit is idempotent per key and records every submission", idempotent)
        await case("posts() caps a 500-post request at 50 and truncates bodies", capped)
        await case("every sort order works and orders as documented", sorts)
        await case("date, choice-label and noul-label filters", filters)
        await case("low_confidence means |p-0.5| < 0.2 for a noul", low_confidence)
        await case("charts are listed and packaged, unknown ids refused", charts)
        await case("a missing demo project is a clear error, not a crash", missing_project)
        await case("bad dates and limits are bad_request, not raw duckdb errors", bad_model_input)
        await case("sampling never passes max_posts and never starves a busy day", sampling_cap)
        await case("report() prints n/a for a day where nothing passed the gate", report_gaps)
        await case("the noul question is read by its spec name, gate or not", noul_naming)
        await case("a run that scored nothing is FAILED, with its reasons", failed_run)
        await case("a second noul question is refused instead of read as relevant_p", second_noul)
        await case("the answer cache is keyed by the questions and the model", cache_fingerprint)
        await case("get_pipeline picks FakePipeline unless PIPELINE=team", selection)
        await case("the real demo project reads back through the same port", real_project)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    print(f"\n{sum(OUTCOMES)}/{len(OUTCOMES)} checks passed", flush=True)
    return 0 if all(OUTCOMES) and OUTCOMES else 1


if __name__ == "__main__":
    started = time.monotonic()
    code = asyncio.run(main())
    print(f"{time.monotonic() - started:.1f}s", flush=True)
    sys.exit(code)
