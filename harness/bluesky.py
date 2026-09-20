"""Bluesky in real time: a live tail of Jetstream and a parallel replay of the last minutes.

Jetstream replays from a sequence cursor at roughly 15-25x real time per connection, and a cursor
resumes exactly, so "the last 15 minutes" is scanned by splitting the window into contiguous
sequence ranges and replaying them side by side.  compile_terms, extract, the socket handling and
seek/replay are copied from morning-brief/collector.py in this repo (that folder is owned by someone
else, so nothing here imports from it).  Engagement is never in the stream: the AppView is asked for
it afterwards, in batches of 25 uris, exactly as morning-brief/briefing.py does.
"""
import asyncio
import json
import math
import re
import time
from contextlib import aclosing, suppress
from datetime import datetime, timezone

import aiohttp

ENDPOINT = "wss://jetstream.us-west.bsky.network/xrpc/network.bsky.jetstream.subscribeEvents"
APPVIEW = "https://public.api.bsky.app/xrpc/app.bsky.feed.getPosts"  # no login needed; searchPosts is 403 without one
POSTS = "app.bsky.feed.post"
SEQ_PER_SECOND, MAX_SEQ_PER_SECOND = 450, 5_000.0  # a first guess only; seek() measures the real rate
CHUNKS_PER_CONNECTION = 2  # a few more chunks than connections keeps none idle; many more get the IP rate-limited
RETRYABLE = (aiohttp.ClientError, asyncio.TimeoutError, OSError, RuntimeError)
# A socket that is accepted and then says nothing is a stalled server, not a quiet network: at 35+
# posts a second a 15 s gap never happens, so drop it and reconnect. (The keyword moved in aiohttp 3.13.)
WS_TIMEOUT = {"timeout": aiohttp.ClientWSTimeout(ws_receive=15)} if hasattr(aiohttp, "ClientWSTimeout") else {"receive_timeout": 15}
BUCKET_MS = 300_000  # scan_recent: five-minute buckets
LIVE_BUCKET_MS = 5_000  # listen_live: five-second buckets
POOL = 160  # matched posts kept for examples, newest first
SLOTS, MIN_SLOT_MS = 100, 5_000  # coverage is measured in slots; public Bluesky puts 150+ posts in 5 s, so a replayed slot is never empty
LAG_MS = 2_000  # the live stream runs about a second behind real time, so "now" is a moment ago
MARGIN_MS = 20_000  # a replay starts a little before the window, so its first slot is never half empty
HYDRATE_LIMIT, HYDRATE_BATCH, HYDRATE_BUDGET_S = 50, 25, 8.0
MAX_EXAMPLES, TEXT_LIMIT, HAYSTACK_LIMIT = 6, 240, 20_000
SAFE_ID = re.compile(r"[A-Za-z0-9:._~-]{1,256}")
ENGAGEMENT_NOTE = "Bluesky like and repost counts are current totals from the AppView, not what the post had inside the window."


def now_ms():
    return int(time.time() * 1000)


def stamp(value):
    """Jetstream stamps every event; a record's own dates are user-controlled, so only this one is trusted."""
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return int((moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp() * 1000)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def local(ms, pattern):
    return datetime.fromtimestamp(ms / 1000).strftime(pattern)


def zone():
    """What the clock times in a result mean, in the words the model may repeat to the user."""
    return "local time on this computer"


def compile_terms(terms):
    """Whole-word match. Short all-caps terms (AI, LLM) stay case-sensitive, so "said" never matches "AI"."""
    terms = [term.strip() for term in terms if isinstance(term, str) and term.strip()]
    if not terms:
        raise ValueError("no usable keywords")
    exact = [re.escape(term) for term in terms if term.isupper() and len(term) <= 5]
    loose = [re.escape(term) for term in terms if not (term.isupper() and len(term) <= 5)]
    parts = exact + ([f"(?i:{'|'.join(loose)})"] if loose else [])
    return re.compile(rf"(?<!\w)(?:{'|'.join(parts)})(?!\w)")


def extract(payload):
    """The parts of a post record a scan needs. Records are user-controlled, so nothing is assumed."""
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
    link = {}
    if isinstance(external, dict):  # only real strings reach the haystack: a dict here would be searched as its repr
        link = {part: external[part][:TEXT_LIMIT] for part in ("title", "description") if isinstance(external.get(part), str)}
    text = record["text"]
    return {
        "text": text,
        "haystack": "\n".join(part for part in (text[:HAYSTACK_LIMIT], link.get("title"), link.get("description")) if part),
        "langs": [lang for lang in langs if isinstance(lang, str)][:8],
        "parent": parent.get("uri") if isinstance(parent.get("uri"), str) else None,
        "quote": quoted.get("uri") if isinstance(quoted, dict) and isinstance(quoted.get("uri"), str) else None,
    }


class Scan:
    """Counts, buckets and a bounded pool of candidate examples. Holds no network, so tests can feed it."""

    def __init__(self, keywords, from_ms, to_ms, bucket_ms, language=None, label="%H:%M"):
        self.pattern = compile_terms(keywords)
        self.keywords = [term.strip() for term in keywords if isinstance(term, str) and term.strip()]
        self.wanted = {language} if isinstance(language, str) and language else set()
        self.from_ms, self.to_ms, self.bucket_ms, self.label = from_ms, max(to_ms, from_ms + 1000), bucket_ms, label
        self.origin = (from_ms // bucket_ms) * bucket_ms
        self.count = [0] * max(1, math.ceil((self.to_ms - self.origin) / bucket_ms))
        # Coverage is measured, not inferred: a slot counts as seen once a post stamped inside it arrives.
        self.slot_ms = max(MIN_SLOT_MS, (self.to_ms - self.from_ms) // SLOTS)
        self.slots, self.total_slots = set(), max(1, math.ceil((self.to_ms - self.from_ms) / self.slot_ms))
        self.scanned = self.matched = 0
        self.seen = set()  # a reconnect can repeat one event; a post is never counted twice
        self.pool = {}  # uri -> row, insertion-ordered, oldest dropped past POOL
        self.heat = {}  # replies and quotes seen for a pooled post: a hint for which example matters

    def feed(self, payload):
        if not isinstance(payload, dict) or payload.get("collection") != POSTS or payload.get("operation") == "delete":
            return False
        at = stamp(payload.get("time"))
        if at is None:
            return False
        if self.from_ms <= at < self.to_ms:
            self.slots.add((at - self.from_ms) // self.slot_ms)
        did, rkey = payload.get("did"), payload.get("rkey")
        if not isinstance(did, str) or not isinstance(rkey, str):
            self.scanned += 1
            return False
        uri = f"at://{did}/{POSTS}/{rkey}"
        if uri in self.seen:
            return False
        if len(self.seen) < 2_000_000:
            self.seen.add(uri)
        self.scanned += 1
        post = extract(payload)
        if post is None:
            return False
        for target in (post["parent"], post["quote"]):
            if target in self.pool:
                self.heat[target] = self.heat.get(target, 0) + 1
        langs = set(post["langs"])
        if self.wanted and langs and not self.wanted & langs:
            return False
        if not self.pattern.search(post["haystack"]):
            return False
        if at < self.from_ms:
            return False
        self.matched += 1
        self.count[min(len(self.count) - 1, max(0, (at - self.origin) // self.bucket_ms))] += 1
        self.pool[uri] = {"uri": uri, "did": did, "rkey": rkey, "t": at, "text": post["text"], "langs": post["langs"]}
        while len(self.pool) > POOL:
            oldest = next(iter(self.pool))
            self.pool.pop(oldest, None)
            self.heat.pop(oldest, None)
        return True

    def coverage(self):
        return min(1.0, len(self.slots) / self.total_slots)

    def buckets(self):
        return [{"label": local(self.origin + index * self.bucket_ms, self.label), "count": count} for index, count in enumerate(self.count)]

    def candidates(self):
        """Most recent matches first, a post that drew replies or quotes ahead of its neighbours."""
        return sorted(self.pool.values(), key=lambda row: (self.heat.get(row["uri"], 0), row["t"]), reverse=True)[:HYDRATE_LIMIT]


class Jetstream:
    def __init__(self, session):
        self.session = session

    async def events(self, cursor=None):
        params = [("collections", POSTS)] + ([("cursor", str(cursor))] if cursor is not None else [])
        socket = await self.session.ws_connect(ENDPOINT, params=params, heartbeat=30, max_msg_size=0, **WS_TIMEOUT)
        try:
            async for message in socket:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(message.data)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("$type") == "message" and isinstance(data.get("payload"), dict):
                    yield data["payload"]
                elif data.get("$type") == "error":
                    raise RuntimeError(f"Jetstream error: {str(data.get('error'))[:120]}")
        finally:
            # A graceful close reads until the server's close frame, i.e. through its whole queued replay.
            with suppress(asyncio.TimeoutError, *RETRYABLE):
                await asyncio.wait_for(socket.close(), 0.5)

    async def probe(self, cursor, attempts=3):
        """The first event at or after `cursor`. Retried: one refused handshake must not end a scan."""
        for attempt in range(attempts):
            try:
                async with aclosing(self.events(cursor)) as stream:
                    async for payload in stream:
                        at = stamp(payload.get("time"))
                        if isinstance(payload.get("seq"), int) and at:
                            return payload["seq"], at
                raise RuntimeError("the stream closed before its first event")
            except RETRYABLE:
                if attempt == attempts - 1:
                    raise
                await asyncio.sleep(1 + attempt)
        raise RuntimeError("unreachable")

    async def seek(self, target_ms, end_seq, end_ms, tolerance_ms, deadline):
        """The sequence number just before target_ms, by probing: the sequence rate drifts through the day.

        An undershoot is worth a little wasted replay, because a cursor that starts after target_ms
        leaves the oldest minutes of the window permanently unscanned.
        """
        rate, best, under = SEQ_PER_SECOND, None, None
        for _ in range(8):
            seq, at = await self.probe(max(0, end_seq - int(rate * (end_ms - target_ms + tolerance_ms) / 1000)))
            if seq in {found[0] for found in (best, under) if found}:
                break  # the same event twice: converged, or the server's retention floor
            if at <= target_ms and (under is None or at > under[1]):
                under = (seq, at)
            if best is None or abs(at - target_ms) < abs(best[1] - target_ms):
                best = (seq, at)
            # A probe that lands a second from the head would divide by nothing and send the next
            # guess to the dawn of time, so the baseline and the rate itself are both bounded.
            rate = min(MAX_SEQ_PER_SECOND, max(50.0, (end_seq - seq) / max(20.0, (end_ms - at) / 1000)))
            if (under and target_ms - under[1] <= tolerance_ms) or time.monotonic() > deadline:
                break
        return (under or best or (None, None)) + (rate,)


async def place_start(jetstream, from_ms, head_seq, head_ms, found, deadline):
    """Step back until the replay really does begin at or before the window.

    Bluesky has bursts, a bulk import can put ten thousand posts into two seconds, so sequence
    numbers and wall clock drift apart badly, and a start placed by an average rate can land inside
    the window and silently lose its oldest minutes. Every step here is a measured probe instead,
    and starting too early only costs replay, which is cheap.
    """
    seq, at, rate = found
    step = 1.0
    while at > from_ms and step <= 8 and time.monotonic() < deadline:
        guess = max(0, head_seq - int(rate * step * (head_ms - from_ms + MARGIN_MS) / 1000))
        if guess <= 0:
            break
        try:
            seq, at = await asyncio.wait_for(jetstream.probe(guess, attempts=1), 6)
        except (asyncio.TimeoutError, *RETRYABLE):
            break
        step *= 2
    return seq, at


async def pump(stream, scan, high, deadline, state):
    """Drain one async iterator of payloads into the scan, stopping at `high` or at the deadline.

    The cursor lives in `state` so that a connection dropping mid-chunk does not lose it: resuming
    from the chunk's start instead would replay — and double-count — everything already scanned.
    """
    async for payload in stream:
        seq = payload.get("seq") if isinstance(payload, dict) else None
        if not isinstance(seq, int):
            continue
        if high is not None and seq >= high:
            state["cursor"] = high
            return
        scan.feed(payload)
        state["cursor"] = seq + 1
        if time.monotonic() >= deadline:
            return


async def segment(jetstream, scan, low, high, deadline):
    state, delay = {"cursor": low}, 1
    while state["cursor"] < high and time.monotonic() < deadline:
        try:
            async with aclosing(jetstream.events(state["cursor"])) as stream:
                await pump(stream, scan, high, deadline, state)
        except RETRYABLE:
            pass  # a dropped connection loses nothing: the cursor resumes exactly where it stopped
        if state["cursor"] < high and time.monotonic() < deadline:
            await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay = min(delay * 2, 8)


async def replay(jetstream, scan, chunks, deadline):
    while chunks and time.monotonic() < deadline:
        low, high = chunks.pop()  # newest first, so a partial scan covers the most recent minutes
        await segment(jetstream, scan, low, high, deadline)


async def hydrate(session, uris, deadline):
    """Current engagement for up to HYDRATE_LIMIT uris, 25 per AppView call (morning-brief/briefing.py)."""
    views = {}
    batches = [uris[index:index + HYDRATE_BATCH] for index in range(0, min(len(uris), HYDRATE_LIMIT), HYDRATE_BATCH)]

    async def fetch(batch):
        for attempt in range(3):
            if time.monotonic() > deadline:
                return
            try:
                async with session.get(APPVIEW, params=[("uris", uri) for uri in batch],
                                       timeout=aiohttp.ClientTimeout(total=max(1.0, deadline - time.monotonic()))) as response:
                    if response.status == 429 or response.status >= 500:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    if response.status != 200:
                        return
                    data = await response.json(content_type=None)
            except RETRYABLE:
                await asyncio.sleep(0.5)
                continue
            for post in (data.get("posts") or []) if isinstance(data, dict) else []:
                author = post.get("author") if isinstance(post.get("author"), dict) else {}
                if isinstance(post.get("uri"), str):
                    views[post["uri"]] = {"likes": post.get("likeCount"), "reposts": post.get("repostCount"),
                                          "replies": post.get("replyCount"), "quotes": post.get("quoteCount"),
                                          "handle": author.get("handle") if isinstance(author.get("handle"), str) else None}
            return

    if batches:
        await asyncio.gather(*(fetch(batch) for batch in batches), return_exceptions=True)
    return views


def whole(value):
    """The AppView's counts, forced to a plain integer: a preview card must never render null or a float."""
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def examples(scan, views, label):
    rows = []
    for row in scan.candidates():
        view = views.get(row["uri"]) or {}
        url = f"https://bsky.app/profile/{row['did']}/post/{row['rkey']}" if SAFE_ID.fullmatch(row["did"]) and SAFE_ID.fullmatch(row["rkey"]) else None
        rows.append((whole(view.get("likes")) + whole(view.get("reposts")), row["t"], {
            "uri": row["uri"], "url": url, "handle": view.get("handle") or row["did"],
            "created_iso": iso(row["t"]), "time_label": local(row["t"], label),
            "like_count": whole(view.get("likes")), "repost_count": whole(view.get("reposts")),
            "reply_count": whole(view.get("replies")), "langs": row["langs"], "text": row["text"][:TEXT_LIMIT],
        }))
    rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [row[2] for row in rows[:MAX_EXAMPLES]]


def summary(scan, *, mode, covered, seconds, views, notes, label):
    covered = max(0.0, min(1.0, covered))
    minutes = (scan.to_ms - scan.from_ms) / 60_000
    window = {"from_iso": iso(scan.from_ms), "to_iso": iso(scan.to_ms), "minutes": round(minutes, 2)}
    if mode == "listen":
        window["seconds"] = round(minutes * 60)
    covered_minutes = minutes * covered
    notes = list(notes) + [ENGAGEMENT_NOTE]
    if scan.scanned == 0:
        notes.insert(0, "Nothing at all was scanned in the time available. The stream was stalled or refused, or the budget "
                        "was too short. This says nothing about the topic. Say so plainly and try again in a moment.")
    elif covered < 0.99 and mode == "listen":
        notes.insert(0, f"The live stream was only open for about {covered * 100:.0f}% of the listening window, so the counts are a floor.")
    elif covered < 0.99:
        notes.insert(0, f"Only about {covered * 100:.0f}% of the window was scanned in the time available, the most recent minutes first, "
                        "so every count is a floor rather than a total.")
    if scan.wanted:
        notes.append("A post whose record lists no language at all is kept by the language filter.")
    return {
        "source": "bluesky_live", "mode": mode, "keywords": scan.keywords, "language": next(iter(scan.wanted), None),
        "window": window, "covered_fraction": round(covered, 3), "scanned": scan.scanned, "matched": scan.matched,
        "per_bucket": scan.buckets(), "timezone": zone(), "examples": examples(scan, views, label),
        "rate_per_min": round(scan.matched / covered_minutes, 2) if covered_minutes > 0.01 else None,
        "seconds": round(seconds, 1), "notes": notes,
        "counts": "every public Bluesky post published in the window, matched on whole words in the text and in any link card",
    }


async def scan_recent(keywords, *, minutes=15, language=None, budget_s=40, connections=12):
    """Replay the last `minutes` of every public Bluesky post and count the keyword matches.

    The replay stops at budget_s whatever it has reached, and covered_fraction says how much of the
    window was actually seen; asking the AppView for engagement afterwards costs a few seconds more.
    """
    started = time.monotonic()
    minutes = max(1, min(60, int(minutes)))
    budget_s = max(1.0, float(budget_s))
    deadline = started + budget_s
    to_ms = now_ms() - LAG_MS
    from_ms = to_ms - minutes * 60_000
    scan = Scan(keywords, from_ms, to_ms, BUCKET_MS, language)
    notes, views = [], {}
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60))
    try:
        jetstream = Jetstream(session)
        found, refused = None, "Jetstream did not answer in time, so nothing was scanned."
        try:
            head_seq, head_ms = await asyncio.wait_for(jetstream.probe(None), max(1.0, deadline - time.monotonic()))
            found = await asyncio.wait_for(
                jetstream.seek(from_ms, head_seq, head_ms, max(5_000, minutes * 600), started + budget_s * 0.5),
                max(1.0, deadline - time.monotonic()))
        except (asyncio.TimeoutError, *RETRYABLE) as error:
            refused = f"Jetstream could not be reached, so nothing was scanned: {error!r}"[:200]
        if not found or found[0] is None:
            notes.append(refused)
            return summary(scan, mode="recent", covered=0.0, seconds=time.monotonic() - started, views={}, notes=notes, label="%H:%M")
        start_seq, start_ms = await place_start(jetstream, from_ms, head_seq, head_ms, found, started + budget_s * 0.7)
        span = max(1, head_seq - start_seq)
        edges = [start_seq + span * index // (connections * CHUNKS_PER_CONNECTION) for index in range(connections * CHUNKS_PER_CONNECTION + 1)]
        chunks = [(edges[index], edges[index + 1]) for index in range(len(edges) - 1) if edges[index] < edges[index + 1]]
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*(replay(jetstream, scan, chunks, deadline) for _ in range(max(1, connections))), return_exceptions=True),
                max(0.5, deadline - time.monotonic()) + 2)  # a worker blocked on a socket read is cancelled, not waited for
        views = await hydrate(session, [row["uri"] for row in scan.candidates()],
                              time.monotonic() + min(HYDRATE_BUDGET_S, max(1.5, budget_s * 0.2)))
    finally:
        await session.close()
    return summary(scan, mode="recent", covered=scan.coverage(), seconds=time.monotonic() - started, views=views, notes=notes, label="%H:%M")


async def listen_live(keywords, *, seconds=20, language=None):
    """Tail the live stream from now for `seconds` and report what went past."""
    started = time.monotonic()
    seconds = max(5, min(45, int(seconds)))
    deadline = started + seconds
    from_ms = now_ms()
    scan = Scan(keywords, from_ms, from_ms + seconds * 1000, LIVE_BUCKET_MS, language, label="%H:%M:%S")
    notes, connected, views = [], 0.0, {}
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30))
    try:
        jetstream = Jetstream(session)

        async def tail():
            nonlocal connected
            delay = 1
            while time.monotonic() < deadline:
                opened = time.monotonic()
                try:
                    async with aclosing(jetstream.events(None)) as stream:
                        await pump(stream, scan, None, deadline, {"cursor": 0})
                except RETRYABLE as error:
                    notes.append(f"The live stream dropped once and was reopened: {error!r}"[:160])
                connected += time.monotonic() - opened
                if time.monotonic() < deadline:
                    await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
                    delay = min(delay * 2, 8)

        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(tail(), seconds + 5)
        views = await hydrate(session, [row["uri"] for row in scan.candidates()], time.monotonic() + HYDRATE_BUDGET_S)
    finally:
        await session.close()
    return summary(scan, mode="listen", covered=min(1.0, connected / seconds), seconds=time.monotonic() - started,
                   views=views, notes=notes, label="%H:%M:%S")
