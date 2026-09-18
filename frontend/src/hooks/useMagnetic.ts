import { useEffect, useRef, type RefObject } from 'react'

export function useMagnetic<T extends HTMLElement = HTMLElement>(
  strength = 0.32,
  radius = 70,
  hoverLift = 0,
): RefObject<T> {
  const ref = useRef<T>(null)

  useEffect(() => {
    const element = ref.current
    if (!element) return
    if (!window.matchMedia('(hover: hover) and (prefers-reduced-motion: no-preference)').matches) return

    let frame = 0
    let x = 0
    let y = 0
    let velocityX = 0
    let velocityY = 0
    let targetX = 0
    let targetY = 0
    let hovering = false

    const animate = () => {
      velocityX = (velocityX + (targetX - x) * .16) * .72
      velocityY = (velocityY + (targetY - y) * .16) * .72
      x += velocityX
      y += velocityY
      element.style.transform = `translate3d(${x.toFixed(2)}px,${y.toFixed(2)}px,0)`

      const settled = Math.abs(targetX - x) + Math.abs(targetY - y) + Math.abs(velocityX) + Math.abs(velocityY) < .08
      if (!hovering && settled) {
        element.style.removeProperty('transform')
        element.style.removeProperty('transition-property')
        frame = 0
        return
      }
      frame = window.requestAnimationFrame(animate)
    }

    const startSpring = () => {
      element.style.transitionProperty = 'box-shadow,border-color,background,color'
      if (!frame) frame = window.requestAnimationFrame(animate)
    }

    const handlePointerMove = (event: PointerEvent) => {
      const bounds = element.getBoundingClientRect()
      const baseLeft = bounds.left - x
      const baseTop = bounds.top - y
      const offsetX = event.clientX - baseLeft - bounds.width / 2
      const offsetY = event.clientY - baseTop - bounds.height / 2

      if (Math.hypot(offsetX, offsetY) < radius + bounds.width / 2) {
        hovering = true
        targetX = offsetX * strength
        targetY = offsetY * strength - hoverLift
        startSpring()
      }
    }

    const handlePointerLeave = () => {
      hovering = false
      targetX = 0
      targetY = 0
      startSpring()
    }

    element.addEventListener('pointerenter', handlePointerMove)
    element.addEventListener('pointermove', handlePointerMove)
    element.addEventListener('pointerleave', handlePointerLeave)

    return () => {
      window.cancelAnimationFrame(frame)
      element.removeEventListener('pointerenter', handlePointerMove)
      element.removeEventListener('pointermove', handlePointerMove)
      element.removeEventListener('pointerleave', handlePointerLeave)
      element.style.removeProperty('transform')
      element.style.removeProperty('transition-property')
    }
  }, [hoverLift, radius, strength])

  return ref
}
