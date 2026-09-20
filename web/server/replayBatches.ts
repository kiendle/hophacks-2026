import type { ReplayMessage } from '../src/data/replayTypes.ts'

type Batch = Extract<ReplayMessage, { type: 'batch' }>
export const MAX_BATCH_EVENTS = 1_000

/** A fast clock can cross the entire archive in one tick. Bound each frame and
 * advance its visible time only through the events that frame has delivered.
 */
export function* splitReplayBatch(batch: Batch): Generator<Batch> {
  if (batch.events.length === 0) {
    yield batch
    return
  }
  for (let offset = 0; offset < batch.events.length; offset += MAX_BATCH_EVENTS) {
    const events = batch.events.slice(offset, offset + MAX_BATCH_EVENTS)
    const final = offset + events.length === batch.events.length
    yield {
      ...batch,
      sequence: batch.sequence + offset / MAX_BATCH_EVENTS,
      events,
      now: final ? batch.now : events[events.length - 1].t,
      status: batch.status === 'complete' && !final ? 'playing' : batch.status,
    }
  }
}
