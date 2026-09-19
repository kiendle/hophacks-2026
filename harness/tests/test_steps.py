# /// script
# requires-python = ">=3.11"
# ///
"""Run: python -m uv run harness/tests/test_steps.py

steps.py writes the words a person reads for every real tool call: WHAT is being done, from the
call's own arguments, and WHAT CAME BACK, from the tool's own result. It also owns plain(), the one
place the punctuation rule lives: a reader gets full stops, commas, "and" and "to", and nothing else.
Both sides are attacker- or model-controlled, so every function here must survive nonsense unraised.
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(errors="replace")  # the Windows console is cp1252

import steps  # noqa: E402

OUTCOMES = []
# what a person must never see: a dash used as punctuation, a middle dot, an arrow, a semicolon
BANNED_CHARS = "‒–—―·•‣▪・←→↔⇒⇨➡;"
BANNED_TEXT = re.compile(r"->|=>|\+\d+ more")
BANNED_WORDS = re.compile(r"(?i)\b(original|originals|firehose|utc|exclusive|version|keyword|keywords)\b")


def check(name, ok, detail=""):
    OUTCOMES.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""), flush=True)


def same(name, got, want):
    check(name, got == want, repr(got) if got == want else f"got {got!r}, want {want!r}")


def dirt(text):
    """Everything in one line a person must never read."""
    found = sorted({character for character in text if character in BANNED_CHARS})
    found += sorted(set(BANNED_TEXT.findall(text)))
    found += sorted({word.lower() for word in BANNED_WORDS.findall(text)})
    return found


PREVIEW_IN = {"reason": "Checking how loud that day really was.", "keywords": ["PlayStation", "PS5", "Sony", "PS6", "Kojima"],
              "date_from": "2026-09-09", "date_to": "2026-09-12", "language": "en"}
PREVIEW_OUT = {"keywords": ["PlayStation", "PS5"], "date_from": "2026-09-09", "date_to": "2026-09-12", "language": "en",
               "exact": True, "files_scanned": 14, "seconds": 8.4, "total": 1522,
               "per_day": [{"day": "2026-09-09", "count": 402}, {"day": "2026-09-10", "count": 610}, {"day": "2026-09-11", "count": 510}],
               "examples": [{"id": "1", "day": "2026-09-09", "like_count": 801270, "lang": "en", "body": "no way they cancelled it"}]}
LIVE_OUT = {"mode": "recent", "window": {"minutes": 15}, "matched": 874, "scanned": 48001, "covered_fraction": 1.0,
            "seconds": 12.7, "per_bucket": [{"label": "18:30", "count": 19}], "examples": []}
SOURCES_OUT = {"sources": [{"id": "bluesky_live", "name": "live Bluesky"},
                           {"id": "twitter_firehose", "name": "the X/Twitter archive"},
                           {"id": "congress", "name": "US Congress posts"}],
               "preview_window_limit_days": 3}
DRAFT = {"name": "PHYSINT cancellation reaction", "keywords": ["playstation", "ps6"], "language": "en"}
SAVE_OUT = {"spec": DRAFT, "spec_hash": "3f9c1ab2d47e55f0a1b2c3d4e5f60718", "saved": True,
            "draft_path": "harness/state/sessions/6f2a/draft.json"}
CONFIRM_OUT = {"confirmation_id": "Zc7mQr1sTgHv2Kpx", "summary": "Name: PHYSINT cancellation reaction",
               "expires_ms": int(time.time() * 1000) + 300_000, "spec": DRAFT, "spec_hash": SAVE_OUT["spec_hash"],
               "draft_path": SAVE_OUT["draft_path"]}
SUBMIT_OUT = {"project_id": "proj_0c35e949", "status": "submitted", "note": "demo: the real pipeline is not connected yet"}
ERROR_OUT = {"error": {"code": "window_too_large", "message": "A preview can cover 1 to 3 days, and that one asked for 5.",
                       "hint": "Preview the busiest three days."}}

print("plain punctuation: what a person is allowed to read")
same("an em dash between words", steps.plain("Sony — sorry, Sony"), "Sony, sorry, Sony")
same("an en dash with no spaces", steps.plain("Sep 9–Sep 11"), "Sep 9, Sep 11")
same("a horizontal bar", steps.plain("live Bluesky ― the archive"), "live Bluesky, the archive")
same("a middle dot separator", steps.plain("live Bluesky · X/Twitter"), "live Bluesky, X/Twitter")
same("a bullet separator", steps.plain("one • two"), "one, two")
same("a real arrow", steps.plain("Sep 9 → Sep 11"), "Sep 9 to Sep 11")
same("a typed arrow", steps.plain("Sep 9 -> Sep 11"), "Sep 9 to Sep 11")
same("a fat arrow", steps.plain("a => b"), "a to b")
same("a semicolon starts a new sentence", steps.plain("it worked; the count is 41"), "it worked. The count is 41")
same("a semicolon at the end", steps.plain("it worked;"), "it worked.")
same("a hyphen inside a word survives", steps.plain("a well-known co-founder"), "a well-known co-founder")
same("an ISO date survives", steps.plain("saved on 2026-09-10"), "saved on 2026-09-10")
same("thousands separators survive", steps.plain("1,522 posts and 801,270 likes"), "1,522 posts and 801,270 likes")
same("a lone spaced hyphen is left alone", steps.plain("PS5 - PS6"), "PS5 - PS6")
same("no space before a comma", steps.plain("posts , then more"), "posts, then more")
same("no doubled comma", steps.plain("a — , b"), "a, b")
same("no doubled space", steps.plain("a    b"), "a b")
same("a comma before a full stop goes", steps.plain("we checked it —."), "we checked it.")
same("no leading comma", steps.plain("— we checked it"), "we checked it")
same("no trailing comma", steps.plain("we checked it —"), "we checked it")
same("newlines are kept, each line tidied", steps.plain("Name: x —\n— Where: live Bluesky"), "Name: x\nWhere: live Bluesky")
same("a fragment can keep its joining comma", steps.plain(" — and then", trim=False), ", and then")
HOSTILE = ("Found 1,522 posts — most on 2026-09-10 · well-known words; see A -> B ⇒ C ; and – more ,, "
           "then 801,270 likes ➡ done ・ over‒there")
same("nothing banned survives a hostile line", dirt(steps.plain(HOSTILE)), [])
same("plain() is idempotent", steps.plain(steps.plain(HOSTILE)), steps.plain(HOSTILE))
check("the hostile line still reads as words", steps.plain(HOSTILE).startswith("Found 1,522 posts, most on 2026-09-10, well-known words. See A to B to C. And, more, then"),
      steps.plain(HOSTILE))
for junk in (None, 5, 3.5, [], {}, True, b"bytes", object()):
    same(f"{type(junk).__name__} is not text", steps.plain(junk), "")

print("titles: what is being done, from the real inputs")
same("preview_keywords", steps.title("mcp__harness__preview_keywords", PREVIEW_IN),
     'Searching X/Twitter for "PlayStation", "PS5", "Sony" and 2 more words, from Sep 9 to Sep 11, in English')
same("preview_keywords, one day, japanese", steps.title("preview_keywords", {"keywords": ["PS6"], "date_from": "2026-09-09", "date_to": "2026-09-10", "language": "ja"}),
     'Searching X/Twitter for "PS6", on Sep 9, in Japanese')
same("preview_keywords, no language", steps.title("preview_keywords", {"keywords": ["PS6"], "date_from": "2026-08-17", "date_to": "2026-08-19"}),
     'Searching X/Twitter for "PS6", from Aug 17 to Aug 18')
same("a fourth word becomes one more word", steps.title("preview_keywords", {"keywords": ["a", "b", "c", "d"], "date_from": "2026-09-09", "date_to": "2026-09-10"}),
     'Searching X/Twitter for "a", "b", "c" and 1 more word, on Sep 9')
same("a window across the year boundary", steps.title("preview_keywords", {"keywords": ["PS6"], "date_from": "2026-12-31", "date_to": "2027-01-02"}),
     'Searching X/Twitter for "PS6", from Dec 31 to Jan 1 2027')
same("a window over a leap day", steps.title("preview_keywords", {"keywords": ["PS6"], "date_from": "2028-02-27", "date_to": "2028-03-01"}),
     'Searching X/Twitter for "PS6", from Feb 27 2028 to Feb 29 2028')
same("bluesky_recent", steps.title("mcp__harness__bluesky_recent", {"reason": "r", "keywords": ["AI", "OpenAI"], "minutes": 15}),
     'Searching the last 15 minutes of Bluesky for "AI", "OpenAI"')
same("bluesky_recent uses the tool's own default", steps.title("bluesky_recent", {"keywords": ["AI"]}),
     'Searching the last 15 minutes of Bluesky for "AI"')
same("bluesky_recent, one minute, one language", steps.title("bluesky_recent", {"keywords": ["AI"], "minutes": 1, "language": "tr"}),
     'Searching the last 1 minute of Bluesky for "AI", in Turkish')
same("bluesky_listen", steps.title("mcp__harness__bluesky_listen", {"keywords": ["AI"], "seconds": 20}),
     'Listening to Bluesky live for 20 seconds for "AI"')
same("bluesky_listen default", steps.title("bluesky_listen", {"keywords": ["PS6"]}),
     'Listening to Bluesky live for 20 seconds for "PS6"')
same("describe_sources", steps.title("mcp__harness__describe_sources", {"reason": "r"}), "Checking what data we have")
same("save_draft", steps.title("mcp__harness__save_draft", {"spec_json": "{}"}), "Saving your project")
same("request_confirmation", steps.title("mcp__harness__request_confirmation", {}), "Getting your project ready for you to confirm")
same("submit_project", steps.title("mcp__harness__submit_project", {}), "Sending your project")
same("an unknown tool says nothing clever", steps.title("mcp__harness__run_sql_query", {"sql": "select 1"}), "Working on it")
same("an unknown language keeps its code", steps.title("preview_keywords", {"keywords": ["x"], "language": "zz"}),
     'Searching X/Twitter for "x", in zz')

print("results: what came back, from the real result")
same("preview_keywords", steps.outcome("mcp__harness__preview_keywords", PREVIEW_OUT), "Found 1,522 posts. Most were on Sep 10 (610).")
same("preview_keywords, nothing matched", steps.outcome("preview_keywords", {"total": 0, "per_day": []}), "No posts matched those words.")
same("preview_keywords, one single post", steps.outcome("preview_keywords", {"total": 1, "per_day": [{"day": "2026-09-10", "count": 1}]}),
     "Found 1 post. Most were on Sep 10 (1).")
same("preview_keywords, thousands separators", steps.outcome("preview_keywords", {"total": 1234567, "per_day": [{"day": "2026-08-20", "count": 987654}]}),
     "Found 1,234,567 posts. Most were on Aug 20 (987,654).")
same("bluesky_recent", steps.outcome("mcp__harness__bluesky_recent", LIVE_OUT), "Found 874 posts out of 48,001 checked.")
same("bluesky_recent, partial coverage", steps.outcome("bluesky_recent", LIVE_OUT | {"covered_fraction": 0.8}),
     "Found 874 posts out of 48,001 checked. Only about 80% of that time could be checked, so the real number is higher.")
same("bluesky_listen, nothing went past", steps.outcome("bluesky_listen", {"matched": 0, "scanned": 2840, "covered_fraction": 1.0}),
     "No posts matched those words. We checked 2,840 posts.")
same("describe_sources", steps.outcome("mcp__harness__describe_sources", SOURCES_OUT),
     "We have 3 sources: live Bluesky, the X/Twitter archive and US Congress posts.")
same("describe_sources falls back to the real name", steps.outcome("describe_sources", {"sources": [{"id": "reddit_live", "name": "Reddit, live"}]}),
     "We have 1 source: Reddit, live.")
same("save_draft", steps.outcome("mcp__harness__save_draft", SAVE_OUT), 'Saved as "PHYSINT cancellation reaction".')
same("save_draft without a name", steps.outcome("save_draft", {"spec": {}, "spec_hash": "ab" * 8}), 'Saved as "your project".')
same("request_confirmation", steps.outcome("mcp__harness__request_confirmation", CONFIRM_OUT),
     "Waiting for you to press Confirm. The button works for 5 minutes.")
same("request_confirmation, seconds left", steps.outcome("request_confirmation", CONFIRM_OUT | {"expires_ms": int(time.time() * 1000) + 20_000}),
     "Waiting for you to press Confirm. The button works for less than a minute.")
same("submit_project", steps.outcome("mcp__harness__submit_project", SUBMIT_OUT), "Your project was sent. Its reference is proj_0c35e949.")
same("a structured error", steps.outcome("mcp__harness__preview_keywords", ERROR_OUT),
     "That did not work: A preview can cover 1 to 3 days, and that one asked for 5.")
same("the reason guard's own error", steps.outcome("describe_sources", {"error": {"code": "no_reason", "message": "Every step needs a reason, one short plain sentence for the user."}}),
     "That did not work: Every step needs a reason, one short plain sentence for the user.")
same("a failed call whose text is not our own", steps.outcome("preview_keywords", "Input validation error: reason Field required", True),
     "That did not work: Input validation error: reason Field required.")
same("a failure with nothing to say", steps.outcome("preview_keywords", None, True), "That did not work.")
same("an error line is put through plain() too", steps.outcome("preview_keywords", {"error": {"message": "bad dates — try 2026-09-09; then again"}}),
     "That did not work: bad dates, try 2026-09-09. Then again.")
same("an unknown tool", steps.outcome("run_sql_query", {"rows": 3}), "Done.")
same("an unparseable result", steps.outcome("preview_keywords", None), "Done.")

print("card words shared with the confirm summary")
same("a window's last day is the day before the end date", steps.window_words("2026-09-09", "2026-09-12"), "Sep 9 to Sep 11, 2026")
same("a one-day window", steps.window_words("2026-09-09", "2026-09-10"), "Sep 9, 2026")
same("a whole month", steps.window_words("2026-08-17", "2026-09-18"), "Aug 17 to Sep 17, 2026")
same("a live window", steps.live_words(2, 24), "From now, also looking back 2 hours, and it keeps running for 24 hours")
same("a live window with no look back", steps.live_words(0, 1), "From now, and it keeps running for 1 hour")
same("a language name, never a code", steps.language_words("en"), "English")
same("no language at all", steps.language_words(None), "Any language")
same("a source in words", steps.source_words("twitter_firehose"), "the X/Twitter archive")
same("an unknown source still reads", steps.source_words("reddit_live"), "reddit live")
same("the words we search for", steps.word_list(["PlayStation", "PS5"]), '"PlayStation", "PS5"')

print("nothing a person reads carries a banned character or a banned word")
REAL = [
    ("mcp__harness__describe_sources", {"reason": "Checking what we can look at."}, SOURCES_OUT),
    ("mcp__harness__preview_keywords", PREVIEW_IN, PREVIEW_OUT),
    ("mcp__harness__preview_keywords", PREVIEW_IN, {"total": 0, "per_day": []}),
    ("mcp__harness__preview_keywords", PREVIEW_IN, ERROR_OUT),
    ("mcp__harness__bluesky_recent", {"keywords": ["AI", "OpenAI"], "minutes": 15, "language": "en"}, LIVE_OUT),
    ("mcp__harness__bluesky_recent", {"keywords": ["AI"], "minutes": 60}, LIVE_OUT | {"covered_fraction": 0.62}),
    ("mcp__harness__bluesky_listen", {"keywords": ["AI"], "seconds": 20}, {"matched": 0, "scanned": 2840, "covered_fraction": 1.0}),
    ("mcp__harness__save_draft", {"spec_json": "{}"}, SAVE_OUT),
    ("mcp__harness__request_confirmation", {}, CONFIRM_OUT),
    ("mcp__harness__submit_project", {}, SUBMIT_OUT),
    ("mcp__harness__run_sql_query", {"sql": "select 1"}, {"rows": 3}),
]
seen, spotted = [], []
for tool, tool_input, result in REAL:
    for line in (steps.title(tool, tool_input), steps.outcome(tool, result)):
        seen.append(line)
        if dirt(line):
            spotted.append(f"{tool}: {line!r} carries {dirt(line)}")
check(f"{len(seen)} real lines are clean", not spotted, "; ".join(spotted[:3]) or "no dash, dot, arrow, semicolon or jargon")
check("every real line is a full sentence or a title", all(line and line[0].isupper() for line in seen),
      repr(next((line for line in seen if not (line and line[0].isupper())), None)))

print("garbage never raises")
JUNK = [None, [], {}, 0, 7, -3.5, "", "  ", float("nan"), float("inf"), True, ["a", None, 3], {"total": "many"},
        {"total": None, "per_day": "nope"}, {"per_day": [None, 3, {"count": "x"}, {"count": 5}]}, {"error": []},
        {"error": {"message": None}}, {"sources": "three"}, {"sources": [None, 3, {}]}, {"spec": "text", "spec_hash": 5},
        {"spec_hash": None}, {"confirmation_id": 7}, {"confirmation_id": "x" * 40, "expires_ms": "soon"},
        {"matched": "many", "scanned": None}, {"matched": 3, "covered_fraction": "half"}, {"project_id": []},
        {"keywords": "not a list"}, {"keywords": [{"a": 1}]}, {"date_from": 20260909, "date_to": []},
        {"date_from": "2026-99-99", "date_to": "not-a-date"}, {"date_from": "2026-01-01-01", "date_to": "2026"},
        {"minutes": "fifteen"}, {"seconds": -4}, {"language": 7}, {"reason": 5}, "x" * 100_000,
        {"keywords": ["x" * 100_000], "date_from": "x" * 100_000}, {"total": 10 ** 30},
        {"keywords": ["a — b", "c; d"], "date_from": "2026-09-09", "date_to": "2026-09-10"}]
TOOLS = ["mcp__harness__preview_keywords", "bluesky_recent", "bluesky_listen", "describe_sources", "save_draft",
         "request_confirmation", "submit_project", "who_knows", "", None, 7, ["x"], "x" * 100_000]
problems, unclean, calls = [], [], 0
for tool in TOOLS:
    for junk in JUNK:
        for is_error in (False, True):
            calls += 1
            try:
                line = steps.title(tool, junk), steps.outcome(tool, junk, is_error)
            except Exception as error:  # noqa: BLE001 - that is the point of this loop
                problems.append(f"{tool!r} {str(junk)[:30]!r}: {error!r}")
                continue
            if not all(isinstance(part, str) for part in line) or any(len(part) > 4000 for part in line):
                problems.append(f"{tool!r} {str(junk)[:30]!r}: {[part[:40] for part in line]}")
            for part in line:
                if any(character in BANNED_CHARS for character in part):
                    unclean.append(f"{tool!r} {str(junk)[:30]!r}: {part[:60]!r}")
check(f"{calls} junk calls return a short string and never raise", not problems, "; ".join(problems[:3]) or "no exception, no runaway length")
check("even a hostile argument cannot smuggle a banned character into a line", not unclean, "; ".join(unclean[:3]) or "titles and results stay clean")
check("a 100,000-character word is cut down", len(steps.title("preview_keywords", {"keywords": ["x" * 100_000]})) < 140,
      steps.title("preview_keywords", {"keywords": ["x" * 100_000]})[:70] + "…")
same("a nan count is ignored", steps.outcome("preview_keywords", {"total": float("nan")}), "Done.")

print(f"\n{sum(OUTCOMES)}/{len(OUTCOMES)} checks passed", flush=True)
sys.exit(0 if all(OUTCOMES) and OUTCOMES else 1)
