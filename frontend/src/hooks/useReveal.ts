import { useLayoutEffect, useRef, useState, type RefObject } from 'react'

export interface RevealResult<T extends Element> {
  ref: RefObject<T>
  revealed: boolean
  className: '' | 'is-in'
}

export function useReveal<T extends Element = HTMLElement>(): RevealResult<T> {
  const ref = useRef<T>(null)
  const [revealed, setRevealed] = useState(() => (
    typeof window !== 'undefined'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  ))

  useLayoutEffect(() => {
    const initialElement = ref.current
    if (!initialElement) return
    let element: T = initialElement

    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)')
    const observer = { current: undefined as IntersectionObserver | undefined }
    let fallbackTimer = 0
    let layoutFrame = 0
    let settleFrame = 0

    const show = () => {
      if (!element) return
      element.classList.add('is-in')
      element.setAttribute('data-reveal-visible', 'true')
      observer.current?.unobserve(element)
      window.clearTimeout(fallbackTimer)
      fallbackTimer = 0
      setRevealed(true)
    }

    const isInViewport = () => {
      if (!element) return false
      const bounds = element.getBoundingClientRect()
      return bounds.bottom >= 0
        && bounds.right >= 0
        && bounds.top <= window.innerHeight
        && bounds.left <= window.innerWidth
    }

    element.setAttribute('data-reveal', '')
    if (revealed || reducedMotion.matches || !('IntersectionObserver' in window)) {
      show()
      return () => undefined
    }

    observer.current = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.target === element && entry.isIntersecting) show()
        })
      },
      { threshold: 0.01, rootMargin: '0px 0px -6% 0px' },
    )

    const observeCurrentNode = () => {
      const current = ref.current
      if (!current || current.classList.contains('is-in')) return
      if (current !== element) {
        observer.current?.unobserve(element)
        element = current
        element.setAttribute('data-reveal', '')
      }
      // Re-registering after layout also covers nodes mounted or moved after
      // the first paint, which can otherwise miss their initial intersection.
      observer.current?.unobserve(element)
      observer.current?.observe(element)
    }

    const scheduleViewportFallback = () => {
      if (!isInViewport()) {
        window.clearTimeout(fallbackTimer)
        fallbackTimer = 0
        return
      }
      if (fallbackTimer) return
      fallbackTimer = window.setTimeout(() => {
        fallbackTimer = 0
        if (isInViewport()) show()
      }, 400)
    }

    observer.current.observe(element)
    layoutFrame = window.requestAnimationFrame(() => {
      observeCurrentNode()
      settleFrame = window.requestAnimationFrame(observeCurrentNode)
    })

    const mutations = new MutationObserver(observeCurrentNode)
    mutations.observe(document.body, { childList: true, subtree: true })

    scheduleViewportFallback()
    window.addEventListener('scroll', scheduleViewportFallback, { passive: true })
    window.addEventListener('resize', scheduleViewportFallback)

    const handleMotionChange = (event: MediaQueryListEvent) => {
      if (event.matches) show()
    }
    reducedMotion.addEventListener('change', handleMotionChange)

    return () => {
      observer.current?.disconnect()
      mutations.disconnect()
      reducedMotion.removeEventListener('change', handleMotionChange)
      window.removeEventListener('scroll', scheduleViewportFallback)
      window.removeEventListener('resize', scheduleViewportFallback)
      window.clearTimeout(fallbackTimer)
      window.cancelAnimationFrame(layoutFrame)
      window.cancelAnimationFrame(settleFrame)
    }
  }, [revealed])

  return { ref, revealed, className: revealed ? 'is-in' : '' }
}
