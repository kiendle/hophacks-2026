import { useEffect, useRef, useState } from 'react'
import { AXIS_SHRINK_DELAY_MS, SentimentAxis, type Domain } from '../sentimentDomain'

/** Hysteresis controls when the scale changes; this is separate from data smoothing. */
export function useSentimentDomain(lo: number, hi: number): Domain {
  const axis = useRef(new SentimentAxis())
  const [domain, setDomain] = useState<Domain>([lo, hi])
  useEffect(() => {
    const update = () => {
      const next = axis.current.update([lo, hi], performance.now())
      setDomain(previous => previous[0] === next[0] && previous[1] === next[1] ? previous : next)
    }
    update()
    const timer = setTimeout(update, AXIS_SHRINK_DELAY_MS + 20)
    return () => clearTimeout(timer)
  }, [lo, hi])
  // Never clip incoming values for a frame while the effect catches up.
  return [Math.min(lo, domain[0]), Math.max(hi, domain[1])]
}
