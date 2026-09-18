import { useCallback, useEffect, useRef, useState } from 'react'

const CARET_MARKUP = '<span class="caret"></span>'

export interface GhostTypeResult {
  ghostHtml: string
  onRealInput: (value: string) => void
}

export function useGhostType(prompts: string[]): GhostTypeResult {
  const [ghostHtml, setGhostHtml] = useState('')
  const stoppedRef = useRef(false)
  const timeoutRef = useRef<number | null>(null)
  const promptsKey = prompts.join('\u0000')

  const onRealInput = useCallback((value: string) => {
    if (!value) return

    stoppedRef.current = true
    if (timeoutRef.current !== null) {
      window.clearTimeout(timeoutRef.current)
      timeoutRef.current = null
    }
    setGhostHtml('')
  }, [])

  useEffect(() => {
    if (stoppedRef.current || prompts.length === 0) return

    let promptIndex = 0
    let characterCount = 0
    let deleting = false
    let cancelled = false

    const tick = () => {
      if (cancelled || stoppedRef.current) {
        setGhostHtml('')
        return
      }

      const prompt = prompts[promptIndex]

      if (!deleting) {
        characterCount += 1
        if (characterCount > prompt.length) {
          deleting = true
          setGhostHtml(prompt + CARET_MARKUP)
          timeoutRef.current = window.setTimeout(tick, 1800)
          return
        }
      } else {
        characterCount -= 1
        if (characterCount <= 0) {
          deleting = false
          promptIndex = (promptIndex + 1) % prompts.length
          timeoutRef.current = window.setTimeout(tick, 240)
          return
        }
      }

      setGhostHtml(prompt.slice(0, characterCount) + CARET_MARKUP)
      timeoutRef.current = window.setTimeout(tick, deleting ? 18 : 46)
    }

    tick()

    return () => {
      cancelled = true
      if (timeoutRef.current !== null) {
        window.clearTimeout(timeoutRef.current)
        timeoutRef.current = null
      }
    }
  }, [prompts, promptsKey])

  return { ghostHtml, onRealInput }
}
