# Chat-driven automation proposals

On the React home screen, choose **Build automation**, describe the goal in a sentence, and
chat through the targets and relevance rules. The agent shows a validated proposal card and
tracks unresolved questions. Once resolved, **Confirm configuration** records the exact revision
and offers the final `automation-config.json` download. A changed draft needs a fresh confirmation.

This is a configuration handoff. It does not create a collector, run Jev, or connect an automation
creation service. Source, schedule, date windows and budgets remain outside the supplied v2 schema.

## Runtime

Restart the backend after pulling this change so it loads the new confirmation handler. The usual
launcher now declares jsonschema and pins DuckDB to the compiler's supplied version:

```powershell
uv run --prerelease allow harness/ui_server.py
```

The front end sends `purpose: automation_proposal`. The backend fixes that conversation's purpose
and selects `automation_mcp_server.py`, exposing only the four proposal tools. The normal chart
chat keeps its existing tool surface. The in-app model and reasoning settings are unchanged.

Tools:

- `get_automation_contract`: exact schema plus AI-company or public-policy example.
- `get_automation_proposal`: latest valid proposal, revision and open questions.
- `save_automation_proposal`: strict JSON parsing, original offline compiler, and atomic draft save.
- `request_automation_confirmation`: request user approval, blocked by unresolved questions.

The existing confirmation endpoint handles proposal decisions directly, without asking a model
to approve or execute anything. It verifies session, revision hash, expiry and decision state,
revalidates the configuration, and writes the exact six-field public object to
`harness/state/sessions/<id>/automation-final.json`. The review envelope stays in
`automation-proposal.json`. Changes invalidate the old approval and remove the old final file.

Proposals live in the current chat session. This first version does not restore their conversation
when you leave the page or restart the server; download a confirmed configuration before leaving.

## Verification

```powershell
uv run --with duckdb==1.5.5 --with 'jsonschema>=4.23,<5' python harness/tests/test_automation_proposals.py
uv run --prerelease allow --with 'aiohttp>=3.11,<4' --with 'mcp>=2' --with duckdb==1.5.5 --with 'jsonschema>=4.23,<5' --with pytz python harness/tests/test_automation_http.py
```

`web/tests/automation-proposal.test.ts` checks request/event mapping. The Playwright script
`web/tests/automation-browser.cjs` exercises goal entry, clarification, review, confirmation,
download and revision supersession against deterministic responses. Serve `web/dist` on 5197 or
set `AUTOMATION_UI_URL`; `PLAYWRIGHT_MODULE` and `UI_BROWSER_PATH` can select local installations.

`harness/tests/smoke_automation_conversation.py --run` is an opt-in real two-turn Claude test. It
uses the existing login and per-turn budget cap, and only the proposal tool server. It does not
approve the result. Schema validity is verified; model accuracy is not calibrated by these tests.
