import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react'
import styles from '../Home.module.css'

export interface SegmentOption<T extends string | number> {
  label: string
  value: T
  disabled?: boolean
  hint?: string
}

interface SegmentedControlProps<T extends string | number> {
  ariaLabel: string
  options: readonly SegmentOption<T>[]
  value: T
  onChange: (value: T) => void
}

export function SegmentedControl<T extends string | number>({
  ariaLabel,
  options,
  value,
  onChange,
}: SegmentedControlProps<T>) {
  const rootRef = useRef<HTMLDivElement>(null)
  const buttonRefs = useRef<Array<HTMLButtonElement | null>>([])
  const [pillStyle, setPillStyle] = useState<CSSProperties>({})

  const movePill = useCallback(() => {
    const selectedIndex = options.findIndex((option) => option.value === value)
    const selectedButton = buttonRefs.current[selectedIndex]
    if (!selectedButton) return

    setPillStyle({
      width: selectedButton.offsetWidth,
      transform: `translateX(${selectedButton.offsetLeft - 3}px)`,
    })
  }, [options, setPillStyle, value])

  useLayoutEffect(() => {
    const timer = window.setTimeout(movePill, 80)
    return () => window.clearTimeout(timer)
  }, [movePill])

  useEffect(() => {
    const root = rootRef.current
    if (!root) return

    const observer = new ResizeObserver(movePill)
    observer.observe(root)
    return () => observer.disconnect()
  }, [movePill])

  return (
    <div aria-label={ariaLabel} className={styles.seg} ref={rootRef} role="tablist">
      <span className={styles.pill} style={pillStyle} />
      {options.map((option, index) => (
        <button
          aria-label={option.hint ? `${option.label}. ${option.hint}` : option.label}
          aria-selected={option.value === value}
          disabled={option.disabled}
          key={option.value}
          onClick={() => onChange(option.value)}
          ref={(element) => { buttonRefs.current[index] = element }}
          role="tab"
          type="button"
        >
          {option.label}{option.disabled && <span aria-hidden="true"> · 🔒</span>}
        </button>
      ))}
    </div>
  )
}
