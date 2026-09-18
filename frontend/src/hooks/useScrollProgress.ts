import { useEffect, useState } from 'react'

export interface ScrollProgress {
  stuck: boolean
  progress: number
}

const INITIAL_SCROLL_PROGRESS: ScrollProgress = {
  stuck: false,
  progress: 0,
}

export function useScrollProgress(): ScrollProgress {
  const [state, setState] = useState(INITIAL_SCROLL_PROGRESS)

  useEffect(() => {
    const update = () => {
      const height = document.documentElement.scrollHeight - window.innerHeight
      const progress = height > 0 ? window.scrollY / height : 0

      setState({
        stuck: window.scrollY > 8,
        progress: Math.min(1, Math.max(0, progress)),
      })
    }

    update()
    window.addEventListener('scroll', update, { passive: true })
    window.addEventListener('resize', update)

    return () => {
      window.removeEventListener('scroll', update)
      window.removeEventListener('resize', update)
    }
  }, [])

  return state
}
