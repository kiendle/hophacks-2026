# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.11,<4", "mcp>=2", "duckdb>=1.4,<2", "pytz"]
# ///
"""Run: python -m uv run harness/tests/test_plugins.py

The five plug-in points four streams of work share, each proved with throwaway modules written into a
temporary folder that is put on sys.path: optional route modules (bridge), optional tool modules
(demo_mcp_server), the step wording registry (steps), the card event and the prompt fragments
(claude_runner), and the browser's own exports (chat.js). Nothing here spends a model turn.

A module that is absent must cost nothing and a module that is broken must cost only itself: both are
checked, because tonight four people are editing four different files at once.
"""
import asyncio
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 5203  # not 5195 (the live chat) and not 5194 (the product)
BASE = f"http://127.0.0.1:{PORT}"
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HARNESS_REPO", str(ROOT.parent))
sys.stdout.reconfigure(errors="replace")  # the Windows console is cp1252

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

import brief_tools  # noqa: E402
import bridge  # noqa: E402
import claude_runner  # noqa: E402
import demo_mcp_server as tools  # noqa: E402
import steps  # noqa: E402

OUTCOMES = []
TEMP = Path(tempfile.mkdtemp(prefix="signal-plugins-"))
BANNED = "‒–—―·•‣▪・←→↔⇒⇨➡;"

# Named so that no real module can ever shadow them: harness/voice.py and harness/analysis_api.py are
# imported by bridge.py at import time, long before this folder reaches sys.path, so a throwaway
# called "voice" would silently be the real one. The addresses they serve are still the real ones,
# because the body limit is decided by the address and not by the module's name.
MODULES = {
    "probe_voice_routes": """
from aiohttp import web

def setup(app):
    async def ping(request):
        return web.json_response({"voice": "here"})

    async def clip(request):
        return web.json_response({"bytes": len(await request.read())})

    app.add_routes([web.get("/api/voice/ping", ping), web.post("/api/voice/clip", clip)])
""",
    "probe_analysis_routes": """
from aiohttp import web

def setup(app):
    async def ping(request):
        return web.json_response({"analysis": "here"})

    app.add_routes([web.get("/api/analysis/ping", ping)])
""",
    # A module of its own for 2b: aiohttp refuses the same method on the same address twice, so the
    # module that proves "the next one still loads" must not be one 2a has already put on this app.
    "probe_more_routes": """
from aiohttp import web

def setup(app):
    async def ping(request):
        return web.json_response({"more": "here"})

    app.add_routes([web.get("/api/analysis/more", ping)])
""",
    "probe_broken_api": "import there_is_no_such_module\n\ndef setup(app):\n    pass\n",
    "probe_no_setup_api": "VALUE = 1\n",
    "probe_fake_tools": '''
import steps

def register(mcp):
    @mcp.tool()
    def fake_probe(reason: str, words: list[str]) -> dict:
        """A throwaway tool. reason: one short plain sentence for the user."""
        return {"found": len(words), "_card": {"kind": "probe", "words": words}}

    steps.register_tool(
        "fake_probe",
        lambda fields: "Looking for " + steps.word_list(fields.get("words") or [], 3),
        lambda result, is_error: "" if is_error else f"Found {result.get('found')} of them.",
        lambda fields: [{"label": "Words searched", "value": steps.word_list(fields.get("words") or [], 8)}],
    )
''',
    "probe_broken_tools": "raise RuntimeError('this module is broken on purpose')\n",
}
FRAGMENTS = {"aa-test-plugins.md": "First fragment: the analysis tools are for counting.",
             "zz-test-plugins.md": "Last fragment: the voice tools speak the answer."}


def check(name, ok, detail=""):
    OUTCOMES.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""), flush=True)  # ASCII: the Windows console is cp1252


def write_modules():
    for name, source in MODULES.items():
        (TEMP / f"{name}.py").write_text(source, encoding="utf-8")
    sys.path.insert(0, str(TEMP))


def caught(function, *arguments, **keywords):
    """What a loader printed, so "a broken module is skipped loudly" can be checked, not assumed."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        answer = function(*arguments, **keywords)
    return answer, out.getvalue() + err.getvalue()


# ------------------------------------------------------------------ nothing there
async def absent():
    app = web.Application()
    loaded, said = caught(bridge.load_plugins, app, ("no_such_route_module",))
    check("1a a route module that is not there loads nothing and says nothing", loaded == [] and said == "", repr(said[:80]))
    server = MCPServer("absent")
    added, quiet = caught(tools.load_plugins, server, ("no_such_tool_module",))
    check("1b a tool module that is not there loads nothing and says nothing", added == [] and quiet == "", repr(quiet[:80]))
    check("1c an unregistered tool still reads plainly",
          (steps.title("mcp__harness__whatever_this_is", {"x": 1}), steps.outcome("whatever_this_is", {"rows": 2}),
           steps.facts("whatever_this_is", {"x": 1})) == ("Working on it", "Done.", []),
          f'{steps.title("whatever_this_is", {})} / {steps.outcome("whatever_this_is", {})}')
    before = [tool.name for tool in await tools.mcp.list_tools()]
    check("1d importing the tool server alone leaves its own seven tools", len(before) == 7 and "fake_probe" not in before, ", ".join(before))


# ------------------------------------------------------------------ routes
def routes():
    app = bridge.attach(web.Application(middlewares=[bridge.local_only], client_max_size=64 * 1024))
    app.add_routes([web.get("/", bridge.asset), web.get("/{name:.*}", bridge.asset)])
    # realtime_api is being written in another stream: whether it is there yet is not this test's business.
    check("2a bridge.attach loads every route module that is there",
          set(app["plugins"]) >= {"voice", "analysis_api"} and set(app["plugins"]) <= set(bridge.PLUGINS), str(app["plugins"]))
    added, _ = caught(bridge.load_plugins, app, ("probe_voice_routes", "probe_analysis_routes"))
    check("2a the throwaway modules of this test load too", added == ["probe_voice_routes", "probe_analysis_routes"], str(added))
    broken, said = caught(bridge.load_plugins, app, ("probe_broken_api", "probe_no_setup_api", "probe_more_routes"))
    check("2b a broken route module is skipped with its traceback, a module without setup is skipped, and the next one still loads",
          broken == ["probe_more_routes"] and "Traceback" in said and "there_is_no_such_module" in said
          and "probe_no_setup_api.py has no setup(app)" in said,
          f"loaded {broken}, printed {len(said)} characters")
    return app


async def served(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()
    try:
        async with aiohttp.ClientSession(headers={"Origin": BASE}, timeout=aiohttp.ClientTimeout(total=30)) as client:
            await talk(client)
    finally:
        await runner.cleanup()


async def talk(client):
    async with client.get(BASE + "/api/voice/ping") as response:
        voice = (response.status, await response.json())
    async with client.get(BASE + "/api/analysis/ping") as response:
        analysis = (response.status, await response.json())
    check("3a both modules' routes answer on the chat server", voice == (200, {"voice": "here"}) and analysis == (200, {"analysis": "here"}),
          f"{voice} and {analysis}")

    async with client.post(BASE + "/api/voice/clip", data=b"x" * (2 * 1024 * 1024)) as response:
        clip = (response.status, await response.json())
    async with client.post(BASE + "/api/sessions/nope/messages", data=json.dumps({"text": "y" * 200_000}),
                           headers={"Content-Type": "application/json"}) as response:
        chat = response.status
    check("3b only the voice routes take a big body, the chat composer keeps its own small limit",
          clip == (200, {"bytes": 2 * 1024 * 1024}) and chat == 413, f"voice {clip[0]} for 2 MB, chat {chat} for 200 KB")

    probes = {"plugins-probe.png": b"\x89PNG\r\n\x1a\n" + b"0" * 64, "plugins-probe.mp3": b"ID3" + b"0" * 64}
    for name, body in probes.items():
        (bridge.WEB / name).write_bytes(body)
    try:
        found = {}
        for path in ("/chat.js", "/chat.css", "/plugins-probe.png", "/plugins-probe.mp3", "/signal.css"):
            async with client.get(BASE + path) as response:
                found[path] = (response.status, response.content_type, response.charset, len(await response.read()))
    finally:
        for name in probes:
            (bridge.WEB / name).unlink(missing_ok=True)
    check("3c a file of any name under harness/web is served with its own content type",
          [found[path][:2] for path in found] == [(200, "text/javascript"), (200, "text/css"), (200, "image/png"), (200, "audio/mpeg"), (200, "text/css")]
          and found["/plugins-probe.png"][2] is None and found["/chat.js"][2] == "utf-8",
          ", ".join(f"{path} {status} {kind}" for path, (status, kind, _, _) in found.items()))

    async with client.get(BASE + "/chat.js") as response:
        headers = dict(response.headers)
    policy = headers.get("Content-Security-Policy", "")
    check("3d the page may record audio and nothing more",
          "media-src 'self' blob:" in policy and "default-src 'self'" in policy and "script-src 'self'" in policy
          and "style-src 'self'" in policy and headers.get("Permissions-Policy") == "microphone=(self)"
          and "unsafe-inline" not in policy and "*" not in policy,
          policy)

    outside = {}
    for path in ("/..%2Fbridge.py", "/%2e%2e/bridge.py", "/..%5Cbridge.py", "/nothing-like-this.js"):
        async with client.get(BASE + path) as response:
            outside[path] = (response.status, "duckdb" in (await response.text()))
    check("3e nothing outside harness/web can be read", all(status == 404 and not leaked for status, leaked in outside.values()),
          ", ".join(f"{path} {status}" for path, (status, _) in outside.items()))

    inside = [bridge.web_file(name) for name in ("chat.js", "web/chat.js")]
    refused = [bridge.web_file(name) for name in ("../bridge.py", "..\\bridge.py", "sub/../../bridge.py", ".env", "", "/etc/passwd", "C:/Windows/win.ini")]
    check("3f web_file allows a file under harness/web and refuses every way out of it",
          inside[0] is not None and inside[1] is None and refused == [None] * 7, f"{inside}, {refused}")


# ------------------------------------------------------------------ tools
async def tool_modules():
    server = MCPServer("plugins-test")
    loaded, said = caught(tools.load_plugins, server, ("probe_fake_tools", "probe_broken_tools", "brief_tools"))
    listed = {tool.name: tool.input_schema.get("required", []) for tool in await server.list_tools()}
    check("4a every tool module that is there is registered, a broken one is skipped with its traceback",
          loaded == ["probe_fake_tools", "brief_tools"] and "Traceback" in said and "broken on purpose" in said,
          f"loaded {loaded}")
    check("4b every tool of every module takes a reason first, and it is required",
          len(listed) == 8 and all(names and names[0] == "reason" for names in listed.values())
          and set(listed) >= {"fake_probe", "brief_overview", "make_brief", "get_brief", "search_collected"},
          ", ".join(sorted(listed)))
    answered = await server.call_tool("brief_overview", {"reason": " "})
    code = json.loads(answered.content[0].text).get("error", {}).get("code")
    check("4c an empty reason is refused in the tool's own words, not a crash", code == "no_reason", str(code))
    direct = brief_tools.search_collected(["AI"], 1, 1)  # the product's own code still calls these functions itself
    check("4d the plain function keeps its own signature for the rest of the product",
          isinstance(direct, dict) and ("error" in direct or "posts" in direct), str(direct.get("error", {}).get("code") or list(direct)[:3]))

    base = tools.load_plugins(MCPServer("base-url-test"), ("brief_tools",)) and brief_tools.BASE
    check("4e brief_tools is told where the product's own server is", base == (os.environ.get("SIGNAL_BASE_URL") or "http://127.0.0.1:5194"), base)


# ------------------------------------------------------------------ step wording
def wording():
    title = steps.title("mcp__harness__fake_probe", {"words": ["AI", "robots"]})
    done = steps.outcome("fake_probe", {"found": 2})
    failed = steps.outcome("fake_probe", {"error": {"message": "The words were too long."}}, True)
    given = steps.facts("mcp__harness__fake_probe", {"words": ["AI", "robots"]})
    check("5a a tool module writes its own step, in plain words",
          title == 'Looking for "AI", "robots"' and done == "Found 2 of them."
          and given == [{"label": "Words searched", "value": '"AI", "robots"'}], f"{title} / {done} / {given}")
    check("5b a registered tool that says nothing about a failure still gets the harness's own line",
          failed == "That did not work: The words were too long.", failed)

    steps.register_tool("angry_probe", lambda fields: 1 / 0, lambda result, is_error: 1 / 0, lambda fields: 1 / 0)
    check("5c a tool module whose own wording raises costs itself one sentence, never the turn",
          (steps.title("angry_probe", {}), steps.outcome("angry_probe", {"ok": True}), steps.facts("angry_probe", {})) == ("Working on it", "Done.", []),
          "the activity list survived a broken title, outcome and facts")

    said = [steps.title("search_collected", {"keywords": ["AI"], "hours": 8}),
            steps.outcome("search_collected", {"total": 3}), steps.outcome("search_collected", {"total": 0}),
            steps.title("make_brief", {"seconds": 60}), steps.outcome("make_brief", {"brief_id": "20260919-210000"}),
            steps.outcome("get_brief", {"status": "ready", "title": "Five minutes on AI"}),
            steps.title("collector_control", {"action": "stop"}), steps.outcome("collector_control", {"paused": True}),
            steps.title("follow_interest", {"query": "what is happening in AI"}),
            steps.outcome("follow_interest", {"interest": {"name": "AI", "terms": ["AI", "LLM"]}}),
            steps.outcome("brief_overview", {"interests": [{"name": "AI"}], "posts_kept": 1200})]
    dirt = [line for line in said if any(character in BANNED for character in line) or "->" in line or "_" in line]
    check("6a the Morning Brief tools read as plain words", not dirt and all(said), " | ".join(said[:4]))
    check("6b no brief step shows a code, an id or a symbol", not dirt, repr(dirt[:1]))
    # Proved in a bare process: the step wording is written where the events are built (the chat
    # server), and the tools themselves live in another process, so the runner has to load it itself.
    alone = subprocess.run([sys.executable, "-c", "import claude_runner, steps; print(claude_runner.WORDING); "
                            "print(steps.title('search_collected', {'keywords': ['AI']}))"],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    check("6c the chat server loads the step wording of every tool module by itself",
          alone.returncode == 0 and "brief_tools" in alone.stdout and 'Looking through the posts we already have for "AI"' in alone.stdout,
          " / ".join(alone.stdout.split()[:3] + [alone.stderr.strip()[-80:]]))
    facts = steps.facts("search_collected", {"keywords": ["AI", "LLM"], "hours": 8})
    check("6d the details panel gets the facts of a brief step",
          facts == [{"label": "Words searched", "value": '"AI", "LLM"'}, {"label": "Time covered", "value": "the last 8 hours"}], str(facts))
    check("6e nonsense arguments from the model cannot break a brief step",
          all(isinstance(steps.title(name, junk), str) and isinstance(steps.outcome(name, junk, True), str) and isinstance(steps.facts(name, junk), list)
              for name in ("search_collected", "make_brief", "get_brief", "follow_interest", "collector_control", "brief_overview", "unfollow_interest")
              for junk in ({"hours": "many"}, {"keywords": "AI"}, {"seconds": None}, [], "", None, {"words": [[]]})),
          "every tool survived seven kinds of nonsense")


# ------------------------------------------------------------------ cards and the prompt
class FakePipe:
    def __init__(self, lines=(), data=b""):
        self.lines, self.data = list(lines), data

    def write(self, _payload):
        pass

    async def drain(self):
        pass

    def close(self):
        pass

    async def read(self):
        return self.data

    async def __aiter__(self):
        for line in self.lines:
            yield line


class FakeProcess:
    def __init__(self, lines):
        self.stdin, self.stdout, self.stderr = FakePipe(), FakePipe(lines), FakePipe()
        self.returncode = None

    def kill(self):
        self.returncode = self.returncode if self.returncode is not None else -9

    async def wait(self):
        self.returncode = self.returncode if self.returncode is not None else 0


async def replay(lines):
    """One recorded turn through the real runner: no model, no subprocess, no cost."""
    directory = bridge.SESSIONS / "plugins-replay"
    (directory / "confirmations").mkdir(parents=True, exist_ok=True)
    runner = claude_runner.Runner("00000000-0000-4000-8000-0000000000aa", directory)
    original = claude_runner.asyncio.create_subprocess_exec

    async def fake(*_arguments, **_keywords):
        return FakeProcess(json.dumps(line).encode("utf-8") + b"\n" for line in lines)

    claude_runner.asyncio.create_subprocess_exec = fake
    try:
        return [event async for event in runner.turn("replayed")]
    finally:
        claude_runner.asyncio.create_subprocess_exec = original


RESULT = {"found": 2, "_card": {"kind": "probe", "words": ["AI", "robots"]}}


async def cards():
    events = await replay([
        {"type": "system", "subtype": "init", "tools": ["mcp__harness__fake_probe"], "mcp_servers": [{"name": "harness", "status": "connected"}]},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_P", "name": "mcp__harness__fake_probe",
                                                       "input": {"reason": "to see how people talk about it", "words": ["AI", "robots"]}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_P", "is_error": False,
                                                  "content": [{"type": "text", "text": json.dumps(RESULT)}]}]}},
        {"type": "result", "is_error": False, "duration_ms": 1200, "num_turns": 2},
    ])
    card = next((event for event in events if event["type"] == "card"), None)
    start = next(event for event in events if event["type"] == "step" and event["phase"] == "start")
    end = next(event for event in events if event["type"] == "step" and event["phase"] == "end")
    check("7a a tool result's own card reaches the page as one card event",
          card == {"type": "card", "card": RESULT["_card"]} and RESULT["_card"] == {"kind": "probe", "words": ["AI", "robots"]},
          str(card))
    check("7b the step of a plug-in tool carries its title, its facts and its outcome",
          start["title"] == 'Looking for "AI", "robots"' and start["facts"] == [{"label": "Words searched", "value": '"AI", "robots"'}]
          and start["why"] == "to see how people talk about it" and end["outcome"] == "Found 2 of them." and end["ok"] is True,
          f"{start['title']} / {end['outcome']}")
    check("7c a card arrives even when the tool failed, and a card that is not an object is ignored",
          [event["type"] for event in claude_runner.tool_events("x", {"error": {"message": "no"}, "_card": {"kind": "probe"}})] == ["card"]
          and list(claude_runner.tool_events("x", {"_card": "not an object"})) == [],
          "a failed call may still show its card")
    check("7d a step of a tool nobody registered carries no facts at all",
          "facts" not in claude_runner.step_start("toolu_Q", "mcp__harness__describe_sources", {"reason": "to look"}, 1, 0),
          "the old events are untouched")


def prompt():
    base = claude_runner.SYSTEM_PROMPT.read_text(encoding="utf-8")
    plain = claude_runner.system_prompt()
    claude_runner.PROMPTS.mkdir(parents=True, exist_ok=True)
    for name, text in FRAGMENTS.items():
        (claude_runner.PROMPTS / name).write_text(text, encoding="utf-8")
    try:
        whole = claude_runner.system_prompt()
    finally:
        for name in FRAGMENTS:
            (claude_runner.PROMPTS / name).unlink(missing_ok=True)
    after = claude_runner.system_prompt()
    first, last = (whole.find(text) for text in FRAGMENTS.values())
    check("8a every prompt fragment is appended, in name order, after the system prompt",
          whole.startswith(base) and 0 < first < last and whole.rstrip().endswith(FRAGMENTS["zz-test-plugins.md"]),
          f"base {len(base)} characters, with fragments {len(whole)}")
    check("8b the folder's own README is never sent to the model",
          "harness/prompts/" not in whole and "Keep a fragment under" not in whole and (claude_runner.PROMPTS / "README.md").exists(),
          "README.md stays out of the prompt")
    check("8c removing a fragment removes it from the next turn's prompt", after == plain and plain.startswith(base), f"{len(after)} characters again")


# ------------------------------------------------------------------ the browser's side
def browser():
    source = (ROOT / "web/chat.js").read_text(encoding="utf-8")
    exported = [name for name in ("onEvent", "appendCard", "send", "plainText", "el")
                if re.search(rf"^export (?:async )?function {name}\b", source, re.M)]
    check("9a chat.js exports the four things a plug-in builds on, and el", exported == ["onEvent", "appendCard", "send", "plainText", "el"], str(exported))
    check("9b each optional module is imported after start-up and a missing one is ignored",
          "'/voice.js', '/analysis.js', '/brief.js', '/realtime.js'" in source and "import(path).catch(() => {})" in source and "loadPlugins();" in source,
          "voice, analysis, brief and realtime")
    check("9c a turn tells a plug-in where it begins and ends",
          "emit({ type: 'turn_start', source })" in source and "emit({ type: 'turn_end' })" in source and "'confirm')" in source,
          "turn_start with its source, and turn_end")
    check("9d the step list is untouched: a row is still a mark, a title and a details link",
          source.count("class: 'link-button step-details', type: 'button', text: 'details'") == 1
          and "el('span', { class: 'step-title'" in source and "el('p', { class: 'step-raw-label', text: 'Exact request' })" in source,
          "the frozen row and the frozen panel")


async def main():
    write_modules()
    await absent()
    await served(routes())
    await tool_modules()
    wording()
    await cards()
    prompt()
    browser()
    print(f"\n{sum(OUTCOMES)}/{len(OUTCOMES)} checks passed", flush=True)
    return 0 if all(OUTCOMES) and OUTCOMES else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
