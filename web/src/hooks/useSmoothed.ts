import { useEffect, useRef, useState } from 'react'

/** Eases a numeric pair toward its target each frame. The first target is taken as-is. */
export function useSmoothed(lo: number, hi: number, timeConstantMs = 140): [number, number] {
  const [value, setValue] = useState<[number, number]>([lo, hi])
  const current = useRef<[number, number] | null>(null)

  useEffect(() => {
    if (!current.current) {
      current.current = [lo, hi]
      setValue([lo, hi])
      return
    }
    let raf = 0
    let last = performance.now()
    const step = (now: number) => {
      const k = 1 - Math.exp(-(now - last) / timeConstantMs)
      last = now
      const [a, b] = current.current!
      const next: [number, number] = [a + (lo - a) * k, b + (hi - b) * k]
      const done = Math.abs(next[0] - lo) < 1e-3 && Math.abs(next[1] - hi) < 1e-3
      current.current = done ? [lo, hi] : next
      setValue(current.current)
      if (!done) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [lo, hi, timeConstantMs])

  return value
}
