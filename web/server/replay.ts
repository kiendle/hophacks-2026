import { stat } from 'node:fs/promises'
import { randomUUID } from 'node:crypto'
import { WebSocket, WebSocketServer } from 'ws'
import type { Plugin, ViteDevServer } from 'vite'
import { DEFAULT_SPEED, type ReplayCommand, type ReplayDataset, type ReplayMessage } from '../src/data/replayTypes.ts'
import { ReplayClock } from './replayClock.ts'
import { loadReplayDataset } from './loadReplayDataset.ts'
import { splitReplayBatch } from './replayBatches.ts'

export function replayPlugin(): Plugin {
  let dataset: Promise<ReplayDataset> | undefined
  let datasetStamp = ''
  const datasetPath = new URL('../data/demo.json.gz', import.meta.url)
  const load = async () => {
    const info = await stat(datasetPath)
    const stamp = `${info.mtimeMs}:${info.ctimeMs}:${info.size}`
    if (!dataset || stamp !== datasetStamp) {
      datasetStamp = stamp
      dataset = loadReplayDataset(datasetPath)
        .catch((error) => { dataset = undefined; throw error })
    }
    return dataset
  }

  function attach(server: ViteDevServer['httpServer']) {
    if (!server) return
    const wss = new WebSocketServer({ noServer: true })
    server.on('upgrade', (request, socket, head) => {
      if (request.url?.split('?')[0] !== '/api/replay') return
      if (request.headers.origin && new URL(request.headers.origin).host !== request.headers.host) {
        socket.destroy()
        return
      }
      wss.handleUpgrade(request, socket, head, (ws) => wss.emit('connection', ws, request))
    })
    wss.on('connection', async (ws) => {
      const send = (message: ReplayMessage) => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message)) }
      try {
        const data = await load()
        if (ws.readyState !== WebSocket.OPEN) return
        let clock: ReplayClock
        let run = ''
        let sequence = 0
        const restart = (speed = DEFAULT_SPEED) => {
          run = randomUUID()
          sequence = 0
          clock = new ReplayClock(data, speed, performance.now())
          send({ type: 'init', run, start: data.start, end: data.end, companies: data.companies, speed })
        }
        const flush = () => {
          const events = clock.tick(performance.now())
          for (const batch of splitReplayBatch({ type: 'batch', run, sequence, now: clock.now,
            events, status: clock.status, speed: clock.speed })) {
            send(batch)
            sequence++
          }
        }
        restart()
        flush()
        const timer = setInterval(() => {
          if (clock.status === 'playing' && ws.bufferedAmount < 1_000_000) flush()
        }, 100)
        ws.on('message', (raw) => {
          try {
            const command = JSON.parse(raw.toString()) as ReplayCommand
            if (command.type === 'restart') restart(clock.speed)
            else if (command.type === 'pause') { flush(); clock.pause() }
            else if (command.type === 'resume') clock.resume(performance.now())
            else if (command.type === 'speed' && Number.isFinite(command.speed) && command.speed >= 1 && command.speed <= 1_000_000) {
              flush()
              clock.speed = command.speed
            }
            flush()
          } catch { send({ type: 'error', message: 'Invalid replay command.' }) }
        })
        ws.on('close', () => clearInterval(timer))
      } catch {
        send({ type: 'error', message: 'Dataset unavailable. Run the dataset preparation script.' })
        ws.close()
      }
    })
    server.on('close', () => { for (const client of wss.clients) client.terminate(); wss.close() })
  }
  return {
    name: 'sentimeter-replay',
    configureServer(server) { attach(server.httpServer) },
    configurePreviewServer(server) { attach(server.httpServer) },
  }
}
