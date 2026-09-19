# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2", "matplotlib>=3.9"]
# ///
"""Packaged chart data (DESIGN.md section 9) for a finished project, plus PNG rendering.

Run: python -m uv run harness/charts.py <project_dir> [repo_root] [--png]

The agent reads these numbers and never an image.  Sentiment is LIKES-WEIGHTED (section 0): the
weight is 1 + like_count, so a post with 50k likes counts as much as 50k quiet ones and a quiet
post still counts once.  Both means are carried on every point (y, y_unweighted) because the gap
between them is itself the finding.
"""
import csv
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import duckdb

INK = "#263d31"  # Signal dark green
MUTED = "#718375"
GRID = "#e4e7e2"
SURFACE = "#fcfcfb"
# Validated categorical order (dataviz skill, light mode); colour follows the option, never its rank.
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
LOW_N = 100
TOP_POSTS = 20
BODY_CHARS = 280
ROUND = 6
WEIGHTING = "likes (1 + like_count)"
COUNTS = "distinct tweet IDs"
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _ident(name):
    """Question names become column identifiers, so they are checked, never trusted."""
    if not IDENT.match(name or ""):
        raise ValueError(f"unsafe identifier in spec: {name!r}")
    return f'"{name}"'


def _columns(con, posts):
    return [d[0] for d in con.execute("SELECT * FROM read_parquet($path) LIMIT 0", {"path": str(posts)}).description]


def _choice_questions(spec):
    return [q for q in (spec.get("classification") or []) if q.get("type") == "choice" and q.get("name")]


def _option_names(question):
    names = [o if isinstance(o, str) else (o or {}).get("name") for o in question.get("options") or []]
    return [n for n in names if n]


def _n_levels(spec):
    sentiment = spec.get("sentiment") or {}
    return len(sentiment.get("criteria") or []) or None


def _denominator_lang(spec):
    langs = (spec.get("filter") or {}).get("languages") or []
    return langs[0] if len(langs) == 1 else "<ALL>"


def _originals(path, lang):
    """day -> originals from topic-analysis/daily-denominators.csv for one language row."""
    out = {}
    path = Path(path)
    if not path.exists():
        return out
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("lang") != lang:
                continue
            try:
                out[row["day"]] = int(row["originals"])
            except (KeyError, TypeError, ValueError):
                continue
    return out


def _days(spec, run, post_days):
    """Every UTC day of the window appears, even the empty ones (a gap is a finding, not a skip)."""
    bounds = [date.fromisoformat(d) for d in set(run.get("matched_by_day") or {}) | set(post_days)]
    window = (spec.get("observation") or {}).get("window") or {}
    if window.get("from"):
        bounds.append(date.fromisoformat(window["from"]))
    if window.get("to"):
        bounds.append(date.fromisoformat(window["to"]) - timedelta(days=1))  # `to` is exclusive (section 5.1)
    if not bounds:
        return []
    start, end = min(bounds), max(bounds)
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def _summary(series, by_x=True):
    """start/end along x (or in option order for bars), min/max by y, ties broken by the earliest x."""
    flat = [(i, s["name"], p) for i, s in enumerate(series) for p in s["points"] if p.get("y") is not None]
    if not flat:
        return {"start": None, "end": None, "min": None, "max": None}
    if by_x:
        flat.sort(key=lambda t: (t[2]["x"], t[0]))
    multi = len(series) > 1
    mark = lambda t: {"x": t[2]["x"], "y": t[2]["y"], **({"series": t[1]} if multi else {})}  # noqa: E731
    return {
        "start": mark(flat[0]),
        "end": mark(flat[-1]),
        "min": mark(min(flat, key=lambda t: (t[2]["y"], t[2]["x"], t[0]))),
        "max": mark(min(flat, key=lambda t: (-t[2]["y"], t[2]["x"], t[0]))),
    }


def _days_apart(start, end):
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except (TypeError, ValueError):
        return None


def _annotations(series, day_over_day=True):
    """largest_change is the biggest move between consecutive *observed* points, not between adjacent
    array positions: a quiet day is a null, and the real change often straddles it, so the pair is
    kept and `days_apart` says it was not literally day over day."""
    out, best = [], None
    if day_over_day:
        for s in series:
            observed = [p for p in s["points"] if p.get("y") is not None]
            for prev, cur in zip(observed, observed[1:]):
                delta = cur["y"] - prev["y"]
                if best is None or abs(delta) > abs(best[2]):
                    best = (s["name"], cur["x"], delta, prev["x"])
    if best:
        annotation = {"x": best[1], "kind": "largest_change", "delta": round(best[2], ROUND), "from": best[3]}
        apart = _days_apart(best[3], best[1])
        if apart is not None:
            annotation["days_apart"] = apart
        if len(series) > 1:
            annotation["series"] = best[0]
        out.append(annotation)
    seen = set()
    for s in series:  # n is a property of the x position, so one low_n per x
        for p in s["points"]:
            n = p.get("n")
            if n is None or p["x"] in seen:
                continue
            seen.add(p["x"])
            if n < LOW_N:
                out.append({"x": p["x"], "kind": "low_n", "n": n})
    return out


def _method(spec, min_p, denominator, weighting=False):
    method = {
        "relevance_gate": min_p,
        "n_levels": _n_levels(spec),
        "denominator": denominator,
        "counts": COUNTS,
    }
    if weighting:
        method = {"weighting": WEIGHTING, **method}
    return method


def _chart(chart_id, ctype, title, x, y, series, spec, min_p, denominator, weighting=False, day_over_day=True, drilldown=None):
    return {
        "chart_id": chart_id,
        "type": ctype,
        "title": title,
        "x": x,
        "y": y,
        "series": series,
        "summary": _summary(series, by_x=day_over_day),
        "annotations": _annotations(series, day_over_day=day_over_day),
        "method": _method(spec, min_p, denominator, weighting),
        "drilldown": drilldown or {"tool": "get_posts", "args_template": {"from": "{x}", "to": "{x}+1d"}},
    }


def _rel(min_p):
    # A NULL relevant_p under a gate is unverified, so it is gated out rather than silently counted.
    return "TRUE" if min_p is None else "(relevant_p IS NOT NULL AND relevant_p >= $minp)"


def _where(min_p):
    return f"scored AND {_rel(min_p)}"


def _params(min_p):
    return {} if min_p is None else {"minp": min_p}


def _newest(columns):
    """Which snapshot wins when one id appears twice.  The results contract carries no `version`, so
    the most complete row wins: scored first, then the most confident, then the later timestamps."""
    keys = [c for c in ("version", "added_at", "scored", "sentiment_confidence", "relevant_p", "created_at")
            if c in columns]
    return ", ".join(f"{c} DESC NULLS LAST" for c in keys) or "id"


def _materialize(con, posts, columns, day_expr, questions):
    """One row per id, once, in `one`; every chart then aggregates the same rows.  A resumed or
    re-scored run can append a second snapshot of a post, and a likes-weighted mean over duplicated
    rows beside a DISTINCT `n` would make method.counts = "distinct tweet IDs" a false statement
    about the mean.  Tiny data, so materializing it is cheaper than repeating the window."""
    projection = ["id", f"{day_expr} AS day", "coalesce(like_count, 0) AS like_count", "sentiment",
                  "sentiment_confidence", f"left(body, {BODY_CHARS}) AS body", "scored"]
    if "relevant_p" in columns:
        projection.append("relevant_p")
    projection += [_ident(q["name"]) for q in questions]
    con.execute(f"CREATE TEMP TABLE one AS SELECT {', '.join(projection)} FROM read_parquet($path) "
                f"QUALIFY row_number() OVER (PARTITION BY id ORDER BY {_newest(columns)}) = 1",
                {"path": str(posts)})
    kept, rows = con.execute("SELECT (SELECT count(*) FROM one), count(*) FROM read_parquet($path)",
                             {"path": str(posts)}).fetchone()
    if rows != kept:
        print(f"charts: posts.parquet has {rows} rows for {kept} distinct ids; "
              "kept one snapshot per id", file=sys.stderr)
    return int(kept)


def volume_daily(spec, run, days, denominators, sampled, fallback_counts, min_p):
    matched, exact = run.get("matched_by_day") or {}, True
    if not matched:
        matched, exact = fallback_counts, False
    lang = _denominator_lang(spec)
    originals = _originals(denominators, lang)
    points = []
    for day in days:
        n = int(matched.get(day, 0) or 0)
        base = originals.get(day)
        points.append({
            "x": day,
            "y": n,  # an exact count: a day with no match is a measured zero, not a missing value
            "n": n,
            "per_100k": round(n / base * 100000, ROUND) if base else None,
            "originals": base,
            "sampled_fraction": sampled.get(day),
        })
    chart = _chart(
        "volume_daily", "line", "Matching posts per day",
        {"field": "day", "type": "date"}, {"field": "matched_posts", "unit": "posts per day"},
        [{"name": "matching original posts", "points": points}],
        spec, min_p, f"originals, {lang}, per day (topic-analysis/daily-denominators.csv)",
    )
    if not exact:
        chart["method"]["counts"] = COUNTS + " (sampled posts: run.json carried no matched_by_day)"
    return chart


def sentiment_daily(con, spec, days, sampled, min_p):
    rows = con.execute(f"""
        WITH p AS (
          SELECT id, day, like_count AS w, sentiment, {_rel(min_p)} AS rel FROM one WHERE scored
        )
        SELECT day,
               count(DISTINCT id) FILTER (WHERE rel AND sentiment IS NOT NULL) AS n,
               count(DISTINCT id) FILTER (WHERE NOT rel) AS n_gated_out,
               sum((1 + w) * sentiment) FILTER (WHERE rel AND sentiment IS NOT NULL) AS wnum,
               sum(1 + w) FILTER (WHERE rel AND sentiment IS NOT NULL) AS wden,
               avg(sentiment) FILTER (WHERE rel AND sentiment IS NOT NULL) AS plain
        FROM p GROUP BY day
    """, _params(min_p)).fetchall()
    by_day = {str(r[0]): r for r in rows}
    points = []
    for day in days:
        row = by_day.get(day)
        n = int(row[1]) if row else 0
        weighted = row[3] / row[4] if row and row[4] else None
        points.append({
            "x": day,
            "y": round(weighted, ROUND) if weighted is not None else None,
            "y_unweighted": round(row[5], ROUND) if row and row[5] is not None else None,
            "n": n,
            "n_gated_out": int(row[2]) if row else 0,
            "sampled_fraction": sampled.get(day),
        })
    return _chart(
        "sentiment_daily", "line", "Mean sentiment per day (likes-weighted)",
        {"field": "day", "type": "date"}, {"field": "mean_sentiment", "unit": "score -1..+1"},
        [{"name": "all relevant posts", "points": points}],
        spec, min_p, "relevant scored posts per day", weighting=True,
    )


def _label_counts(con, question, min_p):
    rows = con.execute(f"""
        WITH p AS (SELECT id, day, {_ident(question)} AS label FROM one WHERE {_where(min_p)})
        SELECT day, label, count(DISTINCT id) AS c FROM p GROUP BY day, label
    """, _params(min_p)).fetchall()
    counts, totals = {}, {}
    for day, label, c in rows:
        day = str(day)
        counts[(day, label)] = int(c)
        totals[day] = totals.get(day, 0) + int(c)
    return counts, totals


def share_daily(con, spec, question, days, sampled, min_p):
    counts, totals = _label_counts(con, question["name"], min_p)
    options = _option_names(question)
    extra = sorted({label for _, label in counts if label not in options}, key=lambda v: (v is None, v))
    names = options + [("(unlabelled)" if label is None else label) for label in extra]
    labels = options + extra  # shares must sum to 1, so labels the spec did not declare get their own series
    series = []
    for name, label in zip(names, labels):
        points = []
        for day in days:
            total = totals.get(day, 0)
            c = counts.get((day, label), 0)
            points.append({
                "x": day,
                "y": round(c / total, ROUND) if total else None,
                "count": c,
                "n": total,
                "sampled_fraction": sampled.get(day),
            })
        series.append({"name": name, "points": points})
    return _chart(
        f"share_daily__{question['name']}", "line", f"Share of relevant posts by {question['name']} per day",
        {"field": "day", "type": "date"}, {"field": "share", "unit": "share of relevant posts 0..1"},
        series, spec, min_p, "relevant scored posts per day",
        drilldown={"tool": "get_posts", "args_template": {"from": "{x}", "to": "{x}+1d", "question": question["name"], "label": "{series}"}},
    )


def totals(con, spec, question, min_p):
    counts, _ = _label_counts(con, question["name"], min_p)
    overall = {}
    for (_, label), c in counts.items():
        overall[label] = overall.get(label, 0) + c
    options = _option_names(question)
    extra = sorted({label for label in overall if label not in options}, key=lambda v: (v is None, v))
    total = sum(overall.values())
    points = [{
        "x": "(unlabelled)" if label is None else label,
        "y": overall.get(label, 0),
        "n": overall.get(label, 0),
        "share": round(overall.get(label, 0) / total, ROUND) if total else None,
        "of": total,
    } for label in options + extra]
    window = (spec.get("observation") or {}).get("window") or {}
    return _chart(
        f"totals__{question['name']}", "bar", f"Relevant posts by {question['name']}, whole window",
        {"field": "option", "type": "string"}, {"field": "posts", "unit": "posts"},
        [{"name": question["name"], "points": points}],
        spec, min_p, "relevant scored posts in the window", day_over_day=False,
        drilldown={"tool": "get_posts", "args_template": {"from": window.get("from"), "to": window.get("to"), "question": question["name"], "label": "{x}"}},
    )


def top_posts(con, spec, questions, min_p):
    choice_cols = "".join(f", {_ident(q['name'])}" for q in questions)
    rows = con.execute(f"""
        SELECT id, day, like_count, sentiment, sentiment_confidence, body{choice_cols}
        FROM one WHERE {_where(min_p)}
        ORDER BY like_count DESC, id
        LIMIT {TOP_POSTS}
    """, _params(min_p)).fetchall()
    pool = con.execute(f"SELECT count(DISTINCT id) FROM one WHERE {_where(min_p)}",
                       _params(min_p)).fetchone()[0]
    points = []
    for row in rows:
        point = {
            "x": row[0], "y": int(row[2]), "id": row[0], "day": str(row[1]), "like_count": int(row[2]),
            "sentiment": round(row[3], ROUND) if row[3] is not None else None,
            "sentiment_confidence": round(row[4], ROUND) if row[4] is not None else None,
            "body": row[5] or "",
        }
        for i, q in enumerate(questions):
            point[q["name"]] = row[6 + i]
        points.append(point)
    window = (spec.get("observation") or {}).get("window") or {}
    chart = _chart(
        "top_posts", "table", "Most liked relevant posts",
        {"field": "id", "type": "string"}, {"field": "like_count", "unit": "likes"},
        [{"name": "top posts", "points": points}],
        spec, min_p, "relevant scored posts in the window", day_over_day=False,
        drilldown={"tool": "get_posts", "args_template": {"from": window.get("from"), "to": window.get("to"), "sort": "engagement"}},
    )
    chart["annotations"] = [a for a in chart["annotations"] if a["kind"] != "low_n"]
    if int(pool) < LOW_N:
        chart["annotations"].append({"x": None, "kind": "low_n", "n": int(pool)})
    return chart


def build_all(project_dir, repo_root=None, denominators=None, png=False):
    project_dir = Path(project_dir)
    spec = _load(project_dir / "spec.json")
    if "observation" not in spec and isinstance(spec.get("spec"), dict):
        spec = spec["spec"]
    run = _load(project_dir / "run.json")
    posts = project_dir / "posts.parquet"
    if denominators is None:
        denominators = Path(repo_root or ".") / "topic-analysis" / "daily-denominators.csv"

    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")  # results are tiny, but the session TZ still decides what `day` means
    columns = _columns(con, posts)
    day_expr = "CAST(day AS DATE)" if "day" in columns else "CAST(created_at AS DATE)"
    gate = spec.get("relevance_gate") or None
    min_p = float(gate.get("min_probability", 0.5)) if gate and "relevant_p" in columns else None
    if gate and min_p is None:
        print("charts: spec has a relevance gate but posts.parquet has no relevant_p; no gate applied", file=sys.stderr)
    questions = [q for q in _choice_questions(spec) if q["name"] in columns]
    _materialize(con, posts, columns, day_expr, questions)

    per_day = con.execute("SELECT day, count(DISTINCT id) FROM one GROUP BY day").fetchall()
    fallback_counts = {str(day): int(n) for day, n in per_day}
    days = _days(spec, run, fallback_counts)
    sampled = {str(k): v for k, v in (run.get("sampled_fraction_by_day") or {}).items()}

    charts = [
        volume_daily(spec, run, days, denominators, sampled, fallback_counts, min_p),
        sentiment_daily(con, spec, days, sampled, min_p),
    ]
    for question in questions:
        charts.append(share_daily(con, spec, question, days, sampled, min_p))
    for question in questions:
        charts.append(totals(con, spec, question, min_p))
    charts.append(top_posts(con, spec, questions, min_p))
    con.close()

    out = project_dir / "charts"
    out.mkdir(parents=True, exist_ok=True)
    for chart in charts:
        (out / f"{chart['chart_id']}.json").write_text(json.dumps(chart, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if png:
            render_png(chart, out / f"{chart['chart_id']}.png")
    # A rebuild after a question was renamed or dropped must not leave the old chart behind: get_chart_data
    # serves whatever is in this directory, and stale numbers read as current results.
    built = {chart["chart_id"] for chart in charts}
    for path in [*out.glob("*.json"), *out.glob("*.png")]:
        if path.stem not in built or (path.suffix == ".png" and not png):
            path.unlink()
    return charts


def list_charts(project_dir):
    order = ["volume_daily", "sentiment_daily", "share_daily__", "totals__", "top_posts"]
    rank = lambda cid: next((i for i, prefix in enumerate(order) if cid.startswith(prefix)), len(order))  # noqa: E731
    out = []
    for path in sorted((Path(project_dir) / "charts").glob("*.json")):
        chart = _load(path)
        out.append({"chart_id": chart["chart_id"], "title": chart["title"], "type": chart["type"]})
    return sorted(out, key=lambda c: (rank(c["chart_id"]), c["chart_id"]))


# --- rendering: Figure + FigureCanvasAgg only.  pyplot keeps global state and is not thread-safe,
# --- and build_all runs inside the MCP server's worker threads.
def _style(ax, chart, rows=None):
    ax.set_facecolor(SURFACE)
    ax.set_title(chart["title"], color=INK, fontsize=11, loc="left", pad=10)
    ax.grid(axis="y" if rows is None else "x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=8, length=0)
    unit = (chart.get("y") or {}).get("unit")
    if unit and rows is None:
        ax.set_ylabel(unit, color=MUTED, fontsize=8)


def _figure(width=7.2, height=3.6):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    figure = Figure(figsize=(width, height), dpi=160, facecolor=SURFACE)
    FigureCanvasAgg(figure)
    return figure


def _day_ticks(ax, labels):
    step = max(1, len(labels) // 10)
    ax.set_xticks(range(0, len(labels), step))
    ax.set_xticklabels([labels[i][5:] for i in range(0, len(labels), step)])


def render_png(chart, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    series = chart.get("series") or []
    points = series[0]["points"] if series else []
    if chart["type"] == "table":
        figure = _figure(height=max(2.0, 0.26 * len(points) + 1.1))
        ax = figure.add_subplot(111)
        _style(ax, chart, rows=True)
        ys = list(range(len(points)))[::-1]
        ax.barh(ys, [p.get("y") or 0 for p in points], height=0.62, color=INK)
        ax.set_yticks(ys)
        ax.set_yticklabels([f"{p.get('id', p['x'])}  {(p.get('body') or '')[:34]}" for p in points], fontsize=6)
        ax.set_xlabel((chart.get("y") or {}).get("unit", ""), color=MUTED, fontsize=8)
        figure.subplots_adjust(left=0.46, right=0.98, top=0.86, bottom=0.12)
    elif chart["type"] == "bar":
        figure = _figure(height=3.2)
        ax = figure.add_subplot(111)
        _style(ax, chart)
        values = [p.get("y") or 0 for p in points]
        xs = range(len(points))
        ax.bar(xs, values, width=0.62, color=[SERIES_COLORS[i % len(SERIES_COLORS)] for i in xs])
        ax.set_xticks(list(xs))
        ax.set_xticklabels([str(p["x"]) for p in points], fontsize=8)
        for i, value in enumerate(values):
            ax.annotate(f"{value:,}", (i, value), textcoords="offset points", xytext=(0, 4),
                        ha="center", color=MUTED, fontsize=7)
        ax.margins(y=0.16)
        figure.subplots_adjust(left=0.1, right=0.98, top=0.86, bottom=0.16)
    else:
        figure = _figure()
        ax = figure.add_subplot(111)
        _style(ax, chart)
        labels = [str(p["x"]) for p in points]
        for i, one in enumerate(series):
            color = INK if len(series) == 1 else SERIES_COLORS[i % len(SERIES_COLORS)]
            ys = [float("nan") if p.get("y") is None else p["y"] for p in one["points"]]
            ax.plot(range(len(ys)), ys, color=color, linewidth=2, marker="o", markersize=3.6,
                    markeredgecolor=SURFACE, markeredgewidth=0.8, label=one["name"])
        _day_ticks(ax, labels)
        for annotation in chart.get("annotations") or []:
            if annotation["kind"] == "largest_change" and annotation["x"] in labels:
                ax.axvline(labels.index(annotation["x"]), color=GRID, linewidth=6, zorder=0)
        bottom = 0.16
        if len(series) > 1:
            ax.legend(frameon=False, fontsize=7, ncol=min(4, len(series)), loc="upper center",
                      bbox_to_anchor=(0.5, -0.12), labelcolor=MUTED)
            bottom = 0.3
        figure.subplots_adjust(left=0.1, right=0.98, top=0.86, bottom=bottom)
    figure.savefig(str(path), format="png", facecolor=SURFACE, edgecolor="none")
    return str(path)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    project = Path(args[0]) if args else Path("harness/demo/projects/demo-playstation")
    root = Path(args[1]) if len(args) > 1 else Path(__file__).resolve().parent.parent
    built = build_all(project, root, png="--png" in sys.argv)
    for chart in built:
        print(f"{chart['chart_id']:28s} {chart['type']:6s} {len(chart['series'][0]['points'])} points  "
              f"{len(chart['annotations'])} annotations", flush=True)
    print(f"wrote {len(built)} charts to {project / 'charts'}", flush=True)
