# harness/prompts/

Every `*.md` file in this folder is appended to the system prompt, in name order, at the start of
every turn (`claude_runner.system_prompt`). One file per stream of work, named after it:
`voice.md`, `analysis.md`, `brief.md`, `jev.md`. This README is the exception and is never sent.

Write for a capable model, not for a parser: short plain prose saying when to use your tools, what
they give back, and what to tell the user. No shouting, no rigid scripts, no repeated rules that
`harness/system_prompt.md` already sets, and nothing a tool's own docstring says better.

Keep a fragment under about 40 lines. The whole prompt goes on the command line of every turn, so a
long one costs every turn on this laptop.

The rest of the plug-in contract, so nobody has to edit a shared file:

- **Routes.** `harness/voice.py`, `harness/analysis_api.py`: define `setup(app)` and register your
  own `/api/...` routes on it. `bridge.attach` imports the module if it exists and calls `setup`.
- **Tools.** `harness/brief_tools.py`, `harness/analysis_tools.py`, `harness/jev_tools.py`: define
  `register(mcp)` and add your tools with `mcp.tool()`. Every tool takes `reason: str` first, one
  short plain sentence for the user, and returns plain dicts, with a failure as
  `{"error": {"code", "message", "hint"}}` whose message a regular person can read.
- **Step wording.** Call `steps.register_tool(name, title_fn, outcome_fn, facts_fn=None)` at import.
  `title_fn(tool_input)` says what is being done, `outcome_fn(result, is_error)` what came back, and
  the optional `facts_fn(tool_input)` returns `[{"label": ..., "value": ...}]` for the details panel.
  Return `""` from `outcome_fn` to let `steps.py` write the failure line itself.
- **Cards.** Put `"_card": {"kind": "brief", ...}` in a tool result. The runner sends the page a
  `{"type": "card", "card": {...}}` event and leaves your result untouched for the model.
- **Browser.** `harness/web/voice.js`, `analysis.js`, `brief.js` are imported by `chat.js` after
  start-up, and a missing one is ignored. Build on its exports and nothing else: `onEvent(fn)` for
  every event of every turn (plus `{type: "turn_start", source}` and `{type: "turn_end"}`),
  `appendCard(node)`, `send(text)`, `plainText(text)` and `el(tag, props, ...children)`. Render only
  the cards of your own `kind`. Text goes in with `textContent`, never as HTML.
