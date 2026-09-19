# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2", "matplotlib>=3.9"]
# ///
"""Run: python -m uv run harness/tests/test_charts.py

Builds synthetic projects (the main one: 4 UTC days, 56 rows for 55 posts, a relevance gate, one
choice question) whose every number is hand-computable, then asserts the packaged charts against
those numbers.  No firehose query: the chart layer only ever sees a finished project's posts.parquet.
"""
import json
import os
import shutil
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import charts  # noqa: E402

REPO = Path(__file__).resolve().parent.parent.parent
SCRATCH = Path(os.environ.get("HARNESS_SCRATCH") or
               r"C:\Users\KAANER~1\AppData\Local\Temp\claude\C--Users-Kaan-Eroltu-orca-hophacks-2026\773b5d58-acc5-4780-b221-0f37be7fa74a\scratchpad")
WORK = SCRATCH / "charts-fixture"
D1, D2, D3, D4 = "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"
LONG_BODY = "PlayStation outage again " * 20  # 500 chars: must come back truncated to 280
OUTCOMES = []


def check(name, ok, detail=""):
    OUTCOMES.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""), flush=True)  # ASCII: cp1252 console


def note(text):
    print(f"note  {text}", flush=True)


def close(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol


def point(chart, series_name, x):
    series = next(s for s in chart["series"] if s["name"] == series_name)
    return next(p for p in series["points"] if p["x"] == x)


def annotations(chart, kind):
    return [a for a in chart["annotations"] if a["kind"] == kind]


def files(directory, suffix=".json"):
    return sorted(p.name for p in directory.glob(f"*{suffix}"))


SPEC = {
    "spec_version": 1,
    "name": "Synthetic charts fixture",
    "observation": {"intent": "fixture", "source": "twitter_firehose", "window": {"from": D1, "to": "2026-09-12"}},
    "filter": {"any_terms": ["playstation"], "languages": ["en"]},
    "sampling": {"max_posts": 20000, "strategy": "stratified_by_day", "seed": 7},
    "classification": [
        {"name": "relevant", "type": "noul", "instructions": "Is this post about the console outage?"},
        {"name": "stance", "type": "choice", "instructions": "Stance toward the company?",
         "options": [{"name": "critical", "description": "blames"}, {"name": "supportive", "description": "defends"},
                     {"name": "neutral_news", "description": "reports"}, {"name": "unclear", "description": "cannot tell"}]},
    ],
    "relevance_gate": {"question": "relevant", "min_probability": 0.5},
    "sentiment": {"type": "score", "instructions": "Overall sentiment.",
                  "criteria": ["Very negative", "Negative", "Neutral or mixed", "Positive", "Very positive"]},
    "budget": {"max_usd": 2.0},
}

TOPIC_QUESTION = {"name": "topic", "type": "choice", "instructions": "What is it about?",
                  "options": [{"name": "outage"}, {"name": "price"}]}

RUN = {
    "project_id": "synthetic-charts", "status": "done", "started_ms": 1, "finished_ms": 2,
    "n_matched": 364, "n_scored": 53, "n_failed": 0,
    "jev_input_tokens": 18560, "jev_cost_usd": 0.00078,
    "sampled_fraction_by_day": {D1: 0.1, D2: 0.2, D3: 0.0, D4: 1.0},
    "matched_by_day": {D1: 250, D2: 105, D3: 0, D4: 9},  # 250*0.1=25, 105*0.2=21, 9*1.0=9 sampled posts
}

DENOMINATORS = """day,lang,tweets,authors,originals,rt_tweets,partial_day
2026-09-08,<ALL>,9000000,2000000,4000000,5000000,False
2026-09-08,en,2500000,900000,1000000,1500000,False
2026-09-09,<ALL>,5000000,1200000,2000000,3000000,False
2026-09-09,en,1200000,500000,500000,700000,False
2026-09-10,<ALL>,900000,300000,400000,500000,False
2026-09-10,en,450000,180000,200000,250000,False
"""


def rows():
    """One dict per posts.parquet row; see the comment above each block for the arithmetic."""
    # D1: 20 relevant posts.  19 quiet ones at +0.5 and one very loud negative -> the likes-weighted
    # mean is -990.5/1019 while the unweighted mean is +0.425.  Plus 3 gated out and 2 unscored.
    out = [{"id": "d1-loud", "day": D1, "sentiment": -1.0, "likes": 999, "relevant_p": 0.95,
            "stance": "critical", "body": LONG_BODY, "second": 10}]
    for i in range(19):
        stance = "critical" if i < 9 else ("supportive" if i < 14 else "neutral_news")
        out.append({"day": D1, "sentiment": 0.5, "relevant_p": 0.9, "stance": stance, "body": f"d1 quiet {i}"})
    out.append({"day": D1, "sentiment": 0.0, "likes": 5000, "relevant_p": 0.2, "stance": "critical",
                "body": "gated out but very liked"})
    out += [{"day": D1, "sentiment": 0.0, "relevant_p": 0.2, "stance": "critical", "body": f"gated out {i}"}
            for i in range(2)]
    out += [{"day": D1, "likes": 9999, "scored": False, "body": f"never scored {i}"} for i in range(2)]
    # D2: 20 relevant posts, no likes anywhere -> weighted == unweighted == -0.25.  1 gated out.
    out += [{"day": D2, "sentiment": 0.0, "relevant_p": 0.8, "stance": "critical", "body": f"d2 zero {i}"}
            for i in range(10)]
    out += [{"day": D2, "sentiment": -0.5, "relevant_p": 0.8, "stance": "unclear", "body": f"d2 half {i}"}
            for i in range(10)]
    out.append({"day": D2, "sentiment": -1.0, "relevant_p": 0.4, "stance": "critical", "body": "d2 gated out"})
    # D3: nothing at all.
    # D4: 4 x (+1.0, weight 1) and 4 x (+0.25, weight 4) -> weighted 0.4, unweighted 0.625; plus one
    # relevant post whose sentiment is NULL: it counts in the share denominator (9) but not in the mean (8).
    out += [{"day": D4, "sentiment": 1.0, "relevant_p": 0.9, "stance": "critical", "body": f"d4 loud praise {i}"}
            for i in range(4)]
    out += [{"day": D4, "sentiment": 0.25, "likes": 3, "relevant_p": 0.9, "stance": "supportive",
             "body": f"d4 mild praise {i}"} for i in range(4)]
    out.append({"day": D4, "relevant_p": 0.9, "stance": "unclear", "body": "d4 sentiment missing"})
    # A resumed run appended a second, older and less confident snapshot of d1-loud, with the opposite
    # sentiment and a different label.  Every number above must be exactly as if it were not here.
    out.append({"id": "d1-loud", "day": D1, "sentiment": 1.0, "relevant_p": 0.95, "stance": "supportive",
                "body": "stale snapshot of the loud post", "confidence": 0.1, "second": 1})
    return out


def write_project(directory, spec, posts=None, questions=("stance",), run=None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "spec.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    (directory / "run.json").write_text(json.dumps(run or RUN, indent=2), encoding="utf-8")
    columns = ["id VARCHAR", "created_at TIMESTAMPTZ", "day DATE", "body VARCHAR", "lang VARCHAR",
               "like_count BIGINT", "retweet_count BIGINT", "views_count BIGINT", "relevant_p DOUBLE"]
    for question in questions:
        columns += [f"{question} VARCHAR", f"{question}_confidence DOUBLE", f"{question}_probs VARCHAR"]
    columns += ["sentiment_raw DOUBLE", "sentiment DOUBLE", "sentiment_confidence DOUBLE", "scored BOOLEAN"]
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute(f"CREATE TABLE posts ({', '.join(columns)})")
    payload = []
    for i, row in enumerate(posts if posts is not None else rows()):
        day, sentiment, likes = row["day"], row.get("sentiment"), row.get("likes", 0)
        stamp = datetime.fromisoformat(day).replace(hour=12, tzinfo=timezone.utc) + timedelta(seconds=row.get("second", i))
        values = [row.get("id") or f"t{i:04d}", stamp, date.fromisoformat(day), row.get("body", f"post {i}"),
                  "en", likes, 0, likes * 40, row.get("relevant_p")]
        for question in questions:
            label = row.get(question)
            values += [label, None if label is None else 0.77,
                       None if label is None else json.dumps({label: 0.8, "unclear": 0.2})]
        values += [None if sentiment is None else (sentiment + 1) * 2,  # sentiment = 2*raw/(5-1) - 1
                   sentiment, None if sentiment is None else row.get("confidence", 0.66), row.get("scored", True)]
        payload.append(tuple(values))
    con.executemany(f"INSERT INTO posts VALUES ({','.join('?' * len(columns))})", payload)
    path = str(directory / "posts.parquet").replace("'", "''")
    con.execute(f"COPY posts TO '{path}' (FORMAT parquet)")
    con.close()
    return len(payload)


def main():
    if WORK.exists():
        shutil.rmtree(WORK)
    project = WORK / "synthetic-charts"
    n_rows = write_project(project, SPEC)
    csv_path = WORK / "daily-denominators.csv"
    csv_path.write_text(DENOMINATORS, encoding="utf-8")
    check("fixture written", n_rows == 56 and (project / "posts.parquet").exists(),
          f"{n_rows} rows for 55 distinct ids")

    built = charts.build_all(project, REPO, denominators=csv_path)
    by_id = {c["chart_id"]: c for c in built}
    check("build_all writes one json per chart",
          files(project / "charts") == ["sentiment_daily.json", "share_daily__stance.json", "top_posts.json",
                                        "totals__stance.json", "volume_daily.json"], str(files(project / "charts")))
    listed = charts.list_charts(project)
    check("list_charts order and shape",
          [c["chart_id"] for c in listed] == ["volume_daily", "sentiment_daily", "share_daily__stance",
                                              "totals__stance", "top_posts"]
          and all(set(c) == {"chart_id", "title", "type"} for c in listed)
          and [c["type"] for c in listed] == ["line", "line", "line", "bar", "table"],
          str([c["chart_id"] for c in listed]))

    # --- volume_daily: exact counts from run.json, per_100k against the injected denominators
    volume = by_id["volume_daily"]
    ys = [p["y"] for p in volume["series"][0]["points"]]
    per = [p["per_100k"] for p in volume["series"][0]["points"]]
    check("volume_daily has every window day, exact counts",
          [p["x"] for p in volume["series"][0]["points"]] == [D1, D2, D3, D4] and ys == [250, 105, 0, 9], str(ys))
    check("volume_daily per_100k = count / originals(en) * 100k",
          close(per[0], 25.0) and close(per[1], 21.0) and close(per[2], 0.0) and per[3] is None, str(per))
    check("volume_daily summary start/end/min/max",
          volume["summary"]["start"] == {"x": D1, "y": 250} and volume["summary"]["end"] == {"x": D4, "y": 9}
          and volume["summary"]["min"] == {"x": D3, "y": 0} and volume["summary"]["max"] == {"x": D1, "y": 250},
          json.dumps(volume["summary"]))
    change = annotations(volume, "largest_change")
    check("volume_daily largest_change is the -145 drop on 09-09",
          len(change) == 1 and change[0]["x"] == D2 and close(change[0]["delta"], -145)
          and change[0]["from"] == D1 and change[0]["days_apart"] == 1, json.dumps(change))
    check("volume_daily low_n flags only the two thin days",
          [a["x"] for a in annotations(volume, "low_n")] == [D3, D4], json.dumps(annotations(volume, "low_n")))
    check("volume_daily method names the denominator and the language",
          "originals" in volume["method"]["denominator"] and " en," in volume["method"]["denominator"]
          and volume["method"]["counts"] == "distinct tweet IDs", volume["method"]["denominator"])
    check("volume_daily carries the per-day sampled fraction",
          [p["sampled_fraction"] for p in volume["series"][0]["points"]] == [0.1, 0.2, 0.0, 1.0])

    # --- sentiment_daily: likes-weighted, gated, one series
    sentiment = by_id["sentiment_daily"]
    p1, p2, p3, p4 = (point(sentiment, "all relevant posts", d) for d in (D1, D2, D3, D4))
    expect1 = (19 * 1 * 0.5 + 1000 * -1.0) / (19 + 1000)
    check("one highly liked negative post drags the weighted mean below the unweighted one",
          close(p1["y"], expect1) and close(p1["y_unweighted"], 0.425) and p1["y"] < p1["y_unweighted"] - 1.3,
          f"y={p1['y']} (expected {expect1:.6f}) y_unweighted={p1['y_unweighted']}")
    check("uniform likes make the two means identical",
          close(p2["y"], -0.25) and close(p2["y_unweighted"], -0.25), f"y={p2['y']}")
    check("weighted mean with mixed weights: 8/20 = 0.4 vs 0.625 unweighted",
          close(p4["y"], 0.4) and close(p4["y_unweighted"], 0.625), f"y={p4['y']} y_unweighted={p4['y_unweighted']}")
    check("gated-out posts are excluded from the mean and counted separately",
          p1["n"] == 20 and p1["n_gated_out"] == 3 and p2["n"] == 20 and p2["n_gated_out"] == 1,
          f"D1 n={p1['n']} gated={p1['n_gated_out']} D2 n={p2['n']} gated={p2['n_gated_out']}")
    check("unscored posts are excluded everywhere (25 posts on 09-08 -> 20 + 3 counted)",
          p1["n"] + p1["n_gated_out"] == 23)
    check("a post with NULL sentiment is not in the mean (n = 8 of 9 relevant)", p4["n"] == 8)
    check("the empty day is present with y null and n 0",
          p3["y"] is None and p3["y_unweighted"] is None and p3["n"] == 0 and p3["n_gated_out"] == 0,
          json.dumps(p3))
    check("sentiment_daily summary over non-null points",
          close(sentiment["summary"]["min"]["y"], expect1) and sentiment["summary"]["min"]["x"] == D1
          and close(sentiment["summary"]["max"]["y"], 0.4) and sentiment["summary"]["max"]["x"] == D4
          and sentiment["summary"]["start"]["x"] == D1 and sentiment["summary"]["end"]["x"] == D4,
          json.dumps(sentiment["summary"]))
    change = annotations(sentiment, "largest_change")
    check("sentiment_daily largest_change is the 09-08 -> 09-09 move, one day apart",
          len(change) == 1 and change[0]["x"] == D2 and close(change[0]["delta"], -0.25 - expect1)
          and change[0]["from"] == D1 and change[0]["days_apart"] == 1, json.dumps(change))
    check("sentiment_daily low_n flags all four days (n = 20, 20, 0, 8)",
          [a["n"] for a in annotations(sentiment, "low_n")] == [20, 20, 0, 8], json.dumps(annotations(sentiment, "low_n")))
    check("sentiment_daily method block",
          sentiment["method"]["weighting"] == "likes (1 + like_count)"
          and sentiment["method"]["relevance_gate"] == 0.5 and sentiment["method"]["n_levels"] == 5
          and sentiment["method"]["counts"] == "distinct tweet IDs", json.dumps(sentiment["method"]))
    check("sentiment_daily drilldown is a get_posts day window",
          sentiment["drilldown"] == {"tool": "get_posts", "args_template": {"from": "{x}", "to": "{x}+1d"}})

    # --- share_daily__stance
    share = by_id["share_daily__stance"]
    check("one series per spec option, in spec order",
          [s["name"] for s in share["series"]] == ["critical", "supportive", "neutral_news", "unclear"])
    d1 = [point(share, name, D1)["y"] for name in ("critical", "supportive", "neutral_news", "unclear")]
    d4 = [point(share, name, D4)["y"] for name in ("critical", "supportive", "neutral_news", "unclear")]
    check("shares on 09-08 are 10/20, 5/20, 5/20, 0/20", d1 == [0.5, 0.25, 0.25, 0.0], str(d1))
    check("shares on 09-11 are 4/9, 4/9, 0/9, 1/9",
          close(d4[0], 4 / 9) and close(d4[1], 4 / 9) and d4[2] == 0.0 and close(d4[3], 1 / 9), str(d4))
    sums = []
    for day in (D1, D2, D4):
        sums.append(sum(point(share, s["name"], day)["y"] for s in share["series"]))
    check("shares sum to 1 on every non-empty day", all(close(total, 1.0, 1e-5) for total in sums), str(sums))
    check("share denominators are the relevant scored posts of that day (20, 20, 0, 9)",
          [point(share, "critical", d)["n"] for d in (D1, D2, D3, D4)] == [20, 20, 0, 9])
    check("the empty day is null in every series, never skipped",
          all(point(share, s["name"], D3)["y"] is None and point(share, s["name"], D3)["n"] == 0
              for s in share["series"]))
    check("share_daily drilldown carries question and series",
          share["drilldown"]["args_template"]["question"] == "stance"
          and share["drilldown"]["args_template"]["label"] == "{series}")
    check("share_daily summary marks which series it came from",
          share["summary"]["max"] == {"x": D1, "y": 0.5, "series": "critical"}, json.dumps(share["summary"]))

    # --- totals__stance
    total = by_id["totals__stance"]
    counts = [p["y"] for p in total["series"][0]["points"]]
    check("totals counts per option are 24, 9, 5, 11 of 49", counts == [24, 9, 5, 11]
          and all(p["of"] == 49 for p in total["series"][0]["points"]), str(counts))
    check("totals shares sum to 1",
          close(sum(p["share"] for p in total["series"][0]["points"]), 1.0, 1e-5)
          and close(total["series"][0]["points"][0]["share"], 24 / 49))
    check("totals is a bar chart with no day-over-day annotation",
          total["type"] == "bar" and annotations(total, "largest_change") == []
          and len(annotations(total, "low_n")) == 4)

    # --- top_posts
    top = by_id["top_posts"]
    rows_out = top["series"][0]["points"]
    check("top_posts returns the 20 most liked relevant posts",
          top["type"] == "table" and len(rows_out) == 20
          and [r["like_count"] for r in rows_out][:6] == [999, 3, 3, 3, 3, 0], str([r["like_count"] for r in rows_out][:6]))
    check("a very liked but gated-out post and an unscored 9999-like post stay out",
          max(r["like_count"] for r in rows_out) == 999, str(max(r["like_count"] for r in rows_out)))
    check("top_posts truncates the body to 280 chars and carries labels",
          len(rows_out[0]["body"]) == 280 and rows_out[0]["stance"] == "critical"
          and close(rows_out[0]["sentiment"], -1.0) and rows_out[0]["day"] == D1, str(len(rows_out[0]["body"])))
    check("top_posts flags the small pool once", annotations(top, "low_n") == [{"x": None, "kind": "low_n", "n": 49}],
          json.dumps(annotations(top, "low_n")))

    # --- a duplicated id is one post everywhere: the mean, the shares and the table agree (finding 3)
    check("top_posts lists every id once",
          len({r["id"] for r in rows_out}) == 20, str(len({r["id"] for r in rows_out})))
    check("the duplicated post appears once, as its newest snapshot",
          [r["id"] for r in rows_out].count("d1-loud") == 1 and rows_out[0]["id"] == "d1-loud"
          and rows_out[0]["stance"] == "critical" and close(rows_out[0]["sentiment"], -1.0),
          json.dumps({"stance": rows_out[0]["stance"], "sentiment": rows_out[0]["sentiment"]}))
    check("the stale snapshot is in neither mean (it would make them 0.452 and -0.969)",
          close(p1["y_unweighted"], 0.425) and close(p1["y"], expect1),
          f"y={p1['y']} y_unweighted={p1['y_unweighted']}")
    check("the stale snapshot does not inflate the share denominator or the totals",
          point(share, "critical", D1)["n"] == 20 and point(share, "supportive", D1)["count"] == 5
          and total["series"][0]["points"][1]["y"] == 9,
          f"n={point(share, 'critical', D1)['n']} supportive={point(share, 'supportive', D1)['count']}")

    # --- largest_change across a quiet day (finding 1)
    gap = WORK / "null-gap"
    write_project(gap, SPEC, posts=[
        {"day": D1, "sentiment": 0.0, "relevant_p": 0.9, "stance": "critical"},
        {"day": D2, "sentiment": 0.1, "relevant_p": 0.9, "stance": "critical"},
        {"day": D4, "sentiment": -1.0, "relevant_p": 0.9, "stance": "critical"},
    ])
    gap_charts = {c["chart_id"]: c for c in charts.build_all(gap, REPO, denominators=csv_path)}
    gap_sentiment = gap_charts["sentiment_daily"]
    gap_ys = [p["y"] for p in gap_sentiment["series"][0]["points"]]
    check("the quiet day is a null between three observed points", gap_ys == [0.0, 0.1, None, -1.0], str(gap_ys))
    gap_change = annotations(gap_sentiment, "largest_change")
    check("largest_change is the -1.1 move that spans the quiet day, not the +0.1 adjacent one",
          len(gap_change) == 1 and gap_change[0]["x"] == D4 and gap_change[0]["from"] == D2
          and close(gap_change[0]["delta"], -1.1) and gap_change[0]["days_apart"] == 2, json.dumps(gap_change))

    # --- a rebuild removes the charts of a dropped question (finding 2)
    stale = WORK / "dropped-question"
    spec_two = json.loads(json.dumps(SPEC))
    spec_two["classification"].append(TOPIC_QUESTION)
    write_project(stale, spec_two, questions=("stance", "topic"), posts=[
        {"day": D1, "sentiment": 0.0, "relevant_p": 0.9, "stance": "critical", "topic": "outage"},
        {"day": D2, "sentiment": 0.5, "relevant_p": 0.9, "stance": "supportive", "topic": "price"},
    ])
    first = [c["chart_id"] for c in charts.build_all(stale, REPO, denominators=csv_path, png=True)]
    check("two choice questions give seven charts",
          sorted(first) == ["sentiment_daily", "share_daily__stance", "share_daily__topic", "top_posts",
                            "totals__stance", "totals__topic", "volume_daily"]
          and len(files(stale / "charts", ".png")) == 7, str(sorted(first)))
    (stale / "spec.json").write_text(json.dumps(SPEC, indent=2), encoding="utf-8")  # the topic question is dropped
    charts.build_all(stale, REPO, denominators=csv_path, png=True)
    check("the dropped question's json and png are deleted, not left to be served as current",
          files(stale / "charts") == ["sentiment_daily.json", "share_daily__stance.json", "top_posts.json",
                                      "totals__stance.json", "volume_daily.json"]
          and not (stale / "charts" / "share_daily__topic.png").exists(),
          str(files(stale / "charts") + files(stale / "charts", ".png")))
    check("list_charts no longer advertises the dropped question",
          [c["chart_id"] for c in charts.list_charts(stale)] == ["volume_daily", "sentiment_daily",
                                                                 "share_daily__stance", "totals__stance", "top_posts"],
          str([c["chart_id"] for c in charts.list_charts(stale)]))
    charts.build_all(stale, REPO, denominators=csv_path)
    check("a build without --png leaves no image outliving its numbers",
          files(stale / "charts", ".png") == [], str(files(stale / "charts", ".png")))

    # --- language selection for the denominator
    two_langs = WORK / "two-langs"
    spec2 = json.loads(json.dumps(SPEC))
    spec2["filter"]["languages"] = ["en", "ja"]
    write_project(two_langs, spec2)
    volume2 = {c["chart_id"]: c for c in charts.build_all(two_langs, REPO, denominators=csv_path)}["volume_daily"]
    check("more than one language falls back to the <ALL> denominator (250/4M*100k = 6.25)",
          close(volume2["series"][0]["points"][0]["per_100k"], 6.25)
          and "<ALL>" in volume2["method"]["denominator"],
          str(volume2["series"][0]["points"][0]["per_100k"]))

    # --- rendering
    sizes = {}
    for chart_id in ("volume_daily", "sentiment_daily", "share_daily__stance", "totals__stance", "top_posts"):
        path = project / "charts" / f"{chart_id}.png"
        charts.render_png(by_id[chart_id], path)
        sizes[chart_id] = path.stat().st_size if path.exists() else 0
    check("render_png writes non-empty PNGs for line, bar and table charts",
          all(size > 3000 for size in sizes.values()), json.dumps(sizes))
    check("PNG files are real PNGs",
          all((project / "charts" / f"{cid}.png").read_bytes()[:4] == b"\x89PNG" for cid in sizes))

    from_thread = {}

    def worker():
        try:
            path = project / "charts" / "thread_share.png"
            charts.render_png(by_id["share_daily__stance"], path)
            from_thread["size"] = path.stat().st_size
        except Exception as error:  # noqa: BLE001 - the point of the test is that nothing escapes
            from_thread["error"] = repr(error)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(60)
    check("render_png works from a worker thread (no pyplot global state)",
          "error" not in from_thread and from_thread.get("size", 0) > 3000, json.dumps(from_thread))

    check("a question name that is not a plain identifier is refused",
          _raises(lambda: charts._ident('stance"; DROP TABLE posts --')))

    # --- the real demo project, if the agent building it has finished
    demo = REPO / "harness" / "demo" / "projects" / "demo-playstation"
    if (demo / "posts.parquet").exists() and (demo / "spec.json").exists() and (demo / "run.json").exists():
        try:
            demo_charts = {c["chart_id"]: c for c in charts.build_all(demo, REPO)}
            note(f"demo-playstation charts: {sorted(demo_charts)}")
            for p in demo_charts["sentiment_daily"]["series"][0]["points"]:
                note(f"  {p['x']} y={p['y']} y_unweighted={p['y_unweighted']} n={p['n']} "
                     f"n_gated_out={p['n_gated_out']} sampled_fraction={p['sampled_fraction']}")
            note(f"  summary {json.dumps(demo_charts['sentiment_daily']['summary'])}")
            note(f"  annotations {json.dumps(demo_charts['sentiment_daily']['annotations'])}")
        except Exception as error:  # noqa: BLE001 - another agent owns that file; report, do not fail
            note(f"demo-playstation present but build_all failed: {error!r}")
    else:
        note("demo-playstation posts.parquet does not exist yet: not built")

    print(f"\n{sum(OUTCOMES)}/{len(OUTCOMES)} checks passed", flush=True)
    return 0 if all(OUTCOMES) else 1


def _raises(call):
    try:
        call()
    except ValueError:
        return True
    return False


if __name__ == "__main__":
    sys.exit(main())
