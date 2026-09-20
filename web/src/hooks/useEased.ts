import { useEffect, useRef, useState } from 'react'

/**
 * Eases every number in `target` toward its new value each frame, so values
 * drift instead of jumping. A change in length snaps to the target.
 */
export function useEased(target: number[], timeConstantMs = 220): number[] {
  const [value, setValue] = useState(target)
  const current = useRef<number[] | null>(null)
  const key = target.join(',')

  useEffect(() => {
    const goal = key ? key.split(',').map(Number) : []
    if (!current.current || current.current.length !== goal.length) {
      current.current = goal
      setValue(goal)
      return
    }
    let raf = 0
    let last = performance.now()
    const step = (now: number) => {
      const k = 1 - Math.exp(-(now - last) / timeConstantMs)
      last = now
      let done = true
      const next = current.current!.map((v, i) => {
        const d = goal[i] - v
        if (Math.abs(d) > 1e-3) done = false
        return v + d * k
      })
      current.current = done ? goal : next
      setValue(current.current)
      if (!done) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [key, timeConstantMs])

  return value
}
