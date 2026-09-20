# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb>=1.4,<2"]
# ///
"""Run: uv run ai-keywords/find_ai_keywords.py [--shards N] [--rescan] [--skip-discovery]

Measures every candidate AI keyword against the X firehose month (2026-08-17..09-17):
distinct ORIGINAL tweets (RT-prefixed bodies excluded, latest `version` per id),
authors, likes, views, English share, peak day by share per 100k originals of that
day, lift over the keyword's own median daily share, and `ai_context_share` -- the
fraction of the keyword's tweets that also carry an unambiguous CORE AI term.
Matching runs on the body with links stripped, so a keyword inside a t.co code never
counts. Step 4 mines core-AI tweets for entities the seed list missed, ranked by lift
against a 0.5% background sample of all originals.

One pass over the archive writes ai-keywords/scan.parquet (matched tweets + the
background sample); everything after that runs off it. Hand judgements
(estimated_precision, verdict, reason, notes) live in ai-keywords/judgements.json and
are merged back in, so rerunning never loses them.

Known ~0.01-0.05% undercount, measured against a from-scratch archive query: the
prefilter runs on the raw body while keywords match on the links-stripped body, and
0.012% of tweets have bodies that differ between version snapshots.
"""
import argparse
import csv
import json
import shutil
import statistics
import time
from pathlib import Path

import duckdb

OUT = Path(__file__).resolve().parent
ROOT = OUT.parent
SCAN = OUT / "scan.parquet"
WINDOW = ("2026-08-17 00:00:00+00", "2026-09-18 00:00:00+00")
PARTIAL_DAY = "2026-09-17"  # collection stops 14:32 UTC; excluded from peak detection
BG_MOD = 200  # deterministic 0.5% background sample of originals, for discovery lift

# keyword, kind, pattern (RE2), case_sensitive, origin
KEYWORDS = [
    # ---- companies / labs -------------------------------------------------
    ("OpenAI", "company", r"\bopenai\b", False, "seeded"),
    ("Anthropic", "company", r"\banthropic\b", False, "seeded"),
    ("DeepMind", "company", r"\bdeepmind\b", False, "seeded"),
    ("xAI", "company", r"\bxai\b", False, "seeded"),
    ("Meta AI", "company", r"\bmeta ai\b", False, "seeded"),
    ("Google AI", "company", r"\bgoogle ai\b", False, "seeded"),
    ("Mistral", "company", r"\bmistral\b", False, "seeded"),
    ("DeepSeek", "company", r"\bdeepseek\b", False, "seeded"),
    ("Qwen", "company", r"\bqwen", False, "seeded"),
    ("Moonshot", "company", r"\bmoonshot\b", False, "seeded"),
    ("Kimi", "product", r"\bkimi\b", False, "seeded"),
    ("Perplexity", "company", r"\bperplexity\b", False, "seeded"),
    ("Nvidia", "company", r"\bnvidia\b", False, "seeded"),
    ("Hugging Face", "company", r"\bhugging ?face\b", False, "seeded"),
    ("Stability AI", "company", r"\bstability ai\b", False, "seeded"),
    ("Midjourney", "company", r"\bmidjourney\b", False, "seeded"),
    ("Runway", "company", r"\brunway\b", False, "seeded"),
    ("RunwayML", "company", r"\brunwayml\b", False, "seeded"),
    ("ElevenLabs", "company", r"\beleven ?labs\b", False, "seeded"),
    ("Cursor", "product", r"\bcursor\b", False, "seeded"),
    ("Character.AI", "company", r"\bcharacter[. ]ai\b", False, "seeded"),
    ("Character", "company", r"\bcharacters?\b", False, "seeded"),
    ("Cohere", "company", r"\bcohere\b", False, "seeded"),
    ("Scale AI", "company", r"\bscale ai\b", False, "seeded"),
    ("Scale", "company", r"\bscale\b", False, "seeded"),
    ("SSI", "company", r"\bSSI\b", True, "seeded"),
    ("safe superintelligence", "company", r"\bsafe superintelligence\b", False, "seeded"),
    # leading zero required: "Figure 2"/"Figure 3" is a paper caption, "Figure 02" is the robot
    ("Figure AI", "company", r"\bfigure (?:ai|0[23])\b", False, "seeded"),
    ("Figure", "company", r"\bfigure\b", False, "seeded"),
    ("Groq", "company", r"\bgroq\b", False, "seeded"),
    ("Cerebras", "company", r"\bcerebras\b", False, "seeded"),
    ("Palantir", "company", r"\bpalantir\b", False, "seeded"),
    ("Baidu", "company", r"\bbaidu\b", False, "seeded"),
    ("Doubao", "product", r"\bdoubao\b", False, "seeded"),
    ("Hunyuan", "product", r"\bhunyuan\b", False, "seeded"),
    ("MiniMax", "company", r"\bminimax\b", False, "seeded"),
    ("Replit", "company", r"\breplit\b", False, "seeded"),
    ("Windsurf", "product", r"\bwindsurf\b", False, "seeded"),
    ("Lovable", "company", r"\blovable\b", False, "seeded"),
    ("Devin", "product", r"\bdevin\b", False, "seeded"),
    ("Manus", "product", r"\bmanus\b", False, "seeded"),
    ("Suno", "company", r"\bsuno\b", False, "seeded"),
    # RE2's \b is ASCII-only, so \budio\b also matches "áudio"/"Cláudio": use a Unicode boundary.
    ("Udio", "company", r"(?:^|[^\p{L}\p{N}])udio(?:[^\p{L}\p{N}]|$)", False, "seeded"),
    ("Synthesia", "company", r"\bsynthesia\b", False, "seeded"),
    ("Luma", "company", r"\bluma\b", False, "seeded"),
    ("Pika", "company", r"\bpika\b", False, "seeded"),
    ("Sakana AI", "company", r"\bsakana\b", False, "seeded"),
    ("Thinking Machines", "company", r"\bthinking machines\b", False, "seeded"),
    ("Waymo", "company", r"\bwaymo\b", False, "seeded"),
    ("Boston Dynamics", "company", r"\bboston dynamics\b", False, "seeded"),
    ("Apple Intelligence", "product", r"\bapple intelligence\b", False, "seeded"),
    # ---- products / models ------------------------------------------------
    ("ChatGPT", "product", r"\bchatgpt\b", False, "seeded"),
    ("GPT", "product", r"\bgpt\b", False, "seeded"),
    ("GPT-4", "product", r"\bgpt[- ]?4(?:o|\.[0-9])?\b", False, "seeded"),
    ("GPT-5", "product", r"\bgpt[- ]?5(?:\.[0-9])?\b", False, "seeded"),
    ("GPT-6", "product", r"\bgpt[- ]?6\b", False, "seeded"),
    ("Sora", "product", r"\bsora\b", False, "seeded"),
    ("Claude", "product", r"\bclaude\b", False, "seeded"),
    ("Gemini", "product", r"\bgemini\b", False, "seeded"),
    ("Grok", "product", r"\bgrok\b", False, "seeded"),
    ("Llama", "product", r"\bllama\b", False, "seeded"),
    ("Copilot", "product", r"\bcopilot\b", False, "seeded"),
    ("Veo", "product", r"\bveo\b", False, "seeded"),
    ("NotebookLM", "product", r"\bnotebooklm\b", False, "seeded"),
    ("o3", "product", r"\bo3\b", True, "seeded"),
    ("o4", "product", r"\bo4\b", True, "seeded"),
    ("R1", "product", r"\bR1\b", True, "seeded"),
    ("Codex", "product", r"\bcodex\b", False, "seeded"),
    ("Gemma", "product", r"\bgemma\b", False, "seeded"),
    ("Sonnet", "product", r"\bsonnet\b", False, "seeded"),
    ("Opus", "product", r"\bopus\b", False, "seeded"),
    ("Haiku", "product", r"\bhaiku\b", False, "seeded"),
    # separator required: a bare "dall e" would match Italian "dalle" tens of thousands of times
    ("DALL-E", "product", r"\bdall[-·–]e\b", False, "seeded"),
    ("Stable Diffusion", "product", r"\bstable diffusion\b", False, "seeded"),
    ("Imagen", "product", r"\bimagen\b", False, "seeded"),
    ("Nano Banana", "product", r"\bnano[- ]?banana\b", False, "seeded"),
    ("Whisper", "product", r"\bwhisper\b", False, "seeded"),
    ("Firefly", "product", r"\bfirefly\b", False, "seeded"),
    ("Siri", "product", r"\bsiri\b", False, "seeded"),
    ("Alexa", "product", r"\balexa\b", False, "seeded"),
    ("Optimus", "product", r"\boptimus\b", False, "seeded"),
    ("MCP", "concept", r"\bMCP\b", True, "seeded"),
    ("deep research", "product", r"\bdeep research\b", False, "seeded"),
    # ---- concepts ---------------------------------------------------------
    ("AI", "concept", r"\bAI\b", True, "seeded"),
    ("A.I.", "concept", r"\bA\.I\.", False, "seeded"),
    ("AGI", "concept", r"\bAGI\b", True, "seeded"),
    ("LLM", "concept", r"\bLLMs?\b", True, "seeded"),
    ("GenAI", "concept", r"\bgen[- ]?ai\b", False, "seeded"),
    ("artificial intelligence", "concept", r"\bartificial intelligence\b", False, "seeded"),
    ("machine learning", "concept", r"\bmachine learning\b", False, "seeded"),
    ("AI safety", "concept", r"\bAI safety\b", False, "seeded"),
    ("AI bubble", "concept", r"\bAI[- ]bubble\b", False, "seeded"),
    ("AI slop", "concept", r"\bAI[- ]slop\b", False, "seeded"),
    ("AI art", "concept", r"\bAI[- ]art\b", False, "seeded"),
    ("AI-generated", "concept", r"\bAI[- ]generated\b", False, "seeded"),
    ("AI agent", "concept", r"\bAI agents?\b", False, "seeded"),
    ("AI model", "concept", r"\bAI models?\b", False, "seeded"),
    ("AI bro", "concept", r"\bAI bros?\b", False, "seeded"),
    ("deepfake", "concept", r"\bdeep ?fakes?\b", False, "seeded"),
    ("chatbot", "concept", r"\bchat ?bots?\b", False, "seeded"),
    ("vibe coding", "concept", r"\bvibe[- ]?cod", False, "seeded"),
    ("agentic", "concept", r"\bagentic\b", False, "seeded"),
    ("superintelligence", "concept", r"\bsuper[- ]?intelligence\b", False, "seeded"),
    ("neural network", "concept", r"\bneural net(?:work)?s?\b", False, "seeded"),
    ("language model", "concept", r"\blanguage models?\b", False, "seeded"),
    ("prompt", "concept", r"\bprompts?\b", False, "seeded"),
    ("RAG", "concept", r"\bRAG\b", True, "seeded"),
    ("IA", "concept", r"\bIA\b", True, "seeded"),
    ("inteligencia artificial", "concept", r"intelig[eê]ncia artificial", False, "seeded"),
    ("intelligence artificielle", "concept", r"intelligence artificielle", False, "seeded"),
    ("yapay zeka", "concept", r"yapay zek", False, "seeded"),
    ("ذكاء اصطناعي", "concept",
     r"ذكاء ال?ا?صطناعي", False, "seeded"),
    ("人工知能", "concept", r"人工知能|人工智能|인공지능", False, "seeded"),
    ("ИИ", "concept", r"(?:^|[^\p{L}\p{N}])ИИ(?:[^\p{L}\p{N}]|$)", True, "seeded"),
    # ---- people -----------------------------------------------------------
    ("Altman", "person", r"\baltman\b", False, "seeded"),
    ("Amodei", "person", r"\bamodei\b", False, "seeded"),
    ("Hassabis", "person", r"\bhassabis\b", False, "seeded"),
    ("Jensen Huang", "person", r"\bjensen huang\b", False, "seeded"),
    ("Sutskever", "person", r"\bsutskever\b", False, "seeded"),
    ("LeCun", "person", r"\blecun\b", False, "seeded"),
    ("Hinton", "person", r"\bhinton\b", False, "seeded"),
    ("Murati", "person", r"\bmurati\b", False, "seeded"),
    ("Karpathy", "person", r"\bkarpathy\b", False, "seeded"),
    ("Suleyman", "person", r"\bsuleyman\b", False, "seeded"),
    ("Elon Musk", "person", r"\belon musk\b", False, "seeded"),
    ("Fei-Fei Li", "person", r"\bfei[- ]?fei li\b", False, "seeded"),
    # ---- discovered in step 4 (tokens lifted from CORE-AI tweets) ---------
    ("AIイラスト", "concept", r"AIイラスト", False, "discovered"),
    ("生成AI", "concept", r"生成AI", False, "discovered"),
    ("Claude Code", "product", r"\bclaude ?code\b", False, "discovered"),
    ("Sol", "product", r"\bsol\b", False, "discovered"),
    ("Astra", "product", r"\bastra\b", False, "discovered"),
    ("Fable", "product", r"\bfable\b", False, "discovered"),
    ("Seedance", "product", r"\bseedance\b", False, "discovered"),
    ("Grok Imagine", "product", r"\bgrok[- ]?imagine\b", False, "discovered"),
    ("physical AI", "concept", r"\bphysical ai\b", False, "discovered"),
    ("generative AI", "concept", r"\bgenerative ?ai\b", False, "discovered"),
    ("harness", "concept", r"\bharness\b", False, "discovered"),
    ("OpenRouter", "company", r"\bopenrouter\b", False, "discovered"),
    ("PixAI", "company", r"\bpixai\b", False, "discovered"),
    ("GLM", "product", r"\bGLM\b", True, "discovered"),
    ("OAI", "company", r"\bOAI\b", True, "discovered"),
    ("Wan", "product", r"\bwan ?[0-9]", False, "discovered"),
    ("Apodex", "company", r"\bapodex\b", False, "discovered"),
    ("Claudeforce", "product", r"\bclaudeforce\b", False, "discovered"),
    ("Nscale", "company", r"\bnscale\b", False, "discovered"),
    ("Nebius", "company", r"\bnebius\b", False, "discovered"),
    ("HUMAIN", "company", r"\bHUMAIN\b", True, "discovered"),
    ("Vera Rubin", "product", r"\bvera rubin\b", False, "discovered"),
    ("AI Overviews", "product", r"\bAI overviews?\b", False, "discovered"),
    ("AI Mode", "product", r"\bAI mode\b", False, "discovered"),
    ("keep4o", "concept", r"\b(?:keep|bringback|free|opensource)4o\b", False, "discovered"),
    ("Ollama", "company", r"\bollama\b", False, "discovered"),
    ("LangChain", "company", r"\blangchain\b", False, "discovered"),
    ("PyTorch", "concept", r"\bpytorch\b", False, "discovered"),
    ("TensorFlow", "concept", r"\btensorflow\b", False, "discovered"),
    ("deep learning", "concept", r"\bdeep learning\b", False, "discovered"),
    ("LLMO", "concept", r"\bllmo\b", False, "discovered"),
    ("AIGC", "concept", r"\baigc\b", False, "discovered"),
    ("Kling", "company", r"\bkling\b", False, "discovered"),
    ("Pollo AI", "company", r"\bpollo ?ai\b", False, "discovered"),
    ("NijiJourney", "company", r"\bniji ?journey\b", False, "discovered"),
    ("SeaArt", "company", r"\bseaart\b", False, "discovered"),
    ("Dwarkesh", "person", r"\bdwarkesh\b", False, "discovered"),
    ("Aschenbrenner", "person", r"\baschenbrenner\b", False, "discovered"),
]

# Unambiguous AI terms used to judge whether an ambiguous keyword sits in AI context.
# Deviation from the brief's example core set: bare "prompt" is left out (an ordinary
# English verb/adjective -- see README), and the non-English renderings of "artificial
# intelligence" are added, because the corpus is multilingual.
CORE = ["AI", "A.I.", "artificial intelligence", "LLM", "chatbot", "OpenAI", "ChatGPT",
        "Anthropic", "DeepSeek", "language model", "GenAI", "AGI", "machine learning",
        "neural network", "AI model", "inteligencia artificial", "intelligence artificielle",
        "yapay zeka", "ذكاء اصطناعي",
        "人工知能", "ИИ"]


def q(text):
    return "'" + text.replace("'", "''") + "'"


def matches(column, pattern, case_sensitive):
    return f"regexp_matches({column}, {q(pattern)}{'' if case_sensitive else ', ' + q('i')})"


def lookup_tweets(rows, name):
    return next(r["tweets"] for r in rows if r["keyword"] == name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shards", type=int, default=0, help="scan only the first N parquet shards (smoke test)")
    parser.add_argument("--rescan", action="store_true", help="rebuild scan.parquet even if it exists")
    parser.add_argument("--skip-discovery", action="store_true")
    args = parser.parse_args()
    started = time.monotonic()
    tmp = OUT / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute("SET threads=4")
    con.execute("SET memory_limit='4GB'")
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"SET temp_directory={q(str(tmp))}")
    con.execute("SET max_temp_directory_size='40GB'")

    def step(label, sql):
        start = time.monotonic()
        con.execute(sql)
        print(f"  {label} ({time.monotonic()-start:.1f}s)", flush=True)

    # ---- step 1: one archive pass -----------------------------------------
    shards = sorted((ROOT / "twitter-firehose").glob("tweets-*.parquet"))
    if args.shards:
        shards = shards[: args.shards]
    source = "[" + ",".join(q(str(p)) for p in shards) + "]"
    ci = "|".join(f"(?:{p})" for _, _, p, cs, _ in KEYWORDS if not cs)
    cs = "|".join(f"(?:{p})" for _, _, p, case, _ in KEYWORDS if case)
    prefilter = f"({matches('body', ci, False)} OR {matches('body', cs, True)})"
    if args.rescan and SCAN.exists():
        SCAN.unlink()
    if not SCAN.exists():
        print(f"scanning {len(shards)} shards...", flush=True)
        step("scan.parquet", f"""COPY (
            SELECT *, {prefilter} AS is_match FROM (
              SELECT id, author_id, created_at, lang, like_count, retweet_count, views_count, body,
                     hash(id) % {BG_MOD} = 0 AS is_bg
              FROM read_parquet({source})
              WHERE NOT starts_with(body, 'RT @')
                AND created_at >= TIMESTAMPTZ {q(WINDOW[0])} AND created_at < TIMESTAMPTZ {q(WINDOW[1])}
                AND ({prefilter} OR hash(id) % {BG_MOD} = 0)
              QUALIFY row_number() OVER (PARTITION BY id ORDER BY version DESC) = 1
            )) TO {q(str(SCAN))} (FORMAT PARQUET, COMPRESSION ZSTD)""")
    else:
        print(f"reusing {SCAN.name} (pass --rescan to rebuild)", flush=True)

    # ---- step 2: exact matching on links-stripped text --------------------
    step("tweets table", f"""CREATE TABLE tweets AS
        SELECT id, author_id, cast(created_at AS DATE) AS day, lang, like_count, views_count, is_bg, is_match,
               regexp_replace(body, 'https?://\\S+', ' ', 'g') AS txt, body
        FROM read_parquet({q(str(SCAN))})""")
    scanned = con.execute("SELECT count(*) FILTER (WHERE is_match), count(*) FILTER (WHERE is_bg) FROM tweets").fetchone()
    print(f"  {scanned[0]:,} prefilter matches, {scanned[1]:,} background sample tweets", flush=True)

    con.execute("CREATE TABLE hits (id VARCHAR, keyword VARCHAR)")
    start = time.monotonic()
    for name, _, pattern, case, _ in KEYWORDS:
        con.execute(f"INSERT INTO hits SELECT id, {q(name)} FROM tweets "
                    f"WHERE is_match AND {matches('txt', pattern, case)}")
    print(f"  hits for {len(KEYWORDS)} keywords ({time.monotonic()-start:.1f}s)", flush=True)
    step("core counts", f"""CREATE TABLE core AS
        SELECT id, count(*) AS n_core FROM hits WHERE keyword IN ({','.join(q(c) for c in CORE)}) GROUP BY id""")

    denominators = {str(d): int(o) for d, o in con.execute(f"""
        SELECT cast(day AS VARCHAR), max(originals) FROM read_csv_auto({q(str(ROOT / 'topic-analysis' / 'daily-denominators.csv'))})
        WHERE lang = '<ALL>' AND day IS NOT NULL GROUP BY 1""").fetchall()}
    days = sorted(d for d in denominators if d != PARTIAL_DAY)

    core_names = set(CORE)
    stats = {r[0]: r for r in con.execute(f"""
        SELECT h.keyword, count(DISTINCT h.id) AS tweets, count(DISTINCT t.author_id) AS authors,
               sum(t.like_count) AS likes, sum(t.views_count) AS views,
               count(DISTINCT t.id) FILTER (WHERE t.lang = 'en') AS english,
               count(DISTINCT t.id) FILTER (WHERE coalesce(c.n_core, 0)
                    - CASE WHEN h.keyword IN ({','.join(q(c) for c in CORE)}) THEN 1 ELSE 0 END > 0) AS in_context
        FROM hits h JOIN tweets t USING (id) LEFT JOIN core c USING (id) GROUP BY h.keyword""").fetchall()}
    daily = {}
    for keyword, day, n in con.execute("""SELECT h.keyword, cast(t.day AS VARCHAR), count(DISTINCT h.id)
        FROM hits h JOIN tweets t USING (id) GROUP BY 1, 2""").fetchall():
        daily.setdefault(keyword, {})[day] = n
    tops = {}
    for keyword, tid, day, likes, excerpt in con.execute("""
        SELECT keyword, id, cast(day AS VARCHAR), like_count, left(body, 160) FROM (
          SELECT h.keyword, t.id, t.day, t.like_count, t.body,
                 row_number() OVER (PARTITION BY h.keyword ORDER BY t.like_count DESC NULLS LAST, t.id) AS rank
          FROM hits h JOIN tweets t USING (id)) WHERE rank <= 3 ORDER BY keyword, rank""").fetchall():
        tops.setdefault(keyword, []).append({"id": tid, "day": day, "likes": int(likes or 0), "excerpt": excerpt})

    judgements = {}
    path = OUT / "judgements.json"
    if path.exists():
        judgements = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for name, kind, pattern, case, origin in KEYWORDS:
        s = stats.get(name)
        tweets = int(s[1]) if s else 0
        series = daily.get(name, {})
        shares = [(d, series.get(d, 0) / denominators[d] * 100000) for d in days]
        peak_day, peak = max(shares, key=lambda x: x[1]) if shares else (None, 0.0)
        # lift uses +0.5 numerator smoothing so sparse keywords still get a finite ratio
        smooth = {d: (series.get(d, 0) + 0.5) / (denominators[d] + 1) for d in days}
        median = statistics.median(smooth.values())
        judged = judgements.get(name, {})
        rows.append({
            "keyword": name, "kind": kind, "origin": origin, "pattern": pattern,
            "case_sensitive": case, "tweets": tweets, "authors": int(s[2]) if s else 0,
            "likes": int(s[3] or 0) if s else 0, "views": int(s[4] or 0) if s else 0,
            "english_share": round(s[5] / tweets, 4) if tweets else None,
            "ai_context_share": round(s[6] / tweets, 4) if tweets else None,
            "estimated_precision": judged.get("estimated_precision"),
            "peak_day": peak_day if tweets else None,
            "peak_share_per_100k": round(peak, 4) if tweets else None,
            "peak_lift": round(smooth[peak_day] / median, 2) if tweets and median else None,
            "verdict": judged.get("verdict", "unjudged"), "reason": judged.get("reason", ""),
            "false_positives": judged.get("false_positives", ""),
            "top_posts": tops.get(name, []),
            "daily_share_per_100k": {d: round(v, 4) for d, v in shares},
        })
    rows.sort(key=lambda r: -r["tweets"])
    (OUT / "ai-keywords.json").write_text(json.dumps(
        {"method": {
            "window": f"created_at in [{WINDOW[0]}, {WINDOW[1]})", "posts": "originals only (body not 'RT @...')",
            "dedup": "latest version per id", "matching": "RE2 whole-word on body with https?://\\S+ removed",
            "denominator": "topic-analysis/daily-denominators.csv originals per day, lang='<ALL>'",
            "peak": f"max daily share per 100k originals over complete days ({PARTIAL_DAY} excluded, partial)",
            "peak_lift": "peak daily share / median daily share of the same keyword",
            "ai_context_share": "share of the keyword's tweets also matching a CORE term other than itself",
            "core_terms": CORE, "duckdb": duckdb.__version__},
         "keywords": rows}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    fields = ["keyword", "kind", "origin", "pattern", "case_sensitive", "tweets", "authors", "likes", "views",
              "english_share", "ai_context_share", "estimated_precision", "peak_day", "peak_share_per_100k",
              "peak_lift", "verdict", "reason", "top_posts"]
    with (OUT / "ai-keywords.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "top_posts": json.dumps(row["top_posts"], ensure_ascii=False)})

    buckets = {"standalone": "use", "need_ai_context": "use_with_context"}
    lookup = {r["keyword"]: r for r in rows}
    (OUT / "recommended-filter.json").write_text(json.dumps({
        "note": "Whole-word RE2 patterns, matched on the tweet body with links removed. "
                "need_ai_context terms only count when a context_terms pattern also matches the same post.",
        **{bucket: [{"keyword": r["keyword"], "pattern": r["pattern"], "case_sensitive": r["case_sensitive"]}
                    for r in rows if r["verdict"] == verdict]
           for bucket, verdict in buckets.items()},
        "context_terms": [{"keyword": c, "pattern": lookup[c]["pattern"], "case_sensitive": lookup[c]["case_sensitive"]}
                          for c in CORE if c in lookup],
    }, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    # ---- precision samples: tweets of a keyword carrying NO core term ------
    # 40 per keyword as designed; 15 for keywords under 500 tweets, where the verdict
    # cannot move the filter much and the reading budget is better spent elsewhere.
    low = [r["keyword"] for r in rows if r["tweets"] >= 50 and (r["ai_context_share"] or 0) < 0.60]
    samples = {}
    for name in low:
        self_core = 1 if name in core_names else 0
        size = 40 if lookup_tweets(rows, name) >= 500 else 15
        samples[name] = [{"id": i, "lang": l, "likes": int(k or 0), "text": " ".join(b.split())[:200]}
                         for i, l, k, b in con.execute(f"""
            SELECT t.id, t.lang, t.like_count, t.txt FROM hits h JOIN tweets t USING (id)
            LEFT JOIN core c USING (id) WHERE h.keyword = {q(name)}
              AND coalesce(c.n_core, 0) - {self_core} <= 0 ORDER BY hash(t.id) LIMIT {size}""").fetchall()]
    (OUT / "precision-samples.json").write_text(
        json.dumps(samples, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  precision samples for {len(low)} keywords below 60% ai_context_share", flush=True)

    # ---- step 4: discovery ------------------------------------------------
    if not args.skip_discovery:
        step("discovery", f"""CREATE TABLE candidates AS
        WITH core_tweets AS (SELECT t.id, t.txt FROM tweets t JOIN core c USING (id) WHERE c.n_core > 0),
        toks AS (
          SELECT id, lower(tok) AS tok, 'caps' AS form FROM core_tweets,
            unnest(regexp_extract_all(txt, '\\b[A-Z][A-Za-z0-9]{{2,}}\\b')) AS u(tok)
          UNION ALL SELECT id, lower(tok), 'caps2' FROM core_tweets,
            unnest(regexp_extract_all(txt, '\\b[A-Z][A-Za-z]+ [A-Z][A-Za-z]+\\b')) AS u(tok)
          UNION ALL SELECT id, lower(tok), 'hashtag' FROM core_tweets,
            unnest(regexp_extract_all(txt, '#[A-Za-z][A-Za-z0-9_]{{2,}}')) AS u(tok)
          UNION ALL SELECT id, lower(tok), 'mention' FROM core_tweets,
            unnest(regexp_extract_all(txt, '@[A-Za-z][A-Za-z0-9_]{{2,}}')) AS u(tok)
          UNION ALL SELECT id, lower(tok), 'model' FROM core_tweets,
            unnest(regexp_extract_all(txt, '\\b[A-Za-z]{{2,}}[- ]?[0-9]+(?:\\.[0-9]+)?\\b')) AS u(tok)),
        bg AS (
          SELECT lower(tok) AS tok, count(DISTINCT id) AS n FROM (
            SELECT id, tok FROM (SELECT id, txt FROM tweets WHERE is_bg),
              unnest(regexp_extract_all(txt, '\\b[A-Z][A-Za-z0-9]{{2,}}\\b')) AS u(tok)
            UNION ALL SELECT id, tok FROM (SELECT id, txt FROM tweets WHERE is_bg),
              unnest(regexp_extract_all(txt, '\\b[A-Z][A-Za-z]+ [A-Z][A-Za-z]+\\b')) AS u(tok)
            UNION ALL SELECT id, tok FROM (SELECT id, txt FROM tweets WHERE is_bg),
              unnest(regexp_extract_all(txt, '#[A-Za-z][A-Za-z0-9_]{{2,}}')) AS u(tok)
            UNION ALL SELECT id, tok FROM (SELECT id, txt FROM tweets WHERE is_bg),
              unnest(regexp_extract_all(txt, '@[A-Za-z][A-Za-z0-9_]{{2,}}')) AS u(tok)
            UNION ALL SELECT id, tok FROM (SELECT id, txt FROM tweets WHERE is_bg),
              unnest(regexp_extract_all(txt, '\\b[A-Za-z]{{2,}}[- ]?[0-9]+(?:\\.[0-9]+)?\\b')) AS u(tok))
          GROUP BY 1)
        SELECT t.tok, any_value(t.form) AS form, count(DISTINCT t.id) AS core_tweets,
               coalesce(max(bg.n), 0) AS bg_tweets
        FROM toks t LEFT JOIN bg USING (tok) GROUP BY t.tok HAVING count(DISTINCT t.id) >= 40""")
        n_core, n_bg = con.execute(
            "SELECT (SELECT count(*) FROM core WHERE n_core>0), (SELECT count(*) FROM tweets WHERE is_bg)").fetchone()
        found = [{"token": t, "form": f, "core_tweets": int(c), "bg_tweets": int(b),
                  "lift": round((c / n_core) / ((b + 0.5) / n_bg), 1)}
                 for t, f, c, b in con.execute("SELECT * FROM candidates").fetchall()]
        found.sort(key=lambda r: -r["lift"])
        (OUT / "discovery-candidates.json").write_text(json.dumps(
            {"core_tweets": n_core, "background_tweets": n_bg,
             "method": "tokens in tweets carrying a CORE AI term, ranked by rate lift over a "
                       f"1-in-{BG_MOD} hash(id) sample of all originals; >=40 core tweets required",
             "candidates": found[:600]}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  {len(found)} discovery candidates ({n_core:,} core tweets vs {n_bg:,} background)", flush=True)

    con.close()
    shutil.rmtree(tmp, ignore_errors=True)  # leave no untracked spill directory behind
    print(f"done in {time.monotonic()-started:.1f}s -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
