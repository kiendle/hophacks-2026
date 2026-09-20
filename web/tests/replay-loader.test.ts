import assert from 'node:assert/strict'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test, type TestContext } from 'node:test'
import { gzipSync } from 'node:zlib'
import { loadReplayDataset } from '../server/loadReplayDataset.ts'
import type { ReplayDataset } from '../src/data/replayTypes.ts'

const fixture: ReplayDataset = {
  version: 1, start: 0, end: 100, companies: [{ id: 'a', name: 'A' }], counts: { posts: 1, likes: 1 },
  events: [
    { id: 'post', postId: 'p', kind: 'post', t: 1, postTime: 1, text: 'Hello 🌍 — 日本語', authorId: 'a',
      contentVersion: 'v', observedAt: 2, grades: [] },
    { id: 'like', postId: 'p', kind: 'like', t: 2, postTime: 1, text: 'Hello 🌍 — 日本語', authorId: 'a',
      contentVersion: 'v', observedAt: 2, grades: [], delta: 2, opening: true },
  ],
}

function framed(data = fixture) {
  const { events, ...header } = data
  return JSON.stringify(header).slice(0, -1) + ',"events":[\n' + events.map(item => JSON.stringify(item)).join(',\n') + '\n]}\n'
}

async function archive(t: TestContext, content: string | Buffer) {
  const directory = await mkdtemp(join(tmpdir(), 'sentimeter-replay-loader-'))
  t.after(() => rm(directory, { recursive: true, force: true }))
  const file = join(directory, 'demo.json.gz')
  await writeFile(file, typeof content === 'string' ? gzipSync(content) : content)
  return file
}

test('loads framed JSON and the legacy single-line package identically', async t => {
  for (const content of [framed(), JSON.stringify(fixture), framed().replaceAll('\n', '\r\n')]) {
    assert.deepEqual(await loadReplayDataset(await archive(t, content)), fixture)
  }
})

test('preserves Unicode when event lines span decompression chunks', async t => {
  const data = structuredClone(fixture)
  data.events[0].text = '日本語🌍'.repeat(30_000) + '\nquoted "text" \\ end'
  assert.deepEqual(await loadReplayDataset(await archive(t, framed(data))), data)
})

test('preserves literal Unicode line separators inside JSON strings', async t => {
  const data = structuredClone(fixture)
  data.events[0].text = 'Grok 명령어\u2028Grok이 이제\u2029next\u0085line'
  for (const content of [framed(data), JSON.stringify(data)]) {
    assert.deepEqual(await loadReplayDataset(await archive(t, content)), data)
  }
})

test('loads an empty framed dataset', async t => {
  const data = { ...fixture, events: [], counts: { posts: 0, likes: 0 } }
  const content = framed(data).replace('[\n\n]}', '[\n]}')
  assert.deepEqual(await loadReplayDataset(await archive(t, content)), data)
})

test('rejects incomplete JSON framing and invalid comma placement', async t => {
  const valid = framed()
  for (const content of [
    valid.slice(0, valid.lastIndexOf(']}')),
    valid.replace('},\n{', '}\n{'),
    valid.replace('\n]}', ',\n]}'),
    valid + '{}\n',
    valid.replace('"kind":"post"', '"kind":"unknown"'),
  ]) {
    await assert.rejects(loadReplayDataset(await archive(t, content)))
  }
})

test('rejects counts that disagree with either format', async t => {
  for (const counts of [{ posts: 2, likes: 1 }, { posts: 0, likes: 2 }, { posts: -1, likes: 1 }]) {
    const data = { ...fixture, counts }
    for (const content of [framed(data), JSON.stringify(data)]) {
      await assert.rejects(loadReplayDataset(await archive(t, content)), /count|metadata/i)
    }
  }
})

test('rejects truncated or corrupt gzip and missing files without hanging', { timeout: 5_000 }, async t => {
  const compressed = gzipSync(framed())
  await assert.rejects(loadReplayDataset(await archive(t, compressed.subarray(0, -8))))
  await assert.rejects(loadReplayDataset(await archive(t, Buffer.from('not gzip'))))
  const file = await archive(t, framed())
  await assert.rejects(loadReplayDataset(file + '.missing'), /ENOENT/)
})
