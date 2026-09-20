# Sentimeter

### Understand what the world thinks—and how it changes.

Every second, people post new opinions, reactions, and experiences. How do you stay on top of what people like, what they don't, and why their opinions are changing?

Introducing **[sentimeter.surf](https://sentimeter.surf)**: scalable, agentic monitoring of public sentiment around companies, products, events, and ideas.

Sentimeter traces the evolution of public sentiment in real time and helps explain the shifts, so you can make decisions informed by public opinion—fast.

## Two layers, one view

- **The big picture: a real-time social firehose.** Follow the conversation across captured activity, compare targets, and spot changes in sentiment over time.
- **The details: granular agentic exploration.** Investigate a period, examine the posts behind a movement, and explore possible explanations with an AI assistant.

Move from “sentiment dropped” to “here is what people were reacting to,” with the underlying conversation available for inspection.

## Start with a question

Instead of building a query by hand, describe what you want to understand. The product experience centers on an intuitive AI chat that helps turn your interests into monitoring targets and retrieval filters.

For example:

> How is public sentiment toward AI companies changing, and what events are people responding to?

Choose the companies and products you care about, explore their timelines, and drill into moments that deserve attention. The same monitoring model can apply to a policy, a launch, a person, or an idea—not just AI companies.

## Demo: the AI conversation

For illustration, we use historical Twitter data from **Calcifer**, described in the demo as a corpus of approximately **365 million tweets**. Keyword retrieval narrows the conversation to roughly **800,000 captured text states** for company relevance classification and target-specific sentiment analysis with **Jev**.

The historical demo lets us explore questions such as:

| Moment | What to investigate |
| --- | --- |
| An Anthropic sentiment dip | Which posts and topics accompanied the decline? |
| A resignation | How did the conversation change around the announcement? |
| A Gemini release | What did people praise, criticize, or compare? |
| Fable | What were people discussing, and toward which targets? |
| An outage | How did reactions evolve during the disruption and recovery? |

These are investigation prompts, not predetermined conclusions. Historical replay illustrates the monitoring experience; it is not a live Twitter feed.

**Data note:** the roughly 800,000 figure describes the enrichment workload, not a claim that every item has completed inference. Repeated observations, unique posts, and distinct captured texts are different counts.

## Technical walkthrough: powered by System 1

Jev is the System 1 model used in the enrichment pipeline. The workflow separates inexpensive retrieval from semantic judgment:

```text
Captured social activity
        ↓
Keyword candidate retrieval
        ↓
Jev relevance classification for each target
        ↓
Target-specific sentiment for accepted targets
        ↓
Time-based analysis and exploration
```

1. **Capture and preserve.** Retain captured text and timestamps so results can be traced back to the source material.
2. **Retrieve candidates.** Use configurable aliases, keywords, and context to find potentially relevant posts. A keyword match is not a confirmed classification.
3. **Determine relevance.** Evaluate each configured target independently. A post can concern multiple targets; posts with no accepted target skip sentiment analysis.
4. **Judge sentiment toward each target.** Distinguish positive, negative, neutral, mixed, and insufficient evidence. A post can praise one company while criticizing another.
5. **Reuse enrichment.** Cache compatible judgments rather than rerunning inference for every repeated observation or engagement update.
6. **Explore over time.** Keep authored opinions separate from engagement changes. A like is not another authored opinion, and confidence is not sentiment intensity.

Historical Twitter preparation and live Bluesky capture provide the two source modes. Agent-generated configurations select what to monitor while preserving a consistent sentiment contract.

## From insight to action

The broader product experience brings monitoring into the tools you already use:

- **Insightful visualizations** to compare targets and explore sentiment changes.
- **Voice chat powered by ElevenLabs** to ask questions conversationally.
- **Telegram interaction** to stay connected to the conversation in real time.
- **Generated briefs** to turn an investigation into a concise update.
- **Automated alerts** to notify you when configured monitoring conditions are met.

These describe the product vision; this repository contains data preparation, enrichment, analysis, and live-capture tooling, not a claim that every product integration is implemented here.

## Interpret the signal responsibly

Sentimeter analyzes captured public conversation—not a representative survey of everyone. Source coverage, retrieval choices, model uncertainty, and missing data affect what a chart can say. A sentiment change can suggest where to investigate; timing alone does not prove its cause.

**Follow the conversation. Understand the shift. Make an informed decision.**
