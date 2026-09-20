"""Loopback control API for the single shared collector and inference scheduler."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import secrets
import signal
import stat
import sys
from urllib.parse import urlsplit

from aiohttp import web

from common import ROOT, compile_config
from runtime import Runtime
from store import Store


EXAMPLES = {
    "ai": "automation-config.current.json",
    "public-policy": "automation-config.public-policy.json",
}
GUIDE = Path(__file__).with_name("AGENT_GUIDE.md")


def _positive(value, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be finite and greater than zero")


def _token(state_dir: Path) -> str:
    path = state_dir / "service.token"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            if stat.S_IMODE(os.fstat(stream.fileno()).st_mode) & 0o077:
                raise ValueError(f"{path} must be private (chmod 600)")
            token = stream.read().strip()
        if len(token) < 32:
            raise ValueError(f"invalid service token in {path}")
        return token
    else:
        token = secrets.token_urlsafe(32)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return token


async def _body(
    request: web.Request, required: set[str], optional: set[str] | None = None
) -> dict:
    def reject(value):
        raise ValueError(f"non-finite JSON value: {value}")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    if request.content_type != "application/json":
        raise ValueError("send application/json")
    value = json.loads(
        await request.text(), parse_constant=reject, object_pairs_hook=unique
    )
    if not isinstance(value, dict):
        raise ValueError("request body must be an object")
    missing = required - set(value)
    unknown = set(value) - required - (optional or set())
    if missing or unknown:
        raise ValueError(
            f"invalid request fields; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    return value


def make_app(
    store: Store, runtime: Runtime, token: str, port: int, stop: asyncio.Event
) -> web.Application:
    """Build the authenticated API; source lifecycle is owned by serve()."""
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    @web.middleware
    async def guarded(request, handler):
        if (
            request.headers.get("Host") not in allowed_hosts
            or "Origin" in request.headers
        ):
            return web.json_response(
                {"error": "only non-browser loopback clients are accepted"}, status=403
            )
        if not secrets.compare_digest(
            request.headers.get("Authorization", "").encode("utf-8"),
            ("Bearer " + token).encode("utf-8"),
        ):
            return web.json_response(
                {"error": "valid local service token required"}, status=401
            )
        try:
            return await handler(request)
        except KeyError as error:
            return web.json_response(
                {"error": f"not found: {error.args[0]}"}, status=404
            )
        except (ValueError, TypeError) as error:
            return web.json_response({"error": str(error)}, status=400)
        except web.HTTPException as error:
            return web.json_response({"error": error.reason}, status=error.status)
        except Exception as error:
            print(
                json.dumps({"event": "api_error", "error": str(error)}),
                file=sys.stderr,
                flush=True,
            )
            return web.json_response(
                {"error": "local service failure; inspect daemon logs"}, status=500
            )

    app = web.Application(middlewares=[guarded], client_max_size=16 * 1024 * 1024)

    async def status(request):
        return web.json_response(
            {"source": store.source_status(), "automations": store.list_automations()}
        )

    async def schema(request):
        return web.json_response(
            json.loads(
                (
                    ROOT / "twitter-preparation" / "automation-config.schema.json"
                ).read_text()
            )
        )

    async def example(request):
        filename = EXAMPLES[request.match_info["name"]]
        return web.json_response(
            json.loads((ROOT / "twitter-preparation" / filename).read_text())
        )

    async def guidance(request):
        return web.json_response({"guidance": GUIDE.read_text(encoding="utf-8")})

    async def validate(request):
        body = await _body(request, {"config"})
        compiled = compile_config(body["config"])
        return web.json_response(
            {
                "ok": True,
                "config_hash": compiled["config_hash"],
                "configuration_key": compiled["configuration_key"],
            }
        )

    async def automations(request):
        if request.method == "GET":
            return web.json_response({"automations": store.list_automations()})
        body = await _body(request, {"name", "config"}, {"max_usd"})
        result = store.create_automation(
            body["name"], body["config"], body.get("max_usd", 0)
        )
        runtime.wake()
        return web.json_response(result, status=201)

    async def automation(request):
        return web.json_response(store.get_automation(request.match_info["id"]))

    async def control(request):
        action = request.match_info["action"]
        identifier = request.match_info["id"]
        if action == "budget":
            body = await _body(request, {"max_usd"})
            result = store.set_budget(identifier, body["max_usd"])
        elif action in {"start", "pause"}:
            await _body(request, set())
            result = store.set_enabled(identifier, action == "start")
        else:
            raise KeyError(action)
        runtime.wake()
        return web.json_response(result)

    async def results(request):
        unknown = set(request.query) - {"kind", "limit"}
        if unknown:
            raise ValueError(f"unsupported query parameters: {sorted(unknown)}")
        identifier = request.match_info["id"]
        kind = request.query.get("kind", "posts")
        limit = int(request.query.get("limit", "100"))
        rows = store.results(identifier, kind=kind, limit=limit)
        return web.json_response(
            {
                "automation_id": identifier,
                "kind": kind,
                "results": rows,
                "snapshot": True,
            }
        )

    async def resume_live(request):
        body = await _body(request, {"acknowledge_gap"})
        if body["acknowledge_gap"] is not True:
            raise ValueError(
                "acknowledge_gap must be true; skipped history cannot be recovered by this command"
            )
        if store.source_status().get("status") != "gap":
            raise ValueError("source is not waiting on an acknowledged cursor gap")
        store.reset_cursor()
        runtime.wake()
        return web.json_response({"source": store.source_status()})

    async def shutdown(request):
        await _body(request, set())
        asyncio.get_running_loop().call_soon(stop.set)
        return web.json_response({"stopping": True})

    app.router.add_get("/v1/status", status)
    app.router.add_get("/v1/schema", schema)
    app.router.add_get("/v1/examples/{name}", example)
    app.router.add_get("/v1/guidance", guidance)
    app.router.add_post("/v1/validate", validate)
    app.router.add_route("GET", "/v1/automations", automations)
    app.router.add_route("POST", "/v1/automations", automations)
    app.router.add_get("/v1/automations/{id}", automation)
    app.router.add_get("/v1/automations/{id}/results", results)
    app.router.add_post("/v1/automations/{id}/{action}", control)
    app.router.add_post("/v1/source/resume-live", resume_live)
    app.router.add_post("/v1/shutdown", shutdown)
    return app


async def serve(args) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("concurrency", "requests_per_minute", "batch_size", "port"):
        _positive(getattr(args, name), name)
    if args.port > 65535:
        raise ValueError("port must be at most 65535")
    if args.run_seconds is not None:
        _positive(args.run_seconds, "run_seconds")
    source = urlsplit(args.source_url)
    if (
        source.scheme not in {"ws", "wss"}
        or not source.hostname
        or source.username
        or source.password
        or source.query
        or source.fragment
    ):
        raise ValueError(
            "source URL must be an unfiltered ws(s) endpoint without credentials, query parameters, or fragment"
        )
    if source.scheme == "ws" and source.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("non-loopback sources require wss")

    lock_fd = os.open(
        state_dir / "service.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(lock_fd, "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(
                f"a service already owns {state_dir}; use its CLI/MCP API instead of starting another collector"
            ) from error
        token = _token(state_dir)
        store = Store(state_dir / "live.sqlite")
        runner = None
        runtime_task = None
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        installed_signals = []
        timer = None
        try:
            store.set_source(args.source_url)
            runtime = Runtime(
                store,
                state_dir,
                source_url=args.source_url,
                api_key=os.environ.get("TYPESAFE_API_KEY"),
                concurrency=args.concurrency,
                requests_per_minute=args.requests_per_minute,
                batch_size=args.batch_size,
            )
            app = make_app(store, runtime, token, args.port, stop)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", args.port).start()
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, stop.set)
                installed_signals.append(signum)
            if args.run_seconds is not None:
                timer = loop.call_later(args.run_seconds, stop.set)
            runtime_task = asyncio.create_task(
                runtime.run(stop), name="shared-bluesky-runtime"
            )
            print(
                json.dumps(
                    {
                        "event": "bluesky_automation_ready",
                        "url": f"http://127.0.0.1:{args.port}",
                        "state_dir": str(state_dir),
                        "source": args.source_url,
                    }
                ),
                file=sys.stderr,
                flush=True,
            )
            stop_task = asyncio.create_task(stop.wait())
            try:
                await asyncio.wait(
                    {runtime_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if runtime_task.done():
                    runtime_task.result()
            finally:
                stop.set()
                runtime.wake()
                stop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stop_task
                await runtime_task
        finally:
            stop.set()
            if timer:
                timer.cancel()
            for signum in installed_signals:
                loop.remove_signal_handler(signum)
            if runtime_task is not None and not runtime_task.done():
                runtime_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runtime_task
            if runner is not None:
                await runner.cleanup()
            store.close()
    return 0
