# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb==1.5.5", "jsonschema>=4.23,<5", "httpx>=0.28,<0.29", "aiohttp>=3.12,<4", "websockets>=15,<16", "mcp>=1.28,<2", "pydantic>=2.11,<3"]
# ///
"""Deterministic local WebSocket/provider scenarios; never contacts a real API."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

import httpx
from aiohttp import web
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from websockets.asyncio.server import serve as websocket_server
from websockets.datastructures import Headers
from websockets.http11 import Response

from api import make_app
from common import ROOT, read_config
from runtime import Runtime
from store import Store


LABELS = {"positive", "negative", "neutral", "mixed", "insufficient_evidence"}


def post(seq, text, *, rkey="post", cid="cid-original", operation="create"):
    event = {
        "$type": "network.bsky.jetstream.subscribeEvents#commit",
        "seq": seq,
        "did": "did:plc:author",
        "time": "2026-09-20T12:00:00Z",
        "rev": "revision",
        "collection": "app.bsky.feed.post",
        "rkey": rkey,
        "operation": operation,
    }
    if operation != "delete":
        event.update(
            cid=cid,
            record={
                "$type": "app.bsky.feed.post",
                "text": text,
                "createdAt": "2026-09-20T11:00:00Z",
            },
        )
    return event


def like(seq, *, rkey="like", subject="post", cid="cid-original", operation="create"):
    event = {
        "$type": "network.bsky.jetstream.subscribeEvents#commit",
        "seq": seq,
        "did": "did:plc:liker",
        "time": "2026-09-20T12:01:00Z",
        "rev": "revision",
        "collection": "app.bsky.feed.like",
        "rkey": rkey,
        "operation": operation,
    }
    if operation != "delete":
        event.update(
            cid="like-cid",
            record={
                "$type": "app.bsky.feed.like",
                "createdAt": event["time"],
                "subject": {
                    "uri": f"at://did:plc:author/app.bsky.feed.post/{subject}",
                    "cid": cid,
                },
            },
        )
    return event


def fixture_config(name="ai"):
    filename = (
        "automation-config.current.json"
        if name == "ai"
        else "automation-config.public-policy.json"
    )
    config = read_config(ROOT / "twitter-preparation" / filename)
    config["targets"] = config["targets"][:1]
    return config


async def until(predicate, timeout=20):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


class StorageScenarios(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bluesky-store-")
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "live.sqlite")
        self.addCleanup(self.store.close)
        self.store.set_source("ws://127.0.0.1/source")

    def test_replay_atomic_cursor_and_missing_like_context(self):
        auto = self.store.create_automation("one", fixture_config(), 0)
        self.store.set_enabled(auto["id"], True)
        original = post(1, "OpenAI is good")
        first_like = like(2)
        self.assertEqual(self.store.ingest([original, first_like]), 2)
        self.assertEqual(self.store.ingest([original, first_like]), 0)
        self.store.ingest(
            [
                like(3, rkey="late-like", subject="late", cid="cid-late"),
                post(4, "OpenAI late context", rkey="late", cid="cid-late"),
                like(5, operation="delete"),
                like(6, rkey="unseen-like", operation="delete"),
                post(7, "OpenAI edited text", cid="cid-edited", operation="update"),
                post(8, "", operation="delete"),
                like(9, operation="delete"),
                like(10),
                like(11, operation="delete"),
            ]
        )
        before = self.store.source_status()
        conflict = copy.deepcopy(original)
        conflict["record"]["text"] = "conflicting replay"
        with self.assertRaises(ValueError):
            self.store.ingest([post(12, "must roll back", rkey="rollback"), conflict])
        after = self.store.source_status()
        self.assertEqual(after["cursor"], 11)
        self.assertEqual(after["event_count"], before["event_count"])
        self.assertEqual(after["unresolved_like_count"], 1)

        rows, through = self.store.fetch_posts(auto["id"])
        matches = [
            dict(row, groups=["openai"], discovery=False)
            for row in rows
            if row.get("text")
        ]
        self.store.route_posts(auto["id"], matches, through)
        results = [
            {
                "content_version": row["content_version"],
                "status": "ready",
                "classification": {
                    "status": "accepted",
                    "companies": ["openai"],
                    "probabilities": {"openai": 0.9},
                },
                "sentiment": {
                    "openai": {
                        "type": "choice",
                        "choice": "positive",
                        "confidence": 0.96,
                        "probabilities": {
                            label: 0.96 if label == "positive" else 0.01
                            for label in LABELS
                        },
                    }
                },
            }
            for row in matches
        ]
        self.store.save_results(auto["id"], results)
        observed = self.store.results(auto["id"], kind="likes", limit=100)
        self.assertEqual(
            sorted(row["likes_delta"] for row in observed), [-1, -1, 1, 1, 1]
        )
        old_hash = hashlib.sha256(original["record"]["text"].encode()).hexdigest()
        for row in observed:
            if row["post_uri"].endswith("/post"):
                self.assertEqual(row["content_version"], old_hash)
        # Rerouting the same data must not reset completed enrichment.
        self.store.route_posts(auto["id"], matches, through)
        self.assertEqual(self.store.pending(auto["id"]), [])

    def test_identical_text_shares_work_without_losing_exact_cid_context(self):
        auto = self.store.create_automation("shared text", fixture_config(), 0)
        text = "OpenAI keeps exact  whitespace\nand Unicode é."
        self.store.ingest(
            [
                post(1, text, cid="first"),
                post(2, text, cid="second", operation="update"),
                like(3, rkey="first-like", cid="first"),
                like(4, rkey="second-like", cid="second"),
                post(5, text, rkey="another", cid="third"),
                like(6, rkey="third-like", subject="another", cid="third"),
            ]
        )
        rows, through = self.store.fetch_posts(auto["id"])
        self.store.route_posts(
            auto["id"],
            [dict(row, groups=["openai"], discovery=False) for row in rows],
            through,
        )
        content_version = hashlib.sha256(text.encode()).hexdigest()
        self.assertEqual(
            self.store.pending(auto["id"]),
            [{"content_version": content_version, "text": text}],
        )
        likes = self.store.results(auto["id"], "likes")
        self.assertEqual(
            {row["subject_cid"] for row in likes}, {"first", "second", "third"}
        )
        self.assertTrue(
            all(
                row["content_version"] == content_version
                and row["cid"] == row["subject_cid"]
                for row in likes
            )
        )
        with self.assertRaisesRegex(ValueError, "conflicting text"):
            self.store.ingest([post(7, "a different body", cid="first")])
        self.assertEqual(self.store.source_status()["cursor"], 6)

    def test_gap_reset_does_not_register_against_old_history(self):
        self.store.ingest([post(100, "OpenAI historical capture")])
        self.store.set_source_status("gap", "cursor expired")
        self.store.reset_cursor()
        auto = self.store.create_automation("new after gap", fixture_config(), 0)
        self.assertEqual(auto["start_cursor"], 100)
        rows, through = self.store.fetch_posts(auto["id"])
        self.assertEqual((rows, through), ([], 100))
        self.store.route_posts(auto["id"], rows, through)
        self.assertEqual(self.store.pending(auto["id"]), [])

    def test_gap_does_not_attribute_unlikes_using_stale_subjects(self):
        auto = self.store.create_automation("subject coverage", fixture_config(), 0)
        self.store.ingest([post(1, "OpenAI before gap"), like(2)])
        rows, through = self.store.fetch_posts(auto["id"])
        matches = [dict(row, groups=["openai"], discovery=False) for row in rows]
        self.store.route_posts(auto["id"], matches, through)
        self.store.set_source_status("gap", "cursor expired")
        self.store.reset_cursor()
        self.store.ingest([like(3, operation="delete")])
        rows, through = self.store.fetch_posts(auto["id"])
        self.store.route_posts(auto["id"], rows, through)
        observed = self.store.results(auto["id"], "likes")
        self.assertEqual([(row["seq"], row["likes_delta"]) for row in observed], [(2, 1)])
        self.assertEqual(self.store.source_status()["unresolved_like_count"], 1)

    def test_repository_sync_is_not_a_post_deletion(self):
        auto = self.store.create_automation("account history", fixture_config(), 0)
        self.store.ingest([post(1, "OpenAI captured text")])
        rows, through = self.store.fetch_posts(auto["id"])
        self.store.route_posts(
            auto["id"],
            [dict(row, groups=["openai"], discovery=False) for row in rows],
            through,
        )
        self.store.ingest(
            [
                {
                    "$type": "network.bsky.jetstream.subscribeEvents#sync",
                    "seq": 2,
                    "did": "did:plc:author",
                }
            ]
        )
        self.assertFalse(self.store.results(auto["id"])[0]["current_deleted"])
        self.assertEqual(
            self.store.source_status()["notices"][0]["kind"], "repository_sync"
        )
        self.store.ingest(
            [
                {
                    "$type": "network.bsky.jetstream.subscribeEvents#account",
                    "seq": 3,
                    "did": "did:plc:author",
                    "active": False,
                    "status": "deleted",
                }
            ]
        )
        self.assertTrue(self.store.results(auto["id"])[0]["current_deleted"])

    def test_rejects_semantic_mutation_and_nonfinite_budgets(self):
        config = fixture_config()
        config["semantics"]["criteria"]["positive"] = "Opposition."
        with self.assertRaises(ValueError):
            self.store.create_automation("bad", config, 0)
        for amount in [True, -1, float("nan"), float("inf")]:
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                self.store.create_automation("bad-budget", fixture_config(), amount)


class SharedRuntimeScenarios(unittest.IsolatedAsyncioTestCase):
    async def test_one_socket_isolated_automations_shared_cache_and_restart(self):
        with tempfile.TemporaryDirectory(prefix="bluesky-runtime-") as directory:
            state_dir = Path(directory)
            store = Store(state_dir / "live.sqlite")
            history = []
            connections = []
            active = 0
            peak = 0
            requests = []

            async def stream(connection):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                connections.append(connection)
                try:
                    cursor = parse_qs(urlsplit(connection.request.path).query).get(
                        "cursor", [None]
                    )[0]
                    if cursor is not None:
                        for event in history:
                            if event["seq"] >= int(cursor):
                                await connection.send(
                                    json.dumps({"$type": "message", "payload": event})
                                )
                    await connection.wait_closed()
                finally:
                    active -= 1

            async def respond(request):
                payload = json.loads(request.content)
                requests.append(payload)
                answers = {}
                for target, question in payload["questions"].items():
                    if question["type"] == "noul":
                        answers[target] = {"type": "noul", "noul": 0.95}
                    else:
                        self.assertEqual(set(question["criteria"]), LABELS)
                        answers[target] = {
                            "type": "choice",
                            "choice": "positive",
                            "confidence": 0.96,
                            "probabilities": {
                                label: 0.96 if label == "positive" else 0.01
                                for label in LABELS
                            },
                        }
                return httpx.Response(
                    200,
                    json={
                        "model": payload["model"],
                        "answers": answers,
                        "usage": {"input_tokens": 100, "output_tokens": 20},
                    },
                )

            async with websocket_server(
                stream, "127.0.0.1", 0, subprotocols=["xrpc.v1.json"]
            ) as server:
                port = server.sockets[0].getsockname()[1]
                source = (
                    f"ws://127.0.0.1:{port}/xrpc/network.bsky.jetstream.subscribeEvents"
                )
                store.set_source(source)
                first = store.create_automation("AI", fixture_config(), 0.1)
                second = store.create_automation(
                    "Policy", fixture_config("public-policy"), 0.1
                )
                duplicate = store.create_automation("AI cached", fixture_config(), 0.1)
                capture_only = store.create_automation(
                    "Capture only", fixture_config(), 0
                )
                limited_config = fixture_config()
                limited_config["categorization"]["rules"].append(
                    "Prefer central discussion over incidental mentions."
                )
                limited = store.create_automation(
                    "Budget guarded", limited_config, 0.00000001
                )
                for auto in [first, second, duplicate, capture_only, limited]:
                    store.set_enabled(auto["id"], True)
                runtime = Runtime(
                    store,
                    state_dir,
                    source_url=source,
                    api_key="offline-test-only",
                    requests_per_minute=60000,
                    batch_size=4,
                    transport=httpx.MockTransport(respond),
                )
                stop = asyncio.Event()
                task = asyncio.create_task(runtime.run(stop))
                try:
                    await until(lambda: active == 1)
                    event = post(
                        100,
                        "OpenAI supports congestion pricing.\nExact  captured text.",
                    )
                    history.append(event)
                    await connections[-1].send(
                        json.dumps({"$type": "message", "payload": event})
                    )
                    await until(
                        lambda: all(
                            any(
                                row.get("status") == "ready"
                                for row in store.results(auto["id"])
                            )
                            for auto in [first, second, duplicate]
                        )
                    )
                    for auto, target in [
                        (first, "openai"),
                        (second, "congestion-pricing"),
                        (duplicate, "openai"),
                    ]:
                        row = store.results(auto["id"])[0]
                        self.assertEqual(row["classification"]["companies"], [target])
                        self.assertEqual(set(row["sentiment"]), {target})
                        self.assertEqual(
                            set(row["sentiment"][target]["probabilities"]), LABELS
                        )
                    self.assertEqual(
                        len(requests),
                        4,
                        "identical successful requests must be shared across automations",
                    )
                    self.assertEqual(
                        store.results(capture_only["id"])[0]["status"], "pending"
                    )
                    self.assertEqual(peak, 1)
                    self.assertEqual(len(connections), 1)
                    for payload in requests:
                        self.assertEqual(
                            payload["state"]["post"], event["record"]["text"]
                        )
                    like_event = like(101)
                    history.append(like_event)
                    await connections[-1].send(
                        json.dumps({"$type": "message", "payload": like_event})
                    )
                    await until(
                        lambda: len(store.results(first["id"], kind="likes")) == 1
                    )
                    self.assertEqual(
                        len(requests), 4, "likes must reuse post enrichment"
                    )
                    await until(
                        lambda: (
                            store.get_automation(limited["id"])["worker_state"]
                            == "budget_exhausted"
                        )
                    )
                    store.set_budget(capture_only["id"], 0.1)
                    runtime.wake()
                    await until(
                        lambda: (
                            store.results(capture_only["id"])[0]["status"] == "ready"
                        )
                    )
                    self.assertEqual(
                        len(requests),
                        4,
                        "unlocking cached work must not dispatch again",
                    )
                    late = store.create_automation(
                        "Policy registered later", fixture_config("public-policy"), 0
                    )
                    store.set_enabled(late["id"], True)
                    runtime.wake()
                    followup = post(
                        102, "OpenAI reports another model.", cid="new-record"
                    )
                    history.append(followup)
                    await connections[-1].send(
                        json.dumps({"$type": "message", "payload": followup})
                    )
                    await until(
                        lambda: (
                            len(store.results(first["id"])) == 2
                            and all(
                                row["status"] == "ready"
                                for row in store.results(first["id"])
                            )
                        )
                    )
                    await until(
                        lambda: (
                            store.get_automation(late["id"])["routing_cursor"] == 102
                        )
                    )
                    self.assertEqual(
                        store.results(late["id"]),
                        [],
                        "new registrations must not inherit earlier batch rows",
                    )
                    self.assertEqual(len(store.results(second["id"])), 1)
                    # Inclusive source replay is harmless when a live socket reconnects.
                    await connections[-1].close()
                    await until(
                        lambda: (
                            len(connections) >= 2
                            and store.source_status()["cursor"] == 102
                        )
                    )
                    self.assertEqual(store.source_status()["event_count"], 3)
                    self.assertEqual(len(store.results(first["id"], kind="likes")), 1)
                finally:
                    stop.set()
                    runtime.wake()
                    await asyncio.wait_for(task, 15)
                before = len(requests)
                runtime = Runtime(
                    store,
                    state_dir,
                    source_url=source,
                    api_key="offline-test-only",
                    requests_per_minute=60000,
                    batch_size=4,
                    transport=httpx.MockTransport(respond),
                )
                stop = asyncio.Event()
                task = asyncio.create_task(runtime.run(stop))
                try:
                    await until(lambda: active == 1)
                    await asyncio.sleep(0.4)
                    self.assertEqual(len(requests), before)
                    self.assertEqual(store.source_status()["event_count"], 3)
                finally:
                    stop.set()
                    runtime.wake()
                    await asyncio.wait_for(task, 15)
                    store.close()


class GapRecoveryScenarios(unittest.IsolatedAsyncioTestCase):
    async def _check_inband_gap(self, frame, prior_event):
        with tempfile.TemporaryDirectory(prefix="bluesky-inband-gap-") as directory:
            state_dir = Path(directory)
            store = Store(state_dir / "live.sqlite")
            connections = []

            async def stream(connection):
                connections.append(connection)
                if len(connections) == 1:
                    if prior_event:
                        await connection.send(
                            json.dumps(
                                {
                                    "$type": "message",
                                    "payload": post(100, "OpenAI before bad frame"),
                                }
                            )
                        )
                    await connection.send(frame)
                    await connection.send(
                        json.dumps(
                            {
                                "$type": "message",
                                "payload": post(200, "must not advance past bad frame"),
                            }
                        )
                    )
                else:
                    await connection.send(
                        json.dumps(
                            {
                                "$type": "message",
                                "payload": post(
                                    300,
                                    "OpenAI after explicit reset",
                                    cid="after-reset",
                                ),
                            }
                        )
                    )
                await connection.wait_closed()

            async with websocket_server(
                stream, "127.0.0.1", 0, subprotocols=["xrpc.v1.json"]
            ) as server:
                source = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/source"
                runtime = Runtime(store, state_dir, source_url=source)
                stop = asyncio.Event()
                task = asyncio.create_task(runtime.run(stop))
                try:
                    await until(lambda: store.source_status()["status"] == "gap")
                    self.assertEqual(
                        store.source_status()["cursor"], 100 if prior_event else None
                    )
                    stop.set()
                    runtime.wake()
                    await asyncio.wait_for(task, 15)
                    self.assertEqual(store.source_status()["status"], "gap")
                    runtime = Runtime(store, state_dir, source_url=source)
                    stop = asyncio.Event()
                    task = asyncio.create_task(runtime.run(stop))
                    await asyncio.sleep(0.3)
                    self.assertEqual(
                        len(connections),
                        1,
                        "restart must not acknowledge a coverage gap",
                    )
                    store.reset_cursor()
                    runtime.wake()
                    await until(lambda: store.source_status()["cursor"] == 300)
                    self.assertEqual(
                        store.source_status()["event_count"], 2 if prior_event else 1
                    )
                finally:
                    stop.set()
                    runtime.wake()
                    await asyncio.wait_for(task, 15)
                    store.close()

    async def test_outdated_cursor_info_without_sequence_survives_restart(self):
        frame = {
            "$type": "message",
            "payload": {
                "$type": "network.bsky.jetstream.subscribeEvents#info",
                "name": "OutdatedCursor",
            },
        }
        await self._check_inband_gap(json.dumps(frame), False)

    async def test_malformed_frame_does_not_skip_to_a_later_cursor(self):
        await self._check_inband_gap("{not-json", True)

    async def test_old_cursor_requires_explicit_gap_acknowledgement(self):
        with tempfile.TemporaryDirectory(prefix="bluesky-gap-") as directory:
            state_dir = Path(directory)
            store = Store(state_dir / "live.sqlite")
            paths = []

            async def handshake(connection, request):
                paths.append(request.path)
                if len(paths) == 1:
                    return Response(
                        400,
                        "Bad Request",
                        Headers({"Content-Type": "application/json"}),
                        b'{"error":"CursorTooOld","message":"cursor is below retention floor"}',
                    )
                return None

            async def stream(connection):
                await connection.send(
                    json.dumps(
                        {
                            "$type": "message",
                            "payload": post(
                                200, "OpenAI after an acknowledged gap", cid="after-gap"
                            ),
                        }
                    )
                )
                await connection.wait_closed()

            async with websocket_server(
                stream,
                "127.0.0.1",
                0,
                subprotocols=["xrpc.v1.json"],
                process_request=handshake,
            ) as server:
                source = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/source"
                store.set_source(source)
                store.ingest([post(100, "OpenAI before downtime")])
                runtime = Runtime(store, state_dir, source_url=source)
                stop = asyncio.Event()
                task = asyncio.create_task(runtime.run(stop))
                try:
                    await until(lambda: store.source_status()["status"] == "gap")
                    self.assertEqual(store.source_status()["cursor"], 100)
                    await asyncio.sleep(0.3)
                    self.assertEqual(len(paths), 1)
                    store.reset_cursor()
                    runtime.wake()
                    await until(lambda: store.source_status()["cursor"] == 200)
                    self.assertIn("cursor=100", paths[0])
                    self.assertNotIn("cursor=", paths[1])
                    self.assertEqual(store.source_status()["event_count"], 2)
                finally:
                    stop.set()
                    runtime.wake()
                    await asyncio.wait_for(task, 15)
                    store.close()


class ControlProtocolScenarios(unittest.IsolatedAsyncioTestCase):
    async def test_cli_and_mcp_share_authenticated_service(self):
        with tempfile.TemporaryDirectory(prefix="bluesky-protocol-") as directory:
            state_dir = Path(directory)
            token = "local-test-token-" + "a" * 32
            (state_dir / "service.token").write_text(token)
            os.chmod(state_dir / "service.token", 0o600)
            store = Store(state_dir / "live.sqlite")
            connections = []

            async def stream(connection):
                connections.append(connection)
                await connection.wait_closed()

            async with websocket_server(
                stream, "127.0.0.1", 0, subprotocols=["xrpc.v1.json"]
            ) as server:
                source = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/source"
                store.set_source(source)
                runtime = Runtime(store, state_dir, source_url=source)
                stop = asyncio.Event()
                task = asyncio.create_task(runtime.run(stop))
                # Reserve a port before constructing the Host-header allowlist.
                import socket

                listener = socket.socket()
                listener.bind(("127.0.0.1", 0))
                listener.listen(128)
                listener.setblocking(False)
                port = listener.getsockname()[1]
                app = make_app(store, runtime, token, port, stop)
                runner = web.AppRunner(app, access_log=None)
                await runner.setup()
                await web.SockSite(runner, listener).start()
                url = f"http://127.0.0.1:{port}"
                command = [
                    sys.executable,
                    str(Path(__file__).with_name("automation.py")),
                    "--state-dir",
                    str(state_dir),
                    "--url",
                    url,
                ]
                try:
                    await until(lambda: len(connections) == 1)
                    async with httpx.AsyncClient(
                        base_url=url, trust_env=False
                    ) as client:
                        self.assertEqual(
                            (await client.get("/v1/status")).status_code, 401
                        )
                        headers = {"Authorization": "Bearer " + token}
                        self.assertEqual(
                            (
                                await client.get(
                                    "/v1/status",
                                    headers={
                                        **headers,
                                        "Origin": "https://untrusted.example",
                                    },
                                )
                            ).status_code,
                            403,
                        )
                        self.assertEqual(
                            (
                                await client.get(
                                    "/v1/status",
                                    headers={**headers, "Host": "untrusted.example"},
                                )
                            ).status_code,
                            403,
                        )
                    process = await asyncio.create_subprocess_exec(
                        *command,
                        "create",
                        "CLI capture",
                        "--config",
                        str(
                            ROOT
                            / "twitter-preparation"
                            / "automation-config.current.json"
                        ),
                        "--max-usd",
                        "0",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, stderr = await process.communicate()
                    self.assertEqual(process.returncode, 0, stderr.decode())
                    auto = json.loads(stdout)
                    self.assertFalse(auto["enabled"])
                    parameters = StdioServerParameters(
                        command=command[0], args=[*command[1:], "mcp"]
                    )
                    async with stdio_client(parameters) as (reader, writer):
                        async with ClientSession(reader, writer) as session:
                            await session.initialize()
                            tools = {
                                tool.name for tool in (await session.list_tools()).tools
                            }
                            self.assertTrue(
                                {
                                    "schema",
                                    "validate",
                                    "create",
                                    "list",
                                    "status",
                                    "start",
                                    "pause",
                                    "budget",
                                    "results",
                                    "resume_live",
                                }
                                <= tools
                            )
                            schema = await session.read_resource("bluesky://schema")
                            self.assertEqual(
                                json.loads(schema.contents[0].text)["properties"][
                                    "schema_version"
                                ]["const"],
                                "semantic-automation-config-v2",
                            )
                            await session.get_prompt("automation_setup")
                            started = await session.call_tool(
                                "start", {"automation_id": auto["id"]}
                            )
                            self.assertFalse(started.isError)
                            self.assertTrue(store.get_automation(auto["id"])["enabled"])
                            result = await session.call_tool("status", {})
                            self.assertFalse(result.isError)
                            body = json.loads(result.content[0].text)
                            self.assertEqual(body["automations"][0]["id"], auto["id"])
                            rejected_budget = await session.call_tool(
                                "budget", {"automation_id": auto["id"], "max_usd": True}
                            )
                            self.assertTrue(rejected_budget.isError)
                            self.assertEqual(
                                store.get_automation(auto["id"])["max_usd"], 0
                            )
                            malformed = fixture_config()
                            malformed["semantics"]["criteria"]["positive"] = (
                                "Opposition"
                            )
                            rejected = await session.call_tool(
                                "validate", {"config": malformed}
                            )
                            self.assertTrue(rejected.isError)
                            paused = await session.call_tool(
                                "pause", {"automation_id": auto["id"]}
                            )
                            self.assertFalse(paused.isError)
                            self.assertFalse(
                                store.get_automation(auto["id"])["enabled"]
                            )
                    self.assertEqual(
                        len(connections),
                        1,
                        "CLI/MCP must not create additional collectors",
                    )
                finally:
                    stop.set()
                    runtime.wake()
                    await asyncio.wait_for(task, 15)
                    await runner.cleanup()
                    store.close()


if __name__ == "__main__":
    unittest.main()
