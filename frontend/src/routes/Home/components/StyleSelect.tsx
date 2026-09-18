import { useEffect, useRef, useState } from 'react'
import styles from '../Home.module.css'

export type VisualStyle = 'Clean diagrams' | 'Whiteboard' | 'Blueprint'

interface StyleSelectProps {
  value: VisualStyle
  onChange: (value: VisualStyle) => void
}

export function StyleSelect({ value, onChange }: StyleSelectProps) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const close = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }

    document.addEventListener('click', close)
    return () => document.removeEventListener('click', close)
  }, [])

  const select = (nextValue: VisualStyle) => {
    onChange(nextValue)
    setOpen(false)
  }

  return (
    <div className={`${styles.ctl} ${styles.stylepick}`} ref={rootRef}>
      <label>Visual style</label>
      <button
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label={`Visual style: ${value}`}
        className={styles.stylebtn}
        onClick={(event) => {
          event.stopPropagation()
          setOpen((current) => !current)
        }}
        type="button"
      >
        <span className={styles.swatch}>
          <svg viewBox="0 0 26 18">
            <rect x="4" y="6" width="10" height="6" rx="1.5" fill="#edf1fc" stroke="#315fda" strokeWidth=".8" />
            <path d="M17 9h5M19.5 6.5 22 9l-2.5 2.5" stroke="#151515" strokeWidth=".8" fill="none" />
          </svg>
        </span>
        <span>{value}</span><span style={{ color: 'var(--text-tertiary)', fontSize: 10 }}>▾</span>
      </button>
      <div aria-hidden={!open} aria-label="Visual style" className={`${styles.stylemenu} ${open ? styles.open : ''}`} role="listbox">
        <button aria-selected={value === 'Clean diagrams'} onClick={() => select('Clean diagrams')} role="option" tabIndex={open ? 0 : -1} type="button">
          <span className={styles.thumb}>
            <svg viewBox="0 0 44 30"><rect x="7" y="10" width="16" height="10" rx="2" fill="#edf1fc" stroke="#315fda" strokeWidth="1" /><path d="M28 15h9M33 11l4 4-4 4" stroke="#151515" strokeWidth="1" fill="none" /></svg>
          </span>
          <span>Clean diagrams<small>Line art, labelled, high contrast</small></span>
        </button>
        <button aria-selected={value === 'Whiteboard'} onClick={() => select('Whiteboard')} role="option" tabIndex={open ? 0 : -1} type="button">
          <span className={styles.thumb}>
            <svg viewBox="0 0 44 30"><path d="M6 20c6-10 12 4 18-4s10 2 14-2" stroke="#151515" strokeWidth="1.4" fill="none" strokeLinecap="round" /></svg>
          </span>
          <span>Whiteboard<small>Hand-drawn, informal</small></span>
        </button>
        <button aria-selected={value === 'Blueprint'} onClick={() => select('Blueprint')} role="option" tabIndex={open ? 0 : -1} type="button">
          <span className={styles.thumb}>
            <svg viewBox="0 0 44 30"><rect width="44" height="30" fill="#edf1fc" /><path d="M0 10h44M0 20h44M14 0v30M30 0v30" stroke="#315fda" strokeWidth=".5" opacity=".5" /><rect x="16" y="12" width="12" height="7" fill="none" stroke="#315fda" /></svg>
          </span>
          <span>Blueprint<small>Technical, grid-based</small></span>
        </button>
      </div>
    </div>
  )
}
