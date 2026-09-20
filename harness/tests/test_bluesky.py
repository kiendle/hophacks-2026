# /// script
# requires-python = ">=3.11"
# dependencies = ["aiohttp>=3.11,<4"]
# ///
"""Run: python -m uv run harness/tests/test_bluesky.py   (HARNESS_TEST_STEPS=1 skips the live network)

Steps 1-2 feed a fake event source, so the matcher, the buckets and the coverage arithmetic are
deterministic; steps 3-6 hit the real Jetstream and the real Bluesky AppView.
"""
import asyncio
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bluesky as B  # noqa: E402

STEPS = {step for step in (os.environ.get("HARNESS_TEST_STEPS") or "1,2,3,4,5,6").split(",")}
OUTCOMES = []
BASE = 1_700_000_000_000  # a fixed clock, so bucket labels and coverage are reproducible


def check(name, ok, detail=""):
    OUTCOMES.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""), flush=True)  # ASCII: the Windows console is cp1252


def event(at_ms, text, *, langs=("en",), did="did:plc:aaa", rkey=None, record=None, **extra):
    """One Jetstream commit payload, shaped exactly as the wire shapes it."""
    payload = {"$type": "network.bsky.jetstream#commit", "collection": B.POSTS, "operation": "create",
               "did": did, "rkey": rkey or f"r{at_ms}", "seq": at_ms, "time": B.iso(at_ms).replace("+00:00", "Z")}
    payload["record"] = record if record is not None else {"$type": B.POSTS, "text": text, "langs": list(langs), "createdAt": B.iso(at_ms)}
    return payload | extra


async def feed(scan, payloads, high=None, deadline=None):
    """The injected event source: pump() cannot tell it from a websocket."""
    async def stream():
        for payload in payloads:
            yield payload
    state = {"cursor": 0}
    await B.pump(stream(), scan, high, deadline if deadline is not None else time.monotonic() + 30, state)
    return state["cursor"]


def matcher():
    terms = B.compile_terms(["AI", "OpenAI", "sam altman"])
    cases = [
        ("Thoughts on AI today", True, "whole word AI"),
        ("said nothing at all", False, "AI is not inside 'said'"),
        ("ai is fine i guess", False, "short all-caps terms stay case-sensitive"),
        ("OPENAI shipped it", True, "long terms ignore case"),
        ("openai-adjacent", True, "a hyphen is a word boundary"),
        ("aircraft AIrport", False, "no match inside a longer word"),
        ("met SAM ALTMAN once", True, "a phrase, case-insensitively"),
    ]
    wrong = [why for text, want, why in cases if bool(terms.search(text)) is not want]
    check("1a whole-word matching", not wrong, f"{len(cases)} cases, wrong: {wrong}")
    check("1b short all-caps stay exact, the rest do not",
          B.compile_terms(["LLM"]).search("an LLM") and not B.compile_terms(["LLM"]).search("an llm")
          and B.compile_terms(["Claude"]).search("CLAUDE code"), "LLM vs llm, Claude vs CLAUDE")
    check("1c no keywords is refused", isinstance(_raises(lambda: B.compile_terms([" ", None])), ValueError), "compile_terms([]) raises")

    scan = B.Scan(["AI"], BASE, BASE + 300_000, 300_000)
    hostile = [  # every rkey differs: two posts with one uri are the same post, and are counted once
        event(BASE, "AI", rkey="h1", record="not a dict"),
        event(BASE, "AI", rkey="h2", record={"text": {"nested": "AI"}, "langs": ["en"]}),
        event(BASE, "AI", rkey="h3", record={"text": "AI " * 200_000, "langs": "en"}),  # huge text, langs not a list
        event(BASE, "AI", rkey="h4", record={"text": "about AI", "langs": [None, 7, "en"], "embed": "not a dict", "reply": 5}),
        event(BASE, "AI", rkey="h5", did=None),
        event(BASE, "AI", rkey={"oops": 1}),
        {"collection": B.POSTS, "operation": "create", "did": "did:plc:b", "rkey": "h6", "time": "not a date", "record": {"text": "AI"}},
        event(BASE, "AI", rkey="h7", record={"text": "AI in a card", "langs": ["en"],
                                             "embed": {"external": {"uri": "https://e.example", "title": None, "description": {"x": 1}}}}),
        {"collection": "app.bsky.feed.like", "operation": "create", "did": "d", "rkey": "h8", "time": B.iso(BASE), "record": {"text": "AI"}},
        event(BASE, "AI", rkey="h9", operation="delete", record=None),
    ]
    survived = True
    for payload in hostile:
        try:
            scan.feed(payload)
        except Exception as error:  # noqa: BLE001 - the whole point is that nothing escapes
            survived = False
            check("1d hostile record", False, f"{type(error).__name__}: {error} on {str(payload)[:80]}")
    check("1d a hostile post record never raises", survived, f"{len(hostile)} malformed payloads fed")
    check("1e only the sane hostile ones matched", scan.matched == 3 and scan.scanned == 7,
          f"matched {scan.matched} (huge text, langs [None,7,'en'], link card), scanned {scan.scanned} (likes, deletes and a non-string rkey excluded)")
    check("1f a link card is searched, a non-string one is not", B.extract(hostile[7])["haystack"] == "AI in a card"
          and B.extract(event(BASE, "x", rkey="c", record={"text": "t", "embed": {"external": {"title": "AI news"}}}))["haystack"] == "t\nAI news",
          repr(B.extract(hostile[7])["haystack"])[:60])
    check("1g text is truncated for the card, not for matching",
          len(B.examples(scan, {}, "%H:%M")[0]["text"]) == 240 and scan.matched == 3, "240 characters")
    rows = B.examples(scan, {}, "%H:%M")
    check("1h every example also carries the full post, for the card's Show full post",
          all(isinstance(row.get("full_text"), str) and row["full_text"].startswith(row["text"]) and len(row["full_text"]) <= 2000 for row in rows)
          and len(rows[0]["full_text"]) == 2000 and any(row["full_text"] == row["text"] for row in rows),
          f"full text lengths {[len(row['full_text']) for row in rows]}: the huge one is capped at 2,000, a short one is the same as its short form")


def _raises(call):
    try:
        call()
    except Exception as error:  # noqa: BLE001
        return error
    return None


async def arithmetic():
    minutes, window = 15, 15 * 60_000
    scan = B.Scan(["AI"], BASE, BASE + window, B.BUCKET_MS)
    check("2a buckets span the window on five-minute boundaries", len(scan.buckets()) == 4 and scan.total_slots == 100 and scan.slot_ms == 9000
          and [label[-2:] for label in (bucket["label"] for bucket in scan.buckets())] == ["10", "15", "20", "25"],
          f"{[bucket['label'] for bucket in scan.buckets()]}, {scan.total_slots} coverage slots of {scan.slot_ms} ms")

    # one matching post a second across the whole window, plus one non-matching post beside it
    payloads = []
    for offset in range(0, window, 1_000):
        payloads.append(event(BASE + offset, "all about AI"))
        payloads.append(event(BASE + offset + 1, "nothing to see"))
    await feed(scan, payloads)
    counts = [bucket["count"] for bucket in scan.buckets()]
    check("2b every post lands in its own bucket", sum(counts) == 900 and counts == [100, 300, 300, 200] and scan.scanned == 1800 and scan.matched == 900,
          f"counts {counts}, scanned {scan.scanned}, matched {scan.matched}")
    check("2c a fully seen window is covered", scan.coverage() == 1.0 and B.summary(
        scan, mode="recent", covered=scan.coverage(), seconds=1.0, views={}, notes=[], label="%H:%M")["rate_per_min"] == 60.0,
        f"covered {scan.coverage()}, 60.0 matches a minute")

    half = B.Scan(["AI"], BASE, BASE + window, B.BUCKET_MS)
    await feed(half, [payload for payload in payloads if payload["seq"] >= BASE + window // 2])
    out = B.summary(half, mode="recent", covered=half.coverage(), seconds=1.0, views={}, notes=[], label="%H:%M")
    check("2d half a window is reported as half", 0.49 <= half.coverage() <= 0.51 and out["covered_fraction"] == round(half.coverage(), 3)
          and "floor" in out["notes"][0] and out["rate_per_min"] == round(half.matched / (minutes * half.coverage()), 2),
          f"covered {out['covered_fraction']}, matched {half.matched}, rate {out['rate_per_min']}/min, note: {out['notes'][0][:58]}…")

    empty = B.Scan(["zzqxv"], BASE, BASE + window, B.BUCKET_MS)
    await feed(empty, payloads)
    blank = B.summary(empty, mode="recent", covered=empty.coverage(), seconds=1.0, views={}, notes=[], label="%H:%M")
    check("2e a covered but empty window is honest", blank["matched"] == 0 and blank["covered_fraction"] == 1.0 and blank["rate_per_min"] == 0.0
          and blank["examples"] == [] and [b["count"] for b in blank["per_bucket"]] == [0, 0, 0, 0] and blank["scanned"] == 1800,
          f"scanned {blank['scanned']}, matched 0, covered 1.0")

    twice = B.Scan(["AI"], BASE, BASE + window, B.BUCKET_MS)
    await feed(twice, payloads + payloads)
    check("2f a repeated event is counted once", twice.matched == 900 and twice.scanned == 1800, f"{twice.scanned} scanned from 3600 payloads")

    stop = B.Scan(["AI"], BASE, BASE + window, B.BUCKET_MS)
    cursor = await feed(stop, payloads, high=BASE + 60_000)
    check("2g pump stops at the chunk boundary", cursor == BASE + 60_000 and stop.matched == 60,
          f"cursor stopped at high, {stop.matched} matches before it")

    language = B.Scan(["AI"], BASE, BASE + window, B.BUCKET_MS, language="ja")
    await feed(language, [event(BASE, "AI ja", langs=["ja"], rkey="a"), event(BASE, "AI en", langs=["en"], rkey="b"),
                          event(BASE, "AI none", langs=[], rkey="c")])
    check("2h the language filter keeps its own language and the unlabelled", language.matched == 2,
          "ja kept, en dropped, a post with no langs kept")


async def live_listen():
    started = time.monotonic()
    out = await B.listen_live(["the"], seconds=8)
    # 250, not 300: the live post rate measured from this machine is 34-47 a second, so 8 seconds carries 270-380 posts.
    check("3 listen_live tails the real stream", out["scanned"] > 250 and out["matched"] > 0 and out["source"] == "bluesky_live"
          and out["mode"] == "listen" and len(out["per_bucket"]) in (2, 3) and out["window"]["seconds"] == 8
          and time.monotonic() - started < 20,
          f"scanned {out['scanned']} posts in 8 s, matched {out['matched']}, covered {out['covered_fraction']}, "
          f"{len(out['per_bucket'])} buckets of 5 s, {out['seconds']}s wall clock")
    check("3 the live examples are usable", out["examples"] and all(
        post["url"].startswith("https://bsky.app/profile/") and isinstance(post["like_count"], int) and isinstance(post["text"], str)
        for post in out["examples"]),
        f"{len(out['examples'])} examples, first: {(out['examples'] or [{}])[0].get('url', '')[:64]}")


async def live_scan():
    started = time.monotonic()
    out = await B.scan_recent(["AI", "OpenAI", "ChatGPT"], minutes=10, budget_s=40)
    elapsed = time.monotonic() - started
    check("4 scan_recent replays the last ten minutes", elapsed < 50 and out["covered_fraction"] > 0.5 and out["matched"] > 0
          and out["scanned"] > 1000 and out["window"]["minutes"] == 10.0 and len(out["per_bucket"]) in (3, 4),
          f"{out['scanned']:,} posts scanned, {out['matched']} matched, covered {out['covered_fraction']}, "
          f"{out['seconds']}s reported / {elapsed:.1f}s wall clock, {out['rate_per_min']}/min")
    urls = [post["url"] for post in out["examples"]]
    likes = [post["like_count"] for post in out["examples"]]
    check("4 examples carry real links and integer engagement", urls and all(
        isinstance(url, str) and url.startswith("https://bsky.app/profile/") for url in urls) and all(isinstance(like, int) for like in likes)
        and all(re.fullmatch(r"\d{2}:\d{2}", post["day"] if "day" in post else post["time_label"]) for post in out["examples"]),
        f"{len(urls)} examples, likes {likes}, first {urls[0][:72] if urls else ''}")
    check("4 the engagement caveat is always stated", any("current totals" in note for note in out["notes"])
          and out["timezone"] and out["window"]["from_iso"].endswith("+00:00"),
          f"timezone {out['timezone']!r}, window {out['window']['from_iso']} to {out['window']['to_iso']}")
    return out


async def live_quiet():
    out = await B.scan_recent(["zzqxv-no-such-word"], minutes=5, budget_s=40)
    check("5 a word nobody posts returns zero, not an error", out["matched"] == 0 and out["examples"] == []
          and out["scanned"] > 1000 and out["covered_fraction"] > 0.5 and out["rate_per_min"] == 0.0
          and all(bucket["count"] == 0 for bucket in out["per_bucket"]),
          f"scanned {out['scanned']:,} posts over {out['covered_fraction'] * 100:.0f}% of 5 minutes, matched 0, {out['seconds']}s")


async def live_budget():
    started = time.monotonic()
    out = await B.scan_recent(["AI"], minutes=30, budget_s=3)
    elapsed = time.monotonic() - started
    # Three seconds barely pays for the probes that find the window, so this may scan a slice or nothing at all;
    # either way it must come back quickly and say what it did not see.
    check("6 a three-second budget is respected", elapsed < 12 and out["covered_fraction"] < 1.0
          and any("floor" in note or "Nothing at all" in note for note in out["notes"]),
          f"{elapsed:.1f}s wall clock for a 30-minute window, covered {out['covered_fraction'] * 100:.0f}%, "
          f"{out['scanned']:,} posts scanned, matched {out['matched']}, first note: {out['notes'][0][:60]}…")


async def main():
    if "1" in STEPS:
        matcher()
        await arithmetic()
    if "3" in STEPS:
        await live_listen()
    if "4" in STEPS:
        await live_scan()
    if "5" in STEPS:
        await live_quiet()
    if "6" in STEPS:
        await live_budget()
    print(f"\n{sum(OUTCOMES)}/{len(OUTCOMES)} checks passed", flush=True)
    return 0 if all(OUTCOMES) and OUTCOMES else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
