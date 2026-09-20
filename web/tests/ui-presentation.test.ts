import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

// Use the app's transform so the real React components and CSS imports are tested.
const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
after(() => server.close())
const { Message } = await server.ssrLoadModule('/src/components/ChatSidebar.tsx')
const { HoverCard } = await server.ssrLoadModule('/src/components/HoverCard.tsx')
const { TopBar } = await server.ssrLoadModule('/src/components/TopBar.tsx')
const { Home } = await server.ssrLoadModule('/src/app/Home.tsx')

test('landing keeps rounded ASCII branding and the singular keyword prompt', () => {
  const html = renderToStaticMarkup(createElement(Home, { onSubmit: () => {}, onCreateAutomation: () => {} }))
  assert.match(html, /“How do people feel about AI companies\?”/)
  assert.match(html, /placeholder="Keyword"/)
  assert.match(html, /aria-label="Keyword"/)
  const ascii = html.match(/class="sentimeter-logo-ascii" role="img" aria-label="Sentimeter">([^<]+)<\/span>/)?.[1].replaceAll('&quot;', '"')
  assert.ok(ascii)
  assert.equal(ascii.split('\n').length, 8)
  assert.ok(ascii.split('\n').every(row => row.length === ascii.split('\n')[0].length))
  assert.match(ascii, /\.d8888b\./)
  assert.match(ascii, /Y8888P/)
  assert.doesNotMatch(ascii, /#/)
})

test('Sentibot renders collapsed evidence and scored citations before its response', () => {
  const post = { id: 'p', subtopic: 'openai', handle: 'ID 1234567890', text: 'SCORED CITATION',
    sentiment: 7, likes: 10, replies: 0, retweets: 0, quotes: 0, time: '' }
  const html = renderToStaticMarkup(createElement(Message, {
    message: { id: 'answer', role: 'assistant', text: 'MAIN RESPONSE', status: 'streaming', steps: [],
      cards: [{ kind: 'preview', title: 'What we found on X/Twitter', total: 2, examples: [] }],
      citations: [post, { ...post, id: 'unscored', text: 'UNSCORED POST', scored: false }] },
    byId: new Map(), voice: {}, talking: false, busy: true, onConfirm: () => {},
  }))
  assert.match(html, /<details class="tool-card tool-preview"><summary>What we found on X\/Twitter<\/summary>/)
  assert.ok(html.indexOf('What we found') < html.indexOf('MAIN RESPONSE'))
  assert.ok(html.indexOf('SCORED CITATION') < html.indexOf('MAIN RESPONSE'))
  assert.doesNotMatch(html, /UNSCORED POST|Unscored|1234567890/)
})

test('tweet popup removes the ID and cuts off long text while retaining its footer', () => {
  const html = renderToStaticMarkup(createElement(HoverCard, {
    post: { id: 'tweet-id', handle: 'ID 1234567890', text: 'x'.repeat(2000) + 'HIDDEN TAIL', time: 0,
      sentiment: 7.5, likes: 99, periodLikes: 42, replies: 0, retweets: 0, quotes: 0, otherMetricsKnown: false },
    anchor: { x: 700, y: 490 }, bounds: { width: 800, height: 500 },
  }))
  assert.doesNotMatch(html, /1234567890|tweet-id|HIDDEN TAIL/)
  assert.match(html, /x{400}…/)
  assert.match(html, /Likes received in this period/)
  assert.match(html, />42</)
  assert.match(html, />7\.5</)
})

test('both chart modes expose the higher playback speeds', () => {
  for (const mode of ['line', 'bubble']) {
    const html = renderToStaticMarkup(createElement(TopBar, {
      topic: 'AI', series: [], hidden: new Set(), terms: ['AI'], playing: false, speed: 604800,
      intervalMs: 43200000, events: { read: 0, kept: 0 }, mode,
      onToggleSeries: () => {}, onIsolateSeries: () => {}, onTermsChange: () => {},
      onTogglePlay: () => {}, onSpeedChange: () => {}, onIntervalChange: () => {}, onModeChange: () => {},
    }))
    assert.match(html, /<option value="86400">1d \/ s<\/option>/)
    assert.match(html, /<option value="259200">3d \/ s<\/option>/)
    assert.match(html, /<option value="604800" selected="">7d \/ s<\/option>/)
  }
})
