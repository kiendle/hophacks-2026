"""The one route the analysis cards need: the picture of a chart, as a PNG.

setup(app) is called by bridge.attach (and so by the combined server too), which is why this file
adds routes and nothing else. The numbers themselves reach the model through analysis_tools.py; this
is only the image the page shows beside them.

Rendering is matplotlib work, so it happens in a worker thread and the result is kept in
harness/state/cache/analysis/. The id in the address is checked twice before any path is built from
it: the project must exist under harness/demo/projects/ and the chart must be one of the charts that
project really has. The server's own interpreter may have no matplotlib (bridge.py does not ask for
it), so a render that cannot import it is done once in a small uv child process instead.
"""
import asyncio
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from aiohttp import web

import analysis_tools
import charts
import pipeline

CACHE = analysis_tools.HARNESS / "state" / "cache" / "analysis"
RENDER_TIMEOUT_S = 120
# One lock per chart: a page that asks for the same picture eight times while it is still being
# drawn would otherwise start eight renders, and on a server without matplotlib eight child processes.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
# charts.py imports duckdb, and render_png imports matplotlib only when it draws, so the child needs both.
CHILD = ("import json, os, sys\n"
         "sys.path.insert(0, os.environ['HARNESS_DIR'])\n"
         "import charts\n"
         "charts.render_png(json.loads(open(os.environ['CHART_JSON'], encoding='utf-8').read()), os.environ['CHART_PNG'])\n")


def _chart_json(project_id: str, chart_id: str) -> Path:
    """The chart file this address means, or a PipelineError the caller turns into a 404.

    Both ids are checked against what exists, never only against a pattern: the project has to have a
    spec, and the chart has to be one charts.list_charts reports for it.
    """
    directory = analysis_tools._project_dir(project_id)  # raises unknown_project
    if not analysis_tools.CHART_ID.fullmatch(chart_id or ""):
        raise pipeline.PipelineError("unknown_chart", f"There is no chart called {str(chart_id)[:60]!r}.",
                                     "Call list_charts for the charts this project has.")
    known = {chart["chart_id"] for chart in charts.list_charts(directory)}
    if chart_id not in known:
        raise pipeline.PipelineError("unknown_chart", f"There is no chart called {chart_id!r} in this project.",
                                     f"It has: {', '.join(sorted(known)) or 'no charts yet'}.")
    return directory / "charts" / f"{chart_id}.json"


def _cached(project_id: str, chart_id: str) -> Path:
    path = (CACHE / project_id / f"{chart_id}.png").resolve()
    if CACHE.resolve() not in path.parents:  # belt and braces: nothing may be written outside the cache
        raise pipeline.PipelineError("unknown_chart", "That chart cannot be drawn.", "Call list_charts.")
    return path


def _fresh(png: Path, source: Path) -> bool:
    try:
        return png.stat().st_size > 0 and png.stat().st_mtime >= source.stat().st_mtime
    except OSError:
        return False


def _render_elsewhere(source: Path, out: Path) -> None:
    """One uv child process with matplotlib, for a server whose own environment has none."""
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("matplotlib is missing in this interpreter and uv is not on the path")
    child = subprocess.run(
        [uv, "run", "--quiet", "--no-project", "--with", "matplotlib>=3.9", "--with", "duckdb>=1.4,<2", "-"],
        input=CHILD, text=True, capture_output=True, timeout=RENDER_TIMEOUT_S,
        env={**os.environ, "HARNESS_DIR": str(analysis_tools.HARNESS), "CHART_JSON": str(source), "CHART_PNG": str(out)})
    if child.returncode != 0 or not out.exists():
        raise RuntimeError(f"drawing the chart failed: {(child.stderr or child.stdout or '').strip()[-300:]}")


def render(source: Path, out: Path) -> Path:
    """Draw one chart into the cache. Two of these can run at once, so the file appears atomically."""
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_name(f"{out.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
    chart = json.loads(source.read_text(encoding="utf-8"))
    try:
        try:
            charts.render_png(chart, temp)
        except ImportError:
            _render_elsewhere(source, temp)
        os.replace(temp, out)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)
    return out


def _lock_for(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _bytes(project_id: str, chart_id: str) -> bytes:
    source = _chart_json(project_id, chart_id)
    png = _cached(project_id, chart_id)
    if not _fresh(png, source):
        with _lock_for(f"{project_id}/{chart_id}"):
            if not _fresh(png, source):  # somebody else drew it while this call was waiting
                render(source, png)
    return png.read_bytes()


async def chart_png(request):
    project_id = request.match_info["project"]
    chart_id = request.match_info["chart"]
    try:
        body = await asyncio.to_thread(_bytes, project_id, chart_id)
    except pipeline.PipelineError as error:
        return web.Response(status=404, text=f"{error.message}\n", content_type="text/plain", headers=HEADERS)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"analysis_api: {project_id}/{chart_id}.png could not be drawn: {error!r}", flush=True)
        return web.Response(status=500, text="That chart could not be drawn.\n", content_type="text/plain", headers=HEADERS)
    return web.Response(body=body, content_type="image/png", headers=HEADERS)


def setup(app):
    app.add_routes([web.get("/api/projects/{project:[a-z0-9_-]{1,60}}/charts/{chart:[a-z0-9_-]{1,60}}.png", chart_png)])
    return app
