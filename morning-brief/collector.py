"""Bluesky Jetstream collector: a live tail plus parallel replay of the recent past.

Jetstream replays from a sequence cursor at roughly 15-25x real time per
connection, and a cursor resumes exactly (it is inclusive).  A backfill therefore
splits [start, live) into contiguous sequence ranges and replays them side by
side, so "the last 8 hours" is ready in about ten minutes instead of 8 hours.

Only posts matching an interest are kept.  Engagement is not taken from the
stream; briefing.py asks the AppView for it when a brief is written.
"""
import asyncio
import json
import os
import re
import time
from contextlib import aclosing, suppress
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

ENDPOINT = "wss://jetstream.us-west.bsky.network/xrpc/network.bsky.jetstream.subscribeEvents"
POSTS = "app.bsky.feed.post"
WORKERS = int(os.environ.get("BACKFILL_CONNECTIONS", 12))  # replay throughput scales per connection
CHUNKS = WORKERS * 6  # more chunks than workers: replay speed varies by region, and no connection should sit idle
SEQ_PER_SECOND = 450  # first guess only; seek() measures the real rate
RETRYABLE = (aiohttp.ClientError, asyncio.TimeoutError, OSError, RuntimeError)


def now_ms():
    return int(time.time() * 1000)


def parse_time(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def compile_terms(terms):
    """Whole-word match. Short all-caps terms (AI, LLM) stay case-sensitive, so "said" never matches "AI"."""
    exact = [re.escape(term) for term in terms if term.isupper() and len(term) <= 5]
    loose = [re.escape(term) for term in terms if not (term.isupper() and len(term) <= 5)]
    parts = exact + ([f"(?i:{'|'.join(loose)})"] if loose else [])
    return re.compile(rf"(?<!\w)(?:{'|'.join(parts)})(?!\w)")


def slug(name, taken):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "topic"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def extract(payload):
    """The parts of a post record a brief needs. Records are user-controlled, so nothing is assumed."""
    record = payload.get("record")
    if not isinstance(record, dict) or not isinstance(record.get("text"), str):
        return None
    embed = record.get("embed") if isinstance(record.get("embed"), dict) else {}
    media = embed.get("media") if isinstance(embed.get("media"), dict) else {}
    external = embed.get("external") or media.get("external")
    quoted = embed.get("record") if isinstance(embed.get("record"), dict) else {}
    quoted = quoted.get("record", quoted)  # recordWithMedia nests the reference one level down
    reply = record.get("reply") if isinstance(record.get("reply"), dict) else {}
    parent = reply.get("parent") if isinstance(reply.get("parent"), dict) else {}
    langs = record.get("langs") if isinstance(record.get("langs"), list) else []
    link = None
    if isinstance(external, dict) and isinstance(external.get("uri"), str):
        link = {"url": external["uri"], "title": str(external.get("title") or ""), "description": str(external.get("description") or "")}
    return {
        "text": record["text"],
        "langs": [lang for lang in langs if isinstance(lang, str)],
        "parent": parent.get("uri"),
        "quote": quoted.get("uri") if isinstance(quoted, dict) else None,
        "link": link,
    }


class Store:
    """Matched posts in memory, mirrored to an append-only JSONL file."""

    def __init__(self, directory, retain_hours):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.retain_ms = retain_hours * 3600_000
        self.posts = {}
        self.counts = {}
        self.heat = {}  # replies and quotes seen in the stream: a hint for what to hydrate first
        self.interests = []
        self.live_seq = None
        self.saved_at = None
        self.log = None

    def load_state(self):
        path = self.dir / "state.json"
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.live_seq, self.saved_at = state.get("live_seq"), state.get("saved_at")

    def load(self):
        path = self.dir / "interests.json"
        self.interests = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        self.load_state()
        path = self.dir / "posts.jsonl"
        if path.exists():
            with path.open(encoding="utf-8") as lines:
                for line in lines:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue  # a torn final line after a crash
                    if "del" in row:
                        self.posts.pop(row["del"], None)
                    else:
                        self.posts[row["uri"]] = row
        self.compact()

    def compact(self):
        """Drop posts past retention and rewrite the log without tombstones or superseded rows."""
        cutoff = now_ms() - self.retain_ms
        known = {interest["id"] for interest in self.interests}
        kept = {}
        for uri, post in self.posts.items():
            topics = [topic for topic in post["topics"] if topic in known]
            if post["t"] >= cutoff and topics:
                kept[uri] = {**post, "topics": topics}
        self.posts = kept
        self.heat = {uri: n for uri, n in self.heat.items() if uri in kept}
        self.counts = {}
        for post in kept.values():
            for topic in post["topics"]:
                self.counts[topic] = self.counts.get(topic, 0) + 1
        if self.log:
            self.log.close()
        temporary = self.dir / "posts.jsonl.tmp"
        with temporary.open("w", encoding="utf-8") as out:
            for post in kept.values():
                out.write(json.dumps(post, ensure_ascii=False) + "\n")
        temporary.replace(self.dir / "posts.jsonl")
        self.log = (self.dir / "posts.jsonl").open("a", encoding="utf-8")

    def write(self, row):
        self.log.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.log.flush()

    def add(self, post):
        previous = self.posts.get(post["uri"])
        if previous:
            new = [topic for topic in post["topics"] if topic not in previous["topics"]]
            if not new:
                return False
            post = {**previous, "topics": previous["topics"] + new}
        else:
            new = post["topics"]
        for topic in new:
            self.counts[topic] = self.counts.get(topic, 0) + 1
        self.posts[post["uri"]] = post
        self.write(post)
        return not previous

    def remove(self, uri):
        post = self.posts.pop(uri, None)
        if post:
            for topic in post["topics"]:
                self.counts[topic] -= 1
            self.write({"del": uri})

    def save_interests(self):
        (self.dir / "interests.json").write_text(json.dumps(self.interests, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def save_state(self):
        if self.live_seq is not None:
            (self.dir / "state.json").write_text(json.dumps({"live_seq": self.live_seq, "saved_at": now_ms()}) + "\n", encoding="utf-8")

    def close(self, save):
        if save:
            self.save_state()
        if self.log:
            self.log.close()


class Collector:
    def __init__(self, store, backfill_hours):
        self.store = store
        self.backfill_hours = backfill_hours
        self.session = None
        self.patterns = []
        self.ready = asyncio.Event()
        self.queue = asyncio.Queue()
        self.job = None
        self.live = {"state": "connecting", "first_seq": None, "event_ms": None, "scanned": 0, "replayed": 0, "matched": 0, "started": now_ms()}
        self.tasks = []
        self.paused = False

    def refresh_patterns(self):
        self.patterns = [(interest["id"], set(interest.get("langs") or []), compile_terms(interest["terms"])) for interest in self.store.interests]

    async def start(self):
        self.session = aiohttp.ClientSession()
        self.refresh_patterns()
        self.tasks = [asyncio.create_task(coroutine) for coroutine in (self.tail(), self.work(), self.housekeeping())]
        window_start = now_ms() - self.backfill_hours * 3600_000
        resume = self.store.live_seq if (self.store.saved_at or 0) > window_start else None
        if any((interest.get("covered_from") or now_ms()) > window_start + 600_000 for interest in self.store.interests):
            resume = None  # some interest has never been backfilled this far
        if self.store.interests:
            self.queue.put_nowait({"interests": [interest["id"] for interest in self.store.interests], "from_seq": resume, "to_seq": None})

    async def pause(self):
        """Do to the cursor exactly what stopping the process does, but keep the store's log open."""
        if self.paused:
            return
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        if self.idle():  # a cancelled backfill still holds self.job, so the cursor stays behind its unfilled gap
            self.store.save_state()
        self.job = None
        while not self.queue.empty():
            self.queue.get_nowait()
        await self.session.close()
        self.ready.clear()
        self.live.update(state="stopped", first_seq=None, event_ms=None)
        self.paused = True

    async def resume(self):
        if not self.paused:
            return
        self.store.load_state()  # the cursor a restart would resume from; the in-memory one may be past an unfilled gap
        self.paused = False
        self.live["state"] = "connecting"
        await self.start()

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.session.close()
        self.store.close(save=self.idle() and not self.paused)  # after a pause idle() is trivially true

    def idle(self):
        """The saved cursor marks where a restart resumes, so it must not move past a gap still being filled."""
        return self.job is None and self.queue.empty()

    def add_interest(self, name, terms, langs):
        interest = {"id": slug(name, {i["id"] for i in self.store.interests}), "name": name, "terms": terms, "langs": langs, "created": now_ms(), "covered_from": None}
        self.store.interests.append(interest)
        self.store.save_interests()
        self.refresh_patterns()  # the live tail matches it from here on; the job below fills in the past
        if not self.paused:  # resume() queues a job per interest, and covered_from=None makes this one a full replay
            self.queue.put_nowait({"interests": [interest["id"]], "from_seq": None, "to_seq": self.store.live_seq})
        return interest

    def remove_interest(self, interest_id):
        self.store.interests = [interest for interest in self.store.interests if interest["id"] != interest_id]
        self.store.save_interests()
        self.refresh_patterns()
        self.store.compact()

    async def events(self, cursor=None):
        params = [("collections", POSTS)] + ([("cursor", str(cursor))] if cursor is not None else [])
        socket = await self.session.ws_connect(ENDPOINT, params=params, heartbeat=30, max_msg_size=0)
        try:
            async for message in socket:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(message.data)
                if data.get("$type") == "message" and isinstance(data.get("payload"), dict):
                    yield data["payload"]
                elif data.get("$type") == "error":
                    raise RuntimeError(f"Jetstream error: {data.get('error')}")
        finally:
            # A graceful close reads until the server's close frame, i.e. through its whole queued replay.
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(socket.close(), 0.5)

    def handle(self, payload, patterns):
        """Keep the post if it matches; return True when it is new to the store."""
        if payload.get("collection") != POSTS or not payload.get("$type", "").endswith("#commit"):
            return False
        uri = f"at://{payload.get('did')}/{POSTS}/{payload.get('rkey')}"
        if payload.get("operation") == "delete":
            self.store.remove(uri)
            return False
        post = extract(payload)
        if not post:
            return False
        for target in (post["parent"], post["quote"]):
            if target in self.store.posts:
                self.store.heat[target] = self.store.heat.get(target, 0) + 1
        link = post["link"] or {}
        haystack = "\n".join(part for part in (post["text"], link.get("title"), link.get("description")) if part)
        langs = set(post["langs"])
        topics = [topic for topic, wanted, pattern in patterns if (not wanted or not langs or wanted & langs) and pattern.search(haystack)]
        if not topics:
            return False
        return self.store.add({"uri": uri, "did": payload["did"], "rkey": payload["rkey"], "t": parse_time(payload["time"]), "topics": topics, **post})

    async def tail(self):
        cursor, delay = None, 1
        while True:
            try:
                async for payload in self.events(cursor):
                    seq = payload.get("seq")
                    if not isinstance(seq, int):
                        continue
                    if self.live["first_seq"] is None:
                        self.live["first_seq"] = seq
                        self.ready.set()
                    self.live["state"], delay = "live", 1
                    self.live["scanned"] += 1
                    self.live["matched"] += self.handle(payload, self.patterns)
                    self.live["event_ms"] = parse_time(payload["time"]) if self.live["scanned"] % 50 == 1 else self.live["event_ms"]
                    self.store.live_seq, cursor = seq, seq + 1
            except RETRYABLE as error:
                print(f"live stream: {error!r}", flush=True)
            self.live["state"] = "reconnecting"
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def probe(self, cursor):
        async with aclosing(self.events(cursor)) as stream:
            async for payload in stream:
                if isinstance(payload.get("seq"), int) and payload.get("time"):
                    return payload["seq"], parse_time(payload["time"])
        raise RuntimeError("stream closed before the first event")

    async def seek(self, target_ms, end_seq):
        """Find the sequence number at target_ms by probing; the sequence rate drifts through the day."""
        rate, best, now = SEQ_PER_SECOND, None, now_ms()
        for _ in range(6):
            seq, at = await self.probe(max(0, end_seq - int(rate * (now - target_ms) / 1000)))
            if best and seq == best[0]:
                break  # the server's retention floor: nothing older to find
            if not best or abs(at - target_ms) < abs(best[1] - target_ms):
                best = (seq, at)
            if abs(at - target_ms) < 300_000:
                break
            rate = (end_seq - seq) / ((now - at) / 1000)
        return best

    async def replay(self, chunks, patterns, job):
        while chunks:
            low, high = chunks.pop()
            await self.segment(low, high, patterns, job)
            job["chunks_done"] += 1

    async def segment(self, low, high, patterns, job):
        cursor, delay = low, 1
        while cursor < high:
            try:
                async with aclosing(self.events(cursor)) as stream:
                    async for payload in stream:
                        seq = payload.get("seq")
                        if not isinstance(seq, int):
                            continue
                        if seq >= high:
                            cursor = high
                            break
                        job["scanned"] += 1
                        self.live["replayed"] += 1
                        job["matched"] += self.handle(payload, patterns)
                        cursor, delay = seq + 1, 1
            except RETRYABLE as error:
                print(f"backfill {low}-{high}: {error!r}", flush=True)
            if cursor < high:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def backfill(self, request):
        await self.ready.wait()
        wanted = [interest for interest in self.store.interests if interest["id"] in request["interests"]]
        if not wanted:
            return
        end = request["to_seq"] or self.live["first_seq"]
        target = now_ms() - self.backfill_hours * 3600_000
        self.job = job = {"interests": [interest["name"] for interest in wanted], "started": now_ms(), "from_ms": None, "scanned": 0, "matched": 0, "chunks_done": 0}
        if request["from_seq"] is not None:
            start, start_ms = request["from_seq"], self.store.saved_at
        else:
            start, start_ms = await self.seek(target, end)
        job["from_ms"] = start_ms
        patterns = [pattern for pattern in self.patterns if pattern[0] in request["interests"]]
        bounds = [start + (end - start) * i // CHUNKS for i in range(CHUNKS + 1)]
        chunks = [(bounds[i], bounds[i + 1]) for i in range(CHUNKS) if bounds[i] < bounds[i + 1]]  # newest popped first
        await asyncio.gather(*(self.replay(chunks, patterns, job) for _ in range(WORKERS)))
        for interest in wanted:
            interest["covered_from"] = min(interest.get("covered_from") or start_ms, start_ms)
        self.store.save_interests()
        print(f"backfill of {job['interests']} finished: {job['scanned']:,} posts scanned, {job['matched']:,} kept, {(now_ms() - job['started']) / 60000:.1f} min", flush=True)

    async def work(self):
        while True:
            request = await self.queue.get()
            try:
                await self.backfill(request)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"backfill failed: {error!r}", flush=True)
            self.job = None

    async def housekeeping(self):
        ticks = 0
        while True:
            await asyncio.sleep(30)
            ticks += 1
            if self.idle():
                self.store.save_state()
                if ticks % 20 == 0:
                    self.store.compact()

    def status(self):
        job = self.job and {
            "interests": self.job["interests"], "from_ms": self.job["from_ms"], "scanned": self.job["scanned"], "matched": self.job["matched"],
            "fraction": self.job["chunks_done"] / CHUNKS, "elapsed_s": (now_ms() - self.job["started"]) / 1000,
        }
        return {
            "live": {**self.live, "lag_s": (now_ms() - self.live["event_ms"]) / 1000 if self.live["event_ms"] else None},
            "backfill": job, "queued": self.queue.qsize(), "backfill_hours": self.backfill_hours, "posts": len(self.store.posts), "paused": self.paused,
            "source": {"network": "Bluesky", "stream": "Jetstream", "host": urlsplit(ENDPOINT).hostname, "collection": POSTS},
            "interests": [{**interest, "posts": self.store.counts.get(interest["id"], 0)} for interest in self.store.interests],
        }
