import { useEffect, useState } from 'react'

const REFERENCE_TICK_MS = 34
const EASING_DIVISOR = 12

function countReferenceSteps(target: number): number {
  let current = 0
  let steps = 0

  while (current < target) {
    current += Math.ceil((target - current) / EASING_DIVISOR) || 1
    steps += 1
  }

  return steps
}

export function useCountUp(target: number, durationApprox?: number): string {
  const normalizedTarget = Math.max(0, Math.floor(target))
  const [current, setCurrent] = useState(0)

  useEffect(() => {
    setCurrent(0)
    if (normalizedTarget === 0) return
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      setCurrent(normalizedTarget)
      return
    }

    let value = 0
    const referenceSteps = countReferenceSteps(normalizedTarget)
    const tickMs = durationApprox && durationApprox > 0
      ? durationApprox / referenceSteps
      : REFERENCE_TICK_MS

    const interval = window.setInterval(() => {
      value += Math.ceil((normalizedTarget - value) / EASING_DIVISOR) || 1

      if (value >= normalizedTarget) {
        value = normalizedTarget
        window.clearInterval(interval)
      }

      setCurrent(value)
    }, tickMs)

    return () => window.clearInterval(interval)
  }, [durationApprox, normalizedTarget])

  return current.toLocaleString()
}
