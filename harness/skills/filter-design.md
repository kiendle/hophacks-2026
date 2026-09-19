Choosing keywords that match the subject and not the rest of Twitter, with the traps measured on this data.

## What a filter actually is here

Your terms become case-insensitive regular-expression matches against the post text, one scan over
parquet files. There is no semantic search, no embedding, no synonym expansion: if the string is not in
the text, the post is not in the project. `any_terms` is an OR, `all_terms` an AND on top of it,
`none_terms` a veto. Between 1 and 40 terms, each 2–80 characters. Two match modes: `substring` (the
default) and `word`, which wraps the term in `\b` boundaries.

The regex engine is RE2. There is no look-ahead and no look-behind, and `\b` only works on ASCII — it
fails silently on "café" or any Japanese term, which is why `word` is not allowed to be a default. You
never write the SQL: the compiler binds each term as a parameter, so a term containing quotes, regex
metacharacters or an injection attempt is matched literally rather than executed.

## The five mistakes that ruin a project

**Short tokens over-match, enormously.** "AI" matches Thailand place names, Finnish words, "AI" inside
URLs and the initials of half a dozen clubs. "UN" matches "under", "until", "fun". "GTA" is mostly GTA
VI but also Greater Toronto Area and airport codes. If a term is three characters or fewer, it needs
`word` plus usually `case_sensitive`, and it needs a preview you actually read. The validator warns
about this; the warning is right.

**But `word` quietly drops handles.** Measured on this corpus: matching "anthropic" as a word instead
of a substring loses **15.5%** of its hits, and nearly all of the lost ones are `@AnthropicAI` — often
the most on-topic posts in the set, because they are people replying to the company. So `word` is a
tool for disambiguating short or common tokens, not a general tidiness setting. For a company or
product name, substring is normally correct, and if you want both the bare name and the handle,
substring gets them in one term.

**Giveaways and campaign templates inflate everything.** A large share of any bursty topic is the same
text posted thousands of times. The clearest case in this data: a FamilyMart campaign matched 10,323
posts of which **19** were not retweet-prefixed — that is amplification of one piece of ad copy, not
10,323 opinions. The preview reports a duplicate-text share for exactly this reason (the baseline
across non-retweets is about 4.3%, so 40% or 80% is a screaming signal), and it only computes that
share on an **exact** scan — a 100-copy template leaves roughly one copy in a 1% sample, so the
sampled tier understates duplication by about 100×. The fixes are `none_terms` on the giveaway
vocabulary ("giveaway", "RT to win", the campaign hashtag), `collapse_duplicate_text`, or a relevance
question. Prizes, quiz-result formats and sermon-slogan hashtags all behave this way.

**Most of the platform is retweets, and the pipeline does not score them.** 59% of posts are plain
retweets; the pipeline scores original posts only — no retweets, quotes or replies. So the count in your
preview and the count that gets labelled are different universes, and a topic that lives entirely in
amplification will produce a big preview and a tiny project. Check the post-type mix in the preview
before you promise anyone a volume. Replies are only about 3% of the corpus and are known to be
under-collected, so any question shaped like "how did people respond to each other" is answerable only
in a heavily qualified way — say so rather than delivering it.

**Raw counts across the September boundary are meaningless.** Collection changed on 2026-09-01: August
days hold 22–29 million tweets, September days 0.8–4.8 million. A topic whose raw count falls by 20×
on September 1 did not die; the corpus shrank 30×. Always reason in **share per 100,000 tweets that
day**, which is what the preview and every chart give you, and if a window crosses the boundary, say
in words that the raw line is a collection artefact. **2026-09-17 is a partial day** — it ends
mid-day, so its counts and any "latest value" from it are not comparable. Treat it as incomplete or
end the window at the 17th exclusive.

## Languages

Language codes must be exactly as they appear in the data, including `zxx` for posts with no linguistic
content (3.7% — mostly links and emoji) and the legacy codes `in` and `iw`. Japanese is around 26% of
the whole corpus, so leaving languages unset on a consumer, gaming or anime subject usually produces a
majority-Japanese project, and filtering to `en` on the same subject measures a minority of the actual
conversation. Either is defensible; choosing by accident is not. For Japanese and other languages
without spaces, `word` is meaningless — use `substring`.

Congress is a different source: no language column and no engagement columns at all, so `languages`,
`min_likes` and `min_views` are errors there, and its filter works on party, chamber, state and handles.

## Reading a preview like an analyst

The number to look at first is not the total, it is the per-day shape and the examples. Ask yourself:
do the top-engagement examples look like the subject? Is the duplicate share near the 4.3% baseline or
far above it? Is one language dominating in a way you did not intend? Is the busiest day the day the
event happened, or a day before it, which would mean you are matching something else?

Counts in the preview are distinct post IDs, already deduplicated across the `version` snapshots a post
accumulates as engagement grows. That matters because viral posts appear about 13% more often than
ordinary ones, so a naive row count inflates exactly the topics you care about.

When the window is wide, the preview switches to a 1% sample scaled by 100 and reports a 95% interval.
Under roughly 300 sample hits that interval is wide enough that the number is a rough order of
magnitude, and a sample count of zero means "fewer than a few hundred in the window", never "no
matches" — a term with ~100 true matches shows zero in a 1% sample more than a third of the time. Get
an exact count on the busiest day or two before drawing a conclusion.

## More keywords, or a relevance question?

The decision rule worth internalising: **lexical ambiguity wants better terms; semantic ambiguity wants
a question.** If the noise is a different string you can name — a club, a city, a giveaway phrase — fix
it with `word`, `all_terms` or `none_terms`, because that is free and reproducible. If the noise is the
same string used in a different sense, or off-topic posts that happen to mention the subject in passing,
no keyword list will separate them: add a `noul` question ("Is this post about the safety practices or
conduct of an AI company?") and a `relevance_gate` at 0.5. Gated-out posts are still stored and still
counted in `n_gated_out`, so you can tell the person how much of the catch was noise.

Two related habits. Keep overlapping term families apart: "anthropic" and "openai" hit many of the same
posts, so their counts must never be added as if they were disjoint. And remember that whole-post
sentiment is not opinion about your subject — a post mentioning a company inside a rant about something
else scores on the rant. That is a limitation to state, not to engineer away.
