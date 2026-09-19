# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.11,<4", "anthropic>=0.75", "mcp>=2", "duckdb>=1.4,<2", "pytz"]
# ///
"""Run: python -m uv run harness/tests/test_signal_server.py   (SIGNAL_TEST_STEPS=1,2,... to pick steps)

Drives the combined server over real HTTP on port 5197 against a temporary data folder, so the
product running on 5194 and its collected posts are never touched.  Step 9 spends one real Claude
Code turn on the subscription; step 6 starts a real brief and cancels it immediately, and
ELEVENLABS_API_KEY is removed from the environment, so no voice credit is spent.
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 5197
BASE = f"http://127.0.0.1:{PORT}"
DEAD = "http://127.0.0.1:5991"  # nothing listens here
TEMP = Path(tempfile.mkdtemp(prefix="signal-test-"))
SECOND = Path(tempfile.mkdtemp(prefix="signal-test-second-"))
STEPS = {step for step in (os.environ.get("SIGNAL_TEST_STEPS") or "1,2,3,4,5,6,7,8,9").split(",")}
OUTCOMES = []

os.environ.update(PORT=str(PORT), BACKFILL_HOURS="0.05", RETAIN_HOURS="2", BACKFILL_CONNECTIONS="2",
                  SIGNAL_DATA=str(TEMP), SIGNAL_BASE_URL=BASE)
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HARNESS_REPO", str(ROOT.parent))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

import brief_tools  # noqa: E402
import signal_server  # noqa: E402

server = signal_server.server
server.DATA, server.BRIEFS = TEMP, TEMP / "briefs"  # the lifecycle reads these at call time, so the live data folder is never opened
os.environ.pop("ELEVENLABS_API_KEY", None)  # after the import: morning-brief/.env is read there, and a test must not buy audio
FAKE = "at://did:plc:testfixture/app.bsky.feed.post/"


def check(name, ok, detail=""):
    OUTCOMES.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""), flush=True)  # ASCII: the Windows console is cp1252


async def page(client):
    async with client.get(BASE + "/") as response:
        status_code, html = response.status, await response.text()
    on_disk = server.ASSETS["/"][0].read_text(encoding="utf-8")
    inline = [body for body in re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S) if body.strip()]
    check("1 the home page is Morning Brief with the chat widget injected",
          status_code == 200 and 'id="interest-form"' in html and 'id="make"' in html and "/app.js" in html
          and all(marker in html for marker in signal_server.MARKERS) and 'id="launcher"' in html and 'id="chat-panel"' in html
          and 'href="/chat.css"' in html and 'src="/chat.js"' in html and not inline,
          f"{len(html)} chars, {html.count('<script')} script tags, {len(inline)} with a body")
    check("1 morning-brief/web/index.html is untouched on disk", "chat.js" not in on_disk and "chat-widget" not in on_disk,
          f"{len(on_disk)} chars, injected {len(html) - len(on_disk)} chars per request")
    # chat.js looks its elements up by id and would throw on a missing one, taking the page's own script with it.
    wanted = set(re.findall(r"\$\('([a-z0-9-]+)'\)", (ROOT / "web/chat.js").read_text(encoding="utf-8")))
    missing = sorted(wanted - set(re.findall(r'id="([a-z0-9-]+)"', html)))
    check("1 the page has every element chat.js looks up", bool(wanted) and not missing, f"{len(wanted)} ids in chat.js, missing {missing}")


async def degrades(client):
    """harness/web/ is edited live: a landing page that is missing or marker-less costs the widget, never the page."""
    async def get(path):
        async with client.get(BASE + path) as response:
            return response.status, await response.text()

    real_web, real_widget = signal_server.WEB, signal_server.WIDGET
    spare = Path(tempfile.mkdtemp(prefix="signal-test-web-"))
    try:
        (spare / "index.html").write_text("<p>a landing page without the markers</p>", encoding="utf-8")
        signal_server.WEB = spare
        signal_server.WIDGET = {path: (spare / Path(path).name, kind) for path, (_, kind) in real_widget.items()}
        unmarked, without_widget = await get("/")
        shutil.rmtree(spare)
        absent, plain = await get("/")
        missing, said = await get("/chat.js")
    finally:
        signal_server.WEB, signal_server.WIDGET = real_web, real_widget
        shutil.rmtree(spare, ignore_errors=True)
    back, whole = await get("/")
    check("1 an unreadable harness/web costs the widget, not the home page", unmarked == 200 and absent == 200
          and all('id="interest-form"' in html and "launcher" not in html and "chat.js" not in html for html in (without_widget, plain))
          and back == 200 and 'id="launcher"' in whole and 'src="/chat.js"' in whole,
          f"no markers -> {unmarked}, landing page absent -> {absent}, restored -> {back} with the widget")
    check("1 a missing widget asset is a 404, not a 500", missing == 404 and "error" in said, f"/chat.js while harness/web is gone -> {missing} {said[:40]}")


async def assets(client):
    found = {}
    for path in ("/app.js", "/brief.css", "/signal.css", "/chat.js", "/chat.css"):
        async with client.get(BASE + path) as response:
            found[path] = (response.status, response.content_type, len(await response.read()))
    check("2 every asset of both apps is served", all(status == 200 and size > 500 for status, _, size in found.values()),
          ", ".join(f"{path} {status} {kind} {size}b" for path, (status, kind, size) in found.items()))


async def collector_live(client, seconds=60):
    deadline, status = time.monotonic() + seconds, {}
    while time.monotonic() < deadline:
        async with client.get(BASE + "/api/status") as response:
            status = await response.json()
        if (status.get("live") or {}).get("state") == "live":
            break
        await asyncio.sleep(0.5)
    check("3 /api/status works and the collector reaches live", (status.get("live") or {}).get("state") == "live"
          and status["source"]["network"] == "Bluesky" and [i["id"] for i in status["interests"]] == ["ai"]
          and status["min_seconds"] and status["max_seconds"],
          f"{status.get('live', {}).get('scanned')} scanned, {status.get('posts')} kept, backfill {bool(status.get('backfill'))}")


async def origins(client):
    async with client.post(BASE + "/api/sessions", json={}) as response:
        allowed, body = response.status, await response.json()
    async with client.post(BASE + "/api/sessions", json={}, headers={"Origin": "http://evil.example"}) as response:
        refused_chat = response.status
    async with client.post(BASE + "/api/collector", json={"action": "start"}, headers={"Origin": "http://evil.example"}) as response:
        refused_brief = response.status
    check("4 both same-origin checks hold on the combined origin", allowed == 200 and body.get("session_id")
          and refused_chat == 403 and refused_brief == 403, f"chat session {body.get('session_id', '')[:8]}, foreign origin {refused_chat}/{refused_brief}")
    return body.get("session_id")


def seed():
    """Three posts straight into the collector's memory, through the app object: no network, no waiting."""
    store = signal_server.app["collector"].store
    now = int(time.time() * 1000)
    rows = [("a", "Anthropic shipped a new AI model today and Zorblatt reactions are everywhere.", 1),
            ("b", "He said the quiet part out loud about Zorblatt rain.", 2),
            ("c", "Matcha and oolong tea at the AI Zorblatt cafe.", 3)]  # seconds old: real AI posts keep arriving, and these must stay in the newest page
    for rkey, text, ago in rows:
        store.add({"uri": FAKE + rkey, "did": "did:plc:testfixture", "rkey": rkey, "t": now - ago * 1000, "topics": ["ai"],
                   "text": text, "langs": ["en"], "parent": None, "quote": None, "link": None})
    return [FAKE + rkey for rkey, _, _ in rows]


async def searching(client, uris):
    async def query(text):
        async with client.get(f"{BASE}/api/posts/search?{text}") as response:
            return response.status, await response.json()
    _, marker = await query("q=Zorblatt&hours=1&limit=50")
    _, lower = await query("q=zorblatt&hours=1")
    _, partial = await query("q=Zorblat&hours=1")
    _, capped = await query("q=Zorblatt&hours=1&limit=1")
    _, acronym = await query("q=AI&hours=1&limit=50")
    _, scoped = await query("q=Zorblatt&hours=1&interest=ai")
    refused = [(await query(text))[0] for text in ("q=&hours=1", "q=Zorblatt&hours=99", "q=Zorblatt&limit=0", "q=Zorblatt&interest=nope")]
    matched = {post["uri"] for post in acronym["posts"]}
    first = (marker["posts"] or [{}])[0]
    check("5 search matches whole words, newest first", [post["uri"] for post in marker["posts"]] == uris and marker["total"] == 3
          and lower["total"] == 3 and partial["total"] == 0 and capped["total"] == 3 and capped["returned"] == 1
          and scoped["total"] == 3, f"{marker['total']} for Zorblatt, {lower['total']} lowercase, {partial['total']} for a prefix")
    check('5 "AI" matches the acronym and not "said"', uris[0] in matched and uris[2] in matched and uris[1] not in matched,
          f"{len(matched)} posts match AI ({acronym['total']} in the window), the one saying 'said' is {'in' if uris[1] in matched else 'out'}")
    check("5 a hit carries everything a citation needs", set(first) >= {"uri", "url", "text", "topics", "langs", "time", "t"}
          and first["url"] == "https://bsky.app/profile/did:plc:testfixture/post/a" and len(first["text"]) <= 280
          and first["topics"] == ["ai"] and marker["kept_posts"] >= 3,
          f"{sorted(first)}, kept_posts {marker['kept_posts']}")
    check("5 bad queries are refused", refused == [400, 400, 400, 404], f"empty q, 99 hours, limit 0, unknown interest -> {refused}")


async def tools():
    brief_tools.register(MCPServer("signal-test"), base_url=BASE)  # a throwaway server: the functions are called directly below
    overview = await asyncio.to_thread(brief_tools.brief_overview)
    check("6a brief_overview reports the product's own state", overview.get("posts_kept", 0) >= 3
          and [interest["id"] for interest in overview["interests"]] == ["ai"] and overview["collector"]["state"] in ("live", "replaying")
          and overview["brief_being_made"] is None and overview["voices"] is False and overview["brief_defaults"]["min_seconds"] == 45,
          f"{overview['collector']['state']}, {overview['posts_kept']} kept, interests {[i['name'] for i in overview['interests']]}")

    followed = await asyncio.to_thread(brief_tools.follow_interest, "Tea: oolong, matcha")
    interest_id = (followed.get("interest") or {}).get("id")
    searched = await asyncio.to_thread(brief_tools.search_collected, ["Zorblatt"], 1, 5)
    check("6b follow_interest takes explicit terms", interest_id == "tea" and followed["interest"]["terms"] == ["oolong", "matcha"], str(followed.get("interest")))
    check("6c search_collected reaches the new endpoint", searched.get("total") == 3 and len(searched["posts"]) == 3
          and searched["posts"][0]["url"].startswith("https://bsky.app/profile/"), f"{searched.get('total')} hits, {searched.get('kept_posts')} kept")

    stopped = await asyncio.to_thread(brief_tools.collector_control, "stop")
    started = await asyncio.to_thread(brief_tools.collector_control, "start")
    check("6d collector_control stops and starts collecting", stopped.get("paused") is True and stopped["collector"]["state"] == "paused"
          and started.get("paused") is False and started["collector"]["state"] in ("live", "connecting", "replaying")
          and (await asyncio.to_thread(brief_tools.collector_control, "sideways")).get("error", {}).get("code") == "bad_action",
          f"stop -> {stopped['collector']['state']}, start -> {started['collector']['state']}")

    dropped = await asyncio.to_thread(brief_tools.unfollow_interest, interest_id)
    missing = await asyncio.to_thread(brief_tools.unfollow_interest, "nothing-like-this")
    check("6e unfollow_interest drops it, and an unknown id is an error", dropped.get("unfollowed") == "tea"
          and missing.get("error", {}).get("code") == "no_such_interest", f"{dropped.get('name')} removed; unknown id -> {missing.get('error', {}).get('code')}")

    lengthy = await asyncio.to_thread(brief_tools.make_brief, 1, 240)
    made = await asyncio.to_thread(brief_tools.make_brief, 1, 60)
    task = signal_server.app["state"]["task"]
    if task:
        task.cancel()  # a test buys neither a script nor a recording; the id is what is being tested
        await asyncio.gather(task, return_exceptions=True)
    error = made.get("error", {})
    check("6f make_brief guards the paid length and starts a brief", lengthy.get("error", {}).get("code") == "length_not_requested"
          and (re.fullmatch(r"\d{8}-\d{6}", made.get("brief_id") or "") or error.get("message")),
          f"240 s -> {lengthy.get('error', {}).get('code')}; 60 s -> {made.get('brief_id') or error.get('code') + ': ' + error.get('message', '')[:60]}")

    known = await asyncio.to_thread(brief_tools.get_brief, made["brief_id"]) if made.get("brief_id") else {}
    unknown = await asyncio.to_thread(brief_tools.get_brief, "no-such-brief")
    check("6g get_brief reports a real brief and refuses an unknown id",
          (not made.get("brief_id") or (known.get("brief_id") == made["brief_id"] and known["status"] in ("working", "failed", "ready")
                                        and set(known) >= {"status", "step", "title", "audio_url", "segments", "spoken_seconds"}))
          and unknown.get("error", {}).get("code") == "not_found",
          f"{known.get('status')} / {str(known.get('step'))[:40]!r}; unknown -> {unknown.get('error', {}).get('message', '')[:60]}")


def dead_server():
    started = time.monotonic()
    brief_tools.register(MCPServer("signal-dead"), base_url=DEAD)  # last: it repoints the module at a port nothing listens on
    answer = brief_tools.brief_overview()
    elapsed = time.monotonic() - started
    check("7 a dead server becomes a structured error, quickly", answer.get("error", {}).get("code") == "unreachable"
          and set(answer["error"]) == {"code", "message", "hint"} and elapsed < 5,
          f"{elapsed:.2f}s, {answer.get('error', {}).get('message', '')[:70]}")


async def second_instance(**environment):
    child = await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / "signal_server.py"), cwd=str(ROOT.parent), env={**os.environ, **environment},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(child.communicate(), 90)
    except asyncio.TimeoutError:  # a guard that regressed leaves a second collector running: kill it, and fail this check instead of the run
        child.kill()
        out, err = await child.communicate()
        return 0, "it was still running after 90 s", (out + err).decode("utf-8", "replace")
    out, err = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    return child.returncode, (err or out).strip(), out + err


async def port_guard():
    """A second instance on a port in use must die before its collector can open the data folder."""
    code, said, _ = await second_instance(SIGNAL_DATA=str(SECOND))
    check("8 a second instance on the same port refuses to start", code != 0 and str(PORT) in said and not any(SECOND.iterdir())
          and "another port" not in said.lower(),  # moving ports is the one thing that must not be suggested: the folder is what is at stake
          f"exit {code}, nothing written to its data folder, said: {said.splitlines()[-1][:110] if said else ''}")


async def folder_guard():
    """The port guard only guards one port; a second instance on a free port must still refuse this data folder."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    code, said, everything = await second_instance(PORT=str(free), SIGNAL_DATA=str(TEMP))
    # "Running on" is run_app's own banner: the folder's lock is taken before server.lifecycle, so the child died before any Store opened posts.jsonl.
    check("8 a second instance on a free port still refuses this data folder", code != 0 and str(TEMP) in said
          and "stop that one first" in said and "Running on" not in everything,
          f"exit {code} on port {free}, never started serving, said: {said.splitlines()[-1][:110] if said else ''}")


async def one_turn(client, session_id):
    events = []
    async with client.post(f"{BASE}/api/sessions/{session_id}/messages", json={"text": "What data can I observe here?"}) as response:
        if response.status != 200:
            return check("9 one real chat turn streams through the combined server", False, f"HTTP {response.status}: {(await response.text())[:120]}")
        async for raw in response.content:
            line = raw.decode("utf-8").strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    kinds = [event["type"] for event in events]
    check("9 one real chat turn streams through the combined server", kinds.count("delta") >= 1 and "message" in kinds and kinds[-1] == "done",
          f"{len(events)} events: {', '.join(sorted(set(kinds)))}; said {sum(len(e.get('text', '')) for e in events if e['type'] == 'message')} chars")


async def main():
    runner = web.AppRunner(signal_server.app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()
    async with aiohttp.ClientSession(headers={"Origin": BASE}, timeout=aiohttp.ClientTimeout(total=420)) as client:
        try:
            session_id = None
            if "1" in STEPS:
                await page(client)
                await degrades(client)
            if "2" in STEPS:
                await assets(client)
            if "3" in STEPS:
                await collector_live(client)
            if "4" in STEPS:
                session_id = await origins(client)
            if "5" in STEPS:
                await searching(client, seed())
            if "6" in STEPS:
                await tools()
            if "7" in STEPS:
                await asyncio.to_thread(dead_server)
            if "8" in STEPS:
                await port_guard()
                await folder_guard()
            if "9" in STEPS:
                await one_turn(client, session_id or await origins(client))
        finally:
            await runner.cleanup()
    for directory in (TEMP, SECOND):
        shutil.rmtree(directory, ignore_errors=True)
    print(f"\n{sum(OUTCOMES)}/{len(OUTCOMES)} checks passed", flush=True)
    return 0 if all(OUTCOMES) and OUTCOMES else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
