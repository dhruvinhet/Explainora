import { useEffect, useRef, useState } from 'react'
import { useReveal } from '../../../hooks'
import { ArtMarkup } from './ArtMarkup'
import styles from '../Home.module.css'

export function ProcessSteps() {
  const sectionRef = useRef<HTMLElement>(null)
  const [activeStep, setActiveStep] = useState(0)
  const heading = useReveal<HTMLDivElement>()

  useEffect(() => {
    const update = () => {
      const section = sectionRef.current
      if (!section) return

      const bounds = section.getBoundingClientRect()
      const progress = Math.min(
        1,
        Math.max(0, (window.innerHeight * 0.75 - bounds.top) / (bounds.height * 0.75)),
      )
      setActiveStep(progress < 0.34 ? 0 : progress < 0.67 ? 1 : 2)
    }

    update()
    window.addEventListener('scroll', update, { passive: true })
    return () => window.removeEventListener('scroll', update)
  }, [])

  return (
    <section className={`${styles.sec} ${styles.muted}`} ref={sectionRef}>
      <div className="shell">
        <div
          className={`${styles['sec-head']} ${styles.reveal} ${heading.className}`}
          ref={heading.ref}
          style={{ transitionDelay: '90ms' }}
        >
          <h2>One question. A complete explanation.</h2>
        </div>
        <div className={styles.steps}>
          <div className={styles.connector} style={{ left: '33%', width: '2%' }}>
            <div className={styles.fill} style={{ width: activeStep >= 1 ? '100%' : '0' }} />
          </div>
          <div className={styles.connector} style={{ left: '66%', width: '2%' }}>
            <div className={styles.fill} style={{ width: activeStep >= 2 ? '100%' : '0' }} />
          </div>

          <article className={`${styles.step} ${activeStep >= 0 ? styles.on : ''}`}>
            <div className={styles.n}>01 — ASK</div>
            <h4>Explain how neural networks learn</h4>
            <div className={styles['step-media']} style={{ background: 'var(--surface)' }}>
              <div style={{ fontSize: 12, color: 'var(--text-tertiary)', padding: 12, textAlign: 'center' }}>
                a single sentence, typed
              </div>
            </div>
          </article>

          <article className={`${styles.step} ${activeStep >= 1 ? styles.on : ''}`}>
            <div className={styles.n}>02 — WE PLAN</div>
            <h4>Eight scenes, in the right order</h4>
            <div className={styles['plan-chips']}>
              <span style={{ animationDelay: '0ms' }}>Training data</span>
              <span style={{ animationDelay: '70ms' }}>Neurons</span>
              <span style={{ animationDelay: '140ms' }}>Prediction</span>
              <span style={{ animationDelay: '210ms' }}>Error</span>
              <span style={{ animationDelay: '280ms' }}>Learning</span>
            </div>
            <div className={styles['step-media']} style={{ background: 'var(--surface)' }}>
              <div style={{ fontSize: 12, color: 'var(--text-tertiary)', padding: 12, textAlign: 'center' }}>
                narration + diagrams, scene by scene
              </div>
            </div>
          </article>

          <article className={`${styles.step} ${activeStep >= 2 ? styles.on : ''}`}>
            <div className={styles.n}>03 — WATCH</div>
            <h4>A finished visual explanation</h4>
            <div className={styles['step-media']} style={{ padding: 0, background: 'var(--surface)' }}>
              <ArtMarkup
                artKey="nn"
                className={styles['process-art']}
              />
            </div>
          </article>
        </div>
      </div>
    </section>
  )
}
