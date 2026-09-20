# Historical data and live automations

Run `npm --prefix web run build` and `uv run harness/ui_server.py`, then open
http://127.0.0.1:5196. Claude Code must be logged in. Live scoring uses
`TYPESAFE_API_KEY` from the existing repository `.env`; it never reaches the browser.

## Workflow

- **Historical data** is selected initially. Enter `AI`, an included AI company,
  or one of its known products. It opens the existing saved replay without new
  inference. Other historical topics are refused with a Live data option.
- **Live data** accepts a topic description. The chat fills the workspace while
  the user refines targets, keywords, and relevance rules. There is no empty
  dashboard behind this conversation.
- The agent uses the schema and compiler from `semantic-automation-config-v2.zip`.
  Its three schema/example JSON files were verified byte-for-byte. Fixed model
  and sentiment semantics remain pinned; runtime settings stay outside the schema.
- **Confirm configuration** binds approval to the exact revision. The final card
  supports JSON download, a total Jev spending limit, and **Start live tracking**.
  The suggested $0.10 cap is displayed before launch. $0 captures without scoring.
  Revising a schema invalidates its old approval. Repeated launch requests reuse
  the same automation and never silently increase its budget.
- Launch opens the familiar line and bubble views for any configured targets.
  Live points have 1m/5m/15m intervals. The archived replay retains its existing
  intervals and playback controls. Live chat reads that automation's recorded
  observations through `get_live_tracking_data`.

Historical and live contracts deliberately differ: the saved export was produced
with `company-sentiment-choice-v1`, while the supplied neutral schema uses
`target-sentiment-v1`. `get_historical_data_contract` exposes the export's original schema, policy,
configuration key and taxonomy. No historical scores are relabelled or recomputed.

## Ownership and shutdown

`harness/automation_api.py` owns a single CLI subprocess on loopback port 8767
(`SENTIMETER_CLI_PORT` overrides it). Its private state and token are under
`harness/state/live-automations/`; the existing standalone CLI default on 8766
is separate. One source connection is shared across active trackers.

Stop tracking, Close tracker, switching workspaces, or leaving for Home pauses
the selected automation and cancels its active Jev HTTP request before returning.
Closing a browser tab sends a viewer-release request. Abrupt browser/network loss
expires the viewer lease within 30 seconds (plus the 2-second monitor interval).
Other viewing tabs and other active trackers retain their own leases. Once all
trackers close, the daemon exits. A separate 45-second control watchdog in the
child handles a crashed or unresponsive harness. An idle saved-result view never
opens the upstream connection; reading it does not authorize resuming inference.

Requests already accepted by Jev may still be billed after cancellation. Native
uncertain-charge reservations are preserved. The total budget subtracts metered,
reserved and uncertain costs from **all** batches for the automation, including
restarts; new batches do not receive a fresh allowance. Raising the total cap is
an explicit dashboard action. Saved configurations and observations survive close.

## Data path

The CLI feature commit `54b1c47` from `feature/bluesky-automation-cli` was imported
without replacing the integrated UI. Windows file locks and signal handling are
supported. The CLI owns collection, filtering, exact text-version classification,
like-subject resolution, and SQLite writes. The harness proxies its authenticated
control API; its MCP version stays isolated from the CLI environment.

`GET /v1/automations/{id}/events?after=N` exposes a durable completion cursor in
`visualization_events`. It includes late Jev results and resolved likes older
than the latest-post snapshot window. Paging and reconnects retain all completed
observations; stable IDs prevent duplicates. Publication, edit, and signed like
changes remain distinct. The browser sorts by observation time before aggregation,
so delivery order cannot change sentiment. Text edits add no publication baseline.
Scores use 5 * (1 + P(positive) - P(negative)); insufficient evidence stays unscored.
Pending inference, budget exhaustion, errors and source gaps are surfaced explicitly.
Gap recovery requires the dashboard's acknowledgement of missed history.

The full source journal grows while trackers run. There is no hidden retention or
fabricated historical backfill for a new automation. Draft conversation restoration
across a page reload is not implemented; download a finalized schema if needed.

## Verification

```powershell
uv run bluesky-automation/test_service.py
uv run bluesky-automation/test_visualization.py
uv run harness/tests/test_automation_lifecycle.py
npm --prefix web test
npm --prefix web run build
npm --prefix web run lint
```

`web/tests/live-automation-browser.cjs` checks the expanded chat, confirmation,
launch, minute charts, generic targets, live query routing and close/release flow.
Use `AUTOMATION_UI_URL`, `PLAYWRIGHT_MODULE` and `UI_BROWSER_PATH` for local browser
installations. These tests mock external provider boundaries; a separate bounded
real smoke run verified Claude schema generation, CLI capture, Jev classification,
chart-feed delivery and complete shutdown. Its local proof is in
`harness/state/live-workflow-proof.json` (excluded from Git).
