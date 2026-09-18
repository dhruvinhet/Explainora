import { useEffect, useState } from 'react'
import { useReveal } from '../../../hooks'
import styles from '../Sections.module.css'

function useMetric(target: number, active: boolean, decimals = 0) {
  const [value, setValue] = useState(0)

  useEffect(() => {
    if (!active) return
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      setValue(target)
      return
    }
    let frame = 0
    const started = performance.now()
    const tick = (now: number) => {
      const progress = Math.min(1, (now - started) / 1100)
      const eased = 1 - Math.pow(1 - progress, 3)
      setValue(Number((target * eased).toFixed(decimals)))
      if (progress < 1) frame = window.requestAnimationFrame(tick)
    }
    frame = window.requestAnimationFrame(tick)
    return () => window.cancelAnimationFrame(frame)
  }, [active, decimals, target])

  return value
}

export function TrustStrip() {
  const reveal = useReveal<HTMLElement>()
  const explanations = useMetric(2847, reveal.revealed)
  const scenes = useMetric(61000, reveal.revealed)
  const clarity = useMetric(4.9, reveal.revealed, 1)

  return (
    <section className={`${styles.strip} ${styles.reveal} ${reveal.className}`} ref={reveal.ref} aria-label="Explainora metrics">
      <div className={`shell ${styles.stripInner}`}>
        <div className={styles.trustMain}>
          <p>Ideas made visible for curious minds, students, and teams</p>
          <div className={styles.metrics}>
            <span><b className="mono">{explanations.toLocaleString()}</b> explanations</span>
            <span><b className="mono">{Math.round(scenes / 1000)}k</b> scenes rendered</span>
            <span><b className="mono">{clarity.toFixed(1)}/5</b> clarity rating</span>
          </div>
        </div>
        <div className={styles.breadth}>
          <span className={styles.avatars} aria-label="Used by curious learners"><i>AK</i><i>MJ</i><i>RS</i><b>+2k</b></span>
          <div className={styles.topicMarquee} aria-label="Topics include Physics, Code, Biology, Finance, History and Design"><div>{['Physics','Code','Biology','Finance','History','Design','Physics','Code','Biology','Finance','History','Design'].map((topic, index) => <span key={`${topic}-${index}`}>{topic}<i>·</i></span>)}</div></div>
        </div>
      </div>
    </section>
  )
}
