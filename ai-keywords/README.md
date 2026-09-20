# AI keywords for the X firehose month (2026-08-17 → 2026-09-17)

`uv run ai-keywords/find_ai_keywords.py`. One pass over the 396 shards builds `scan.parquet` (2.33M matched original tweets + a 1-in-200 background sample); everything else runs off it. 164 candidates: 126 seeded, 38 discovered by mining tokens out of core-AI tweets and ranking them by lift over that background.
**Method.** Originals only (body not `RT @…`), `count(DISTINCT id)`, latest `version` per id, RE2 whole-word matching on the body **with links stripped**, so a keyword inside a t.co code never counts. Short all-caps tokens (AI, AGI, LLM, R1, SSI, MCP, GLM, OAI, HUMAIN, ИИ) are case-sensitive. Daily rates are per 100k **originals that day** (`topic-analysis/daily-denominators.csv`), never raw counts. `ctx` = share of a keyword's tweets that also carry an unambiguous CORE AI term other than itself; `prec` = estimated precision. Every keyword under 60% ctx was sampled — 40 tweets containing **no** core term, ordered by `hash(id)` — and read by hand; those reads produced `prec` and the verdicts in `judgements.json`, which the script merges back in on every run.

## Top 25 by volume (dropped keywords excluded)

| keyword | kind | origin | tweets | authors | likes | ctx | prec | verdict |
|---|---|---|---|---|---|---|---|---|
| AI | concept | seed | 907,293 | 355,499 | 82.2M | 0.06 | 0.95 | use |
| Grok | product | seed | 188,012 | 96,223 | 15.6M | 0.08 | 0.98 | use |
| Claude | product | seed | 83,799 | 43,107 | 6.4M | 0.32 | 0.98 | use |
| IA | concept | seed | 70,009 | 44,209 | 13.1M | 0.08 | 0.89 | use |
| ChatGPT | product | seed | 69,395 | 45,387 | 10.1M | 0.26 | 1.00 | use |
| #AIイラスト | concept | **disc** | 53,927 | 5,833 | 4.1M | 0.96 | 1.00 | use |
| prompt | concept | seed | 53,133 | 24,674 | 4.3M | 0.24 | 0.83 | context |
| Gemini | product | seed | 46,741 | 27,942 | 4.8M | 0.26 | 0.78 | context |
| GPT | product | seed | 39,061 | 24,996 | 6.5M | 0.26 | 0.98 | use |
| OpenAI | company | seed | 37,738 | 19,527 | 6.5M | 0.52 | 0.98 | use |
| Nvidia | company | seed | 37,040 | 17,858 | 3.2M | 0.37 | 0.92 | use |
| Codex | product | seed | 35,511 | 16,983 | 2.3M | 0.27 | 1.00 | use |
| AI agent | concept | seed | 30,375 | 16,829 | 1.5M | 0.95 | 0.99 | use |
| Anthropic | company | seed | 26,910 | 14,749 | 5.5M | 0.50 | 1.00 | use |
| LLM | concept | seed | 25,274 | 15,766 | 1.5M | 0.32 | 1.00 | use |
| 生成AI | concept | **disc** | 24,564 | 13,659 | 2.7M | 0.99 | 1.00 | use |
| Kimi | product | seed | 21,254 | 13,435 | 3.9M | 0.10 | 0.21 | context |
| Claude Code | product | **disc** | 20,562 | 10,876 | 1.3M | 0.30 | 1.00 | use |
| agentic | concept | seed | 16,214 | 10,035 | 1.2M | 0.46 | 1.00 | use |
| Fable | product | **disc** | 15,864 | 9,860 | 1.9M | 0.20 | 0.78 | context |
| Astra | product | **disc** | 14,616 | 8,623 | 5.1M | 0.31 | 0.90 | use |
| Cursor | product | seed | 13,963 | 8,456 | 1.0M | 0.34 | 0.92 | use |
| AI-generated | concept | seed | 13,840 | 11,895 | 3.4M | 0.80 | 0.98 | use |
| Optimus | product | seed | 13,448 | 10,716 | 5.2M | 0.02 | 0.19 | context |
| Qwen | company | seed | 12,649 | 5,502 | 0.8M | 0.26 | 1.00 | use |

## The month's ten biggest AI moments (peak day; lift over that keyword's median day)

| keyword | day | lift | top post that day |
|---|---|---|---|
| GPT-6 | 09-04 | **177×** | "GPT-6 Astra is now available to all Pro, Enterprise and Business Premium users in ChatGPT Work and Codex" (36.5k likes) |
| Amodei | 09-12 | 67× | "If Sam Altman, Dario Amodei and Elon Musk all agree that we have to slow down…" (34.8k) |
| Astra | 09-05 | 44× | Astra one-shotting a 2,234-piece 3D anatomy site — "we are in a renaissance" (53.4k) |
| superintelligence | 09-09 | 24× | "I resigned from Anthropic today… Neither company is acting responsibly" (801k — the month's biggest AI post) |
| Apodex | 08-26 | 16× | Apodex 1.1 open-sourced: a 35B agentic model plus an Apache-2.0 harness |
| Nscale | 09-03 | 15× | "partnering with Nscale to deploy up to 100,000 GPUs on the NVIDIA Vera Rubin Platform" |
| Claudeforce | 08-27 | 11× | "Welcome Claudeforce" — Salesforce puts its CRM inside Claude (9.8k) |
| Groq | 09-10 | 11× | DOJ opens an antitrust investigation into Nvidia's Groq LPU deal |
| AI safety | 09-14 | 10× | a fight over who gets to speak for AI safety (29.7k) |
| Hugging Face | 09-03 | 10× | Nvidia's $12.9B acquisition of Hugging Face, days after the 700-agent HF incident |

## What the hand reads found, and the caveats

**Zero AI hits in 40 samples, all dropped:** Character (233k tweets, prec 0.01), Sol (147k, 0.07), Figure (72k, 0.02), Scale (52k, 0.11), Imagen (24k, 0.01, Spanish "imagen"), Runway (8.0k, 0.07), R1 (7.6k, 0.02, tennis and Rand amounts). **Near-zero:** Veo 0.03 (Spanish "veo"), Llama 0.07 ("se llama"), Sora 0.06 (Kingdom Hearts), Pika 0.07, Haiku 0.13, Devin 0.12 (Booker/Haney), Whisper 0.10, Elon Musk 0.11 (politics, not AI), Moonshot 0.14 (a crypto launchpad), Suleyman 0.05 (Sultan Süleyman). **Two traps:** Kimi's 10.8× "peak" on 09-06 is Kimi Antonelli winning the Italian GP, and Optimus's 16× spike on 08-27 is Peter Cullen's death (298k likes), not Tesla. **Salvageable with context:** Gemini 0.78 (zodiac + a Thai actor), Fable 0.78 (the Xbox game), Opus 0.78, Manus 0.79, Sonnet 0.75 (Shakespeare), deep research 0.76, Siri 0.72, o3 0.62 (betting lines "o3.5"), Mistral 0.67 (the poets and the wind), Murati 0.68, Altman 0.86, harness 0.85, prompt 0.83 (writing prompts), Hinton 0.34, Palantir 0.37 (surveillance politics, not AI). Three findings were pattern bugs rather than ambiguity: `\budio\b` matched Portuguese "áudio" 6,352× because RE2's `\b` is ASCII-only, `dall e` matched Italian "dalle" 6,038×, and `figure 2|3` matched paper captions — all fixed and now clean.

**Caveats.** Collection changes mid-month (August 22–29M tweets/day, September 0.8–4.8M), so only per-100k shares compare across the month; 2026-09-17 is partial and excluded from peaks. Replies are under-collected. Matching is lexical, so irony, negation and topic drift are invisible. All languages are in scope — most AI volume is non-English and the largest single tag, #AIイラスト, is Japanese. Engagement is the latest snapshot per tweet, not a final count. `scan.parquet` (357 MB) is gitignored; delete it to force a rescan. Known undercount of ~0.01–0.05%: the prefilter runs on the raw body while keywords match on the links-stripped body, and 0.012% of tweets have bodies that drift between version snapshots (AI: 102 of 907,395; ИИ: exact).
