# Harness providers

New chat and automation-proposal sessions use **Codex, `gpt-6-astra`, low reasoning,
Fast service tier** by default. The existing Claude runner remains available.
This selects the conversational harness; the sentiment classifier and collection
pipeline keep their existing models and contracts.

Start the Sentimeter server from the repository root:

```powershell
$env:HARNESS_PROVIDER = 'codex'
python -m uv run harness/ui_server.py
```

Codex must be installed on PATH and authenticated with `codex login` under the
same account running the server. The adapter uses that existing login through
`codex app-server`; it does not require a separate OpenAI API key or change your
global Codex settings. Tested with Codex CLI 0.155.1.

To return to Claude, stop the server, select it, and launch again:

```powershell
$env:HARNESS_PROVIDER = 'claude'
python -m uv run harness/ui_server.py
```

Claude still uses `claude_runner.py`, its existing login, `HARNESS_MODEL` (default
`opus`), `HARNESS_EFFORT` (default `low`), and the existing per-turn budget cap.
There is no automatic provider fallback: configuration or account failures appear
in the chat rather than silently running a different model.

| Codex setting | Default |
| --- | --- |
| `HARNESS_CODEX_MODEL` | `gpt-6-astra` |
| `HARNESS_CODEX_EFFORT` | `low` |
| `HARNESS_CODEX_SERVICE_TIER` | `fast` |
| `HARNESS_CODEX_IDLE_TIMEOUT` | `300` seconds |
| `HARNESS_TURN_TIMEOUT` | `300` seconds; shared with Claude |

Fast is a requested service tier, subject to the logged-in account's availability
and limits. The adapter checks the app-server's selected model, effort and tier
(which may report `priority` for Fast). This is not a latency guarantee.

Each conversation owns a warm app-server and harness MCP process. After five idle
minutes they close; the next message resumes the same native Codex thread. A
server restart expires web sessions as before; the web client creates a new one.
Session-local `codex-thread.json` records the selected configuration and
`codex.stderr.log` contains transport diagnostics. Native conversation history is
stored by Codex under its existing user account.

The adapter preserves the existing SSE messages, streamed text, progress steps,
cards and confirmation events. Personal MCP servers, plugins, hooks and host
environment tools are disabled. The proposal conversation exposes only its four
existing automation tools. The existing HTTP confirmation button still owns the
final decision. Configuring Fast does not enable Claude usage credits.

Offline checks, using the harness Python environment:

```powershell
python harness/tests/test_codex_runner.py
python harness/tests/test_automation_http.py
```

An opt-in live check uses the selected provider and account quota:

```powershell
python harness/tests/smoke_automation_conversation.py --run
```

Protocol reference: [Codex app-server](https://learn.chatgpt.com/docs/app-server).
