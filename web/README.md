# web

Frontend for the topic sentiment view: one line per subtopic over time, with a
stacked volume band, a replay/live timeline, and an Ask sidebar.

```bash
npm install
npm run dev
```

## Data

The UI reads `Series[]` (see `src/data/types.ts`) from a `DataSource`
(`src/data/source.ts`). `devMockSource` is a development fixture built from
synthetic posts in `src/data/devMock.ts`; swap it for a live source (WebSocket)
when the backend is up. No component changes are needed.

Sentiment is 0 to 10 per bucket, traction-weighted (`src/data/sentiment.ts`).
Jev returns -1 to 1, so convert with `(score + 1) * 5`.

## Ask panel

Questions go out over the protocol in `src/ask/protocol.ts` with the selected
time range and subtopics. Without `VITE_ASK_URL` it uses a local mock client.
