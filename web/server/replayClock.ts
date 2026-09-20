import type { ReplayDataset, ReplayEvent, ReplayStatus } from '../src/data/replayTypes.ts'

/** Pure virtual clock: pause never accumulates wall time, and ties stay ordered. */
export class ReplayClock {
  now: number
  status: ReplayStatus = 'playing'
  private cursor = 0
  private lastWall: number
  private data: ReplayDataset
  speed: number

  constructor(data: ReplayDataset, speed: number, wall: number) {
    this.data = data
    this.now = data.start
    this.speed = speed
    this.lastWall = wall
  }

  tick(wall: number): ReplayEvent[] {
    if (this.status === 'playing') this.now = Math.min(this.data.end, this.now + Math.max(0, wall - this.lastWall) * this.speed)
    this.lastWall = wall
    const start = this.cursor
    while (this.cursor < this.data.events.length && this.data.events[this.cursor].t <= this.now) this.cursor++
    if (this.now >= this.data.end) this.status = 'complete'
    return this.data.events.slice(start, this.cursor)
  }

  pause() { if (this.status === 'playing') this.status = 'paused' }
  resume(wall: number) {
    if (this.status !== 'complete') this.status = 'playing'
    this.lastWall = wall
  }
}
