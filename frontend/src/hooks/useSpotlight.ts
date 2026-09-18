import { useEffect, useRef, type RefObject } from 'react'

export function useSpotlight<T extends HTMLElement = HTMLElement>(): RefObject<T> {
  const ref = useRef<T>(null)

  useEffect(() => {
    const element = ref.current
    if (!element) return
    if (!window.matchMedia('(hover: hover) and (prefers-reduced-motion: no-preference)').matches) return

    const handlePointerMove = (event: PointerEvent) => {
      const bounds = element.getBoundingClientRect()
      element.style.setProperty('--cx', `${event.clientX - bounds.left}px`)
      element.style.setProperty('--cy', `${event.clientY - bounds.top}px`)
    }

    element.addEventListener('pointermove', handlePointerMove)
    return () => element.removeEventListener('pointermove', handlePointerMove)
  }, [])

  return ref
}
