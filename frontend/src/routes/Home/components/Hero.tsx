import { useEffect, useRef, useState, type CSSProperties, type PointerEvent } from 'react'
import { useCountUp } from '../../../hooks'
import { ArtMarkup } from './ArtMarkup'
import styles from '../Home.module.css'

type DotGridStyle = CSSProperties & {
  '--mx'?: string
  '--my'?: string
}

export function Hero() {
  const [started, setStarted] = useState(false)
  const backgroundRef = useRef<HTMLDivElement>(null)
  const count = useCountUp(2847)

  useEffect(() => {
    setStarted(true)
  }, [])

  const handlePointerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (!window.matchMedia('(hover: hover) and (prefers-reduced-motion: no-preference)').matches) return
    const background = backgroundRef.current
    if (!background) return

    const bounds = background.getBoundingClientRect()
    background.style.setProperty('--mx', `${event.clientX - bounds.left}px`)
    background.style.setProperty('--my', `${event.clientY - bounds.top}px`)
  }

  const handlePointerLeave = () => {
    backgroundRef.current?.style.setProperty('--mx', '-500px')
  }

  return (
    <div
      className={`${styles.hero} ${started ? 'is-go' : ''}`}
      onPointerLeave={handlePointerLeave}
      onPointerMove={handlePointerMove}
    >
      <div
        className={styles['hero-bg']}
        ref={backgroundRef}
        style={{ '--mx': '-500px', '--my': '-500px' } as DotGridStyle}
      />
      <div className={`shell ${styles['hero-grid']}`}>
        <div>
          <h1>
            <span className={styles.w} style={{ animationDelay: '0ms' }}>Explain</span>{' '}
            <span className={styles.w} style={{ animationDelay: '80ms' }}>anything</span>{' '}
            <span className={styles.nb}>
              <span
                className={`${styles.w} ${styles['arrow-wrap']}`}
                style={{ animationDelay: '160ms' }}
              >
                <svg width="86" height="34" viewBox="0 0 86 34">
                  <g className={styles['arrow-dot']}><circle cx="76" cy="17" r="3.4" /></g>
                  <path className={styles['arrow-path']} d="M4 17 H70 M56 5 L70 17 L56 29" />
                </svg>
              </span>{' '}
              <span className={styles.w} style={{ animationDelay: '240ms' }}>video</span>
            </span>
          </h1>
          <p className={styles['hero-copy']}>
            Turn any question, concept or idea into a clear visual explanation.
          </p>
          <div className={styles.trust}>
            <span className={styles.avatars}><i /><i /><i /><i /><i /></span>
            <b className="num" style={{ color: 'var(--text)', fontWeight: 700 }}>{count}</b>{' '}
            explanations generated
            <span className={styles['dot-sep']} /> ⌀ 6 min per video
            <span className={styles['dot-sep']} /> 9 scenes average
          </div>
        </div>

        <div className={styles.proof}>
          <div className={styles['proof-media']}>
            <ArtMarkup artKey="parse" className={styles['proof-art']} />
            <div className={styles['proof-bar']} />
          </div>
          <div className={styles['proof-foot']}>
            <span className={styles['play-chip']}>▶</span>
            How does a compiler understand code?
            <span className="num">2:11</span>
          </div>
        </div>
      </div>
    </div>
  )
}
