"""Every step the user sees is real. WHAT is being done comes from the tool call's own inputs
(`title`); WHAT CAME BACK is written here from the tool's real result (`outcome`), so the model can
never invent an outcome; the WHY is the model's own `reason` argument, shown as it wrote it.

Everything here is written for a person who is not a technician: short plain sentences, everyday
words, and no symbol punctuation at all. `plain()` is the one place that rule is enforced, and the
runner puts the model's own words through it too. Pure functions, no I/O: inputs and results are
untrusted, so nothing here may raise.
"""
import re
import time

DONE = "Done."
BASE_YEAR = 2026  # the demo's data year: a date inside it needs no year on screen
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DAYS_IN = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
LIVE_DEFAULTS = {"minutes": 15, "seconds": 20}  # the tools' own defaults, so an omitted argument still reads truthfully
LANGUAGES = {"en": "English", "ja": "Japanese", "es": "Spanish", "pt": "Portuguese", "ko": "Korean",
             "fr": "French", "de": "German", "tr": "Turkish", "ar": "Arabic", "it": "Italian",
             "zh": "Chinese", "ru": "Russian", "nl": "Dutch", "hi": "Hindi"}
SOURCE_NAMES = {"bluesky_live": "live Bluesky", "twitter_firehose": "the X/Twitter archive", "congress": "US Congress posts"}
TITLES = {"describe_sources": "Checking what data we have", "save_draft": "Saving your project",
          "request_confirmation": "Getting your project ready for you to confirm",
          "submit_project": "Sending your project"}

# ---------------------------------------------------------------- plain words
# The user reads commas, full stops, "and" and "to". Nothing else. These patterns are the only
# place that is decided, so a title, an outcome, a card label and the model's own answer all obey it.
_ARROWS = re.compile(r"[^\S\n]*(?:[←→↔⇒⇨➡]+|-{1,2}>|=>)[^\S\n]*")
_DASHES = re.compile(r"[^\S\n]*[‒–—―]+[^\S\n]*")
_DOTS = re.compile(r"[^\S\n]*[·•‣▪・]+[^\S\n]*")
# a plain hyphen standing in for a dash: spaces on both sides and a word before it on the same line,
# which is what a model reaches for once it has been told not to type an em dash
_HYPHEN = re.compile(r"(?<=\S)[^\S\n]+(?:-{1,2}[^\S\n]+)+(?=\S)")  # "a - b", and "a - - b" in one pass
# a semicolon OR a run of them (";;", "; ;") in one match: taken one at a time, "a;;b" came out as
# "a. ;b" and only a second pass finished it, so plain() was not idempotent
_SEMICOLON = re.compile(r"[^\S\n]*(?:;[^\S\n]*)+(\S?)")
_RUNS = re.compile(r"[^\S\n]{2,}")
_BEFORE = re.compile(r"[^\S\n]+([,.])")
_REPEATS = re.compile(r"(?:,[^\S\n]*){2,}")
_COMMA_STOP = re.compile(r",[^\S\n]*\.")
_EDGES = re.compile(r"(?m)^[^\S\n]*,[^\S\n]*|[^\S\n]*,[^\S\n]*$")


def _uncomma(match):
    """One full stop for the whole run, and a capital on the word after it. chat.js's unsemicolon is
    the same function: a sentence that had already ended ("e.g.; x") or a line that opens with a
    semicolon does not get a second stop."""
    tail = match.group(1)
    before = match.string[:match.start()]
    opens_line = not before or before.endswith("\n")
    stop = "" if opens_line or before.endswith((".", "!", "?")) else "."
    if not tail:
        return stop
    if tail in ".,!?":  # the mark that follows does the semicolon's job
        return tail if stop else ""
    return ("" if opens_line else stop + " ") + tail.upper()


def plain(value, trim=True):
    """Plain punctuation, for anything a person reads. Idempotent, and never raises.

    A dash of any kind becomes ", ", including a spaced hyphen ("posts - most on Sep 10"), a middle
    dot or bullet used as a separator becomes ", ", an arrow becomes " to ", a semicolon becomes a
    full stop and the next word gets a capital. Hyphens inside words (well-known), dates
    (2026-09-10), negative numbers (-5), a line that starts with "- " and thousands (1,522) survive.
    `trim` is off for a streamed fragment, whose leading comma may be the join to the last chunk.
    """
    if not isinstance(value, str):
        return ""
    text = _RUNS.sub(" ", value)  # first: the patterns below scan the spaces around a dash, and a
    text = _ARROWS.sub(" to ", text)  # long run of them would make each of those scans quadratic
    text = _DASHES.sub(", ", text)
    text = _DOTS.sub(", ", text)
    text = _HYPHEN.sub(", ", text)
    text = _SEMICOLON.sub(_uncomma, text)
    text = _RUNS.sub(" ", text)
    text = _BEFORE.sub(r"\1", text)
    text = _REPEATS.sub(", ", text)
    text = _COMMA_STOP.sub(".", text)
    return _EDGES.sub("", text) if trim else text


def _text(value, limit=80):
    return " ".join(str(value).split())[:limit] if isinstance(value, str) else ""


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _count(value):
    number = _number(value)
    return "" if number is None else f"{number:,.0f}"


def _posts(count):
    """"1 post", "1,522 posts": a count a person reads, with the right ending."""
    return f"{count:,.0f} post" + ("" if count == 1 else "s")


def _name(tool):
    return _text(tool, 80).rsplit("__", 1)[-1]


def _plural(count, word):
    return f"{count:g} {word}" if count == 1 else f"{count:g} {word}s"


def _sentence(text):
    text = plain(text)
    return text if not text or text[-1] in ".!?" else text + "."


def _and(names):
    names = [name for name in names if name]
    if len(names) < 2:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " and " + names[-1]


def _ymd(value, shift=0):
    try:
        year, month, day = (int(part) for part in _text(value, 24).split("-"))
        day += shift
        while day < 1:
            month -= 1
            if month < 1:
                month, year = 12, year - 1
            day += DAYS_IN[month - 1] + (month == 2 and year % 4 == 0 and (year % 100 != 0 or year % 400 == 0))
        if not 1 <= month <= 12 or day > 31:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    return year, month, day


def _month_day(value, shift=0):
    """"2026-09-09" to Sep 9, with no year at all, for a caller that writes the year itself."""
    parts = _ymd(value, shift)
    return f"{MONTHS[parts[1] - 1]} {parts[2]}" if parts else ""


def _day(value, shift=0):
    """"2026-09-09" to Sep 9. The year appears only when it is not the year the data is from."""
    parts = _ymd(value, shift)
    if not parts:
        return ""
    return _month_day(value, shift) + ("" if parts[0] == BASE_YEAR else f" {parts[0]}")


def _when(fields):
    """The tool's end date is the first day NOT observed, so the last day shown is the one before it."""
    low, high = _day(fields.get("date_from")), _day(fields.get("date_to"), shift=-1)
    if low and high and low != high:
        return f"from {low} to {high}"
    return f"on {low or high}" if (low or high) else ""


def _words(value, most=3):
    """'"PlayStation", "PS5", "Sony" and 2 more words'."""
    items = [f'"{_text(word, 40)}"' for word in value if _text(word, 40)] if isinstance(value, list) else []
    if not items:
        return ""
    extra = len(items) - most
    return ", ".join(items[:most]) + (f" and {_plural(extra, 'more word')}" if extra > 0 else "")


def _language(value):
    code = _text(value, 16)
    return LANGUAGES.get(code.lower(), code) if code else ""


def _span(value, fallback):
    number = _number(value)
    return fallback if number is None or number <= 0 else number


def _problem(result, is_error):
    if isinstance(result, dict):
        error = result.get("error")
        if isinstance(error, dict):
            return _text(error.get("message"), 160) or _text(error.get("code"), 60)
        return _text(error, 160) or ("something went wrong" if error else "")
    return _text(result, 160) if is_error else ""


def _peak(per_day):
    best = None
    for bucket in per_day if isinstance(per_day, list) else []:
        if not isinstance(bucket, dict):
            continue
        count = _number(bucket.get("count"))
        if count is not None and (best is None or count > best[0]):
            best = (count, _day(bucket.get("day")) or _text(bucket.get("label"), 24) or _text(bucket.get("day"), 24))
    return "" if best is None or not best[1] else f"{best[1]} ({best[0]:,.0f})"


def _left(expires_ms):
    """The card's own deadline, in words. Absolute epoch ms, or a duration."""
    raw = _number(expires_ms)
    if raw is None:
        return ""
    remaining = raw - time.time() * 1000 if raw > 1e12 else raw
    if remaining <= 0:
        return ""
    return "less than a minute" if remaining < 60_000 else _plural(round(remaining / 60_000), "minute")


# ------------------------------------------------- words the cards share with the server
def language_words(value):
    return _language(value) or "Any language"


def source_words(value):
    key = _text(value, 40)
    return SOURCE_NAMES.get(key) or _text(key.replace("_", " "), 40)


def word_list(values, most=20):
    items = [f'"{_text(word, 40)}"' for word in values if _text(word, 40)] if isinstance(values, list) else []
    return ", ".join(items[:most])


def window_words(date_from, date_to):
    """The dates a person reads: Sep 9 to Sep 11, 2026. The end date is the first day not observed.

    The year is written once, at the end, so the two days are asked for without one: _day() adds the
    year itself for any year but the data's, which used to give "Jan 5 2024 to Jan 7 2024, 2024".
    """
    parts = _ymd(date_to, shift=-1) or _ymd(date_from)
    low, high = _month_day(date_from), _month_day(date_to, shift=-1)
    span = f"{low} to {high}" if low and high and low != high else (low or high)
    if not span:
        return ""
    return f"{span}, {parts[0]}" if parts else span


def live_words(lookback_hours, run_hours):
    back, run = _number(lookback_hours), _number(run_hours)
    said = ["From now"]
    if back and back > 0:
        said.append(f"also looking back {_plural(back, 'hour')}")
    if run and run > 0:
        said.append(f"and it keeps running for {_plural(run, 'hour')}")
    return ", ".join(said)


# ------------------------------------------------------------------ the two lines
def title(tool, tool_input):
    """What is being done, in plain words, built only from the arguments of the real call."""
    name = _name(tool)
    fields = tool_input if isinstance(tool_input, dict) else {}
    words, language = _words(fields.get("keywords")), _language(fields.get("language"))
    if name == "preview_keywords":
        head = "Searching X/Twitter" + (f" for {words}" if words else "")
        return plain(", ".join(part for part in (head, _when(fields), f"in {language}" if language else "") if part))
    if name in ("bluesky_recent", "bluesky_listen"):
        if name == "bluesky_recent":
            head = f"Searching the last {_plural(_span(fields.get('minutes'), LIVE_DEFAULTS['minutes']), 'minute')} of Bluesky"
        else:
            head = f"Listening to Bluesky live for {_plural(_span(fields.get('seconds'), LIVE_DEFAULTS['seconds']), 'second')}"
        head += f" for {words}" if words else ""
        return plain(", ".join(part for part in (head, f"in {language}" if language else "") if part))
    return TITLES.get(name, "Working on it")


def outcome(tool, result, is_error=False):
    """What came back, from the real parsed result. Never the model's words, never a guess."""
    name = _name(tool)
    problem = _problem(result, is_error)
    if problem or is_error:
        return _sentence(f"That did not work: {problem}") if problem else "That did not work."
    fields = result if isinstance(result, dict) else {}
    if name == "preview_keywords":
        total = _number(fields.get("total"))
        if total is None:
            return DONE
        if total <= 0:
            return "No posts matched those words."
        peak = _peak(fields.get("per_day"))
        return _sentence(f"Found {_posts(total)}." + (f" Most were on {peak}." if peak else ""))
    if name in ("bluesky_recent", "bluesky_listen"):
        matched = _number(fields.get("matched"))
        if matched is None:
            return DONE
        scanned, covered = _count(fields.get("scanned")), _number(fields.get("covered_fraction"))
        if matched > 0:
            line = f"Found {_posts(matched)}" + (f" out of {scanned} checked." if scanned else ".")
        else:
            line = "No posts matched those words." + (f" We checked {scanned} posts." if scanned else "")
        if covered is not None and covered < 0.99:
            line += f" Only about {max(0.0, min(1.0, covered)) * 100:.0f}% of that time could be checked, so the real number is higher."
        return _sentence(line)
    if name == "describe_sources":
        sources = fields.get("sources") if isinstance(fields.get("sources"), list) else []
        names = [n for n in (SOURCE_NAMES.get(_text(s.get("id"), 40)) or _text(s.get("name"), 48)
                             for s in sources if isinstance(s, dict)) if n]
        return _sentence(f"We have {_plural(len(names), 'source')}: {_and(names)}") if names else DONE
    if name == "save_draft":
        if "spec_hash" not in fields and "spec" not in fields:
            return DONE
        spec = fields.get("spec") if isinstance(fields.get("spec"), dict) else {}
        return _sentence(f'Saved as "{_text(spec.get("name"), 60) or "your project"}".')
    if name == "request_confirmation":
        if not _text(fields.get("confirmation_id"), 40):
            return DONE
        left = _left(fields.get("expires_ms"))
        return _sentence("Waiting for you to press Confirm." + (f" The button works for {left}." if left else ""))
    if name == "submit_project":
        project = _text(fields.get("project_id"), 40)
        return _sentence(f"Your project was sent. Its reference is {project}.") if project else "Your project was sent."
    return DONE
