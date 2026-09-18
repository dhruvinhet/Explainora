import { useEffect, type CSSProperties } from 'react'
import { Paper } from '../../../components/Paper/Paper'
import { useReveal, useSpotlight } from '../../../hooks'
import styles from '../Sections.module.css'

export function Showcase() {
  const reveal = useReveal<HTMLDivElement>()
  const renderedSpotlight = useSpotlight<HTMLElement>()
  useEffect(() => {
    const grid = reveal.ref.current
    if (!grid) return
    let frame = 0
    const update = () => {
      window.cancelAnimationFrame(frame)
      frame = window.requestAnimationFrame(() => {
        const section = grid.closest('section') as HTMLElement | null
        const sticky = window.matchMedia('(min-width:901px)').matches && section
        const bounds = (sticky || grid).getBoundingClientRect()
        const travel = sticky ? Math.max(1, sticky.offsetHeight - window.innerHeight) : window.innerHeight * .56
        const progress = sticky
          ? Math.min(1, Math.max(0, (84 - bounds.top) / travel))
          : Math.min(1, Math.max(0, (window.innerHeight * .82 - bounds.top) / travel))
        grid.style.setProperty('--resolve', progress.toFixed(3))
      })
    }
    update()
    window.addEventListener('scroll', update, { passive: true })
    window.addEventListener('resize', update)
    return () => {
      window.cancelAnimationFrame(frame)
      window.removeEventListener('scroll', update)
      window.removeEventListener('resize', update)
    }
  }, [reveal.ref])

  return (
    <section className={`${styles.section} ${styles.showcase} ${styles.sequenceSection}`} id="showcase">
      <div className={`shell ${styles.sequenceInner}`}>
        <div className={styles.sectionHead}>
          <div className="eyebrow">Notes to visual</div>
          <h2>Your rough notes, rendered clearly</h2>
          <p>Bring the messy thinking. Explainora turns it into a visual story with structure, narration, and polish.</p>
        </div>
        <div className={`${styles.showcaseGrid} ${styles.reveal} ${reveal.className}`} ref={reveal.ref} style={{ '--resolve': 0 } as CSSProperties}>
          <Paper className={styles.notesPaper} tape={false}>
            <h3 className="hand">F1 aerodynamics — notes</h3>
            <ul className="hand">
              <li><i />air moves around + under car</li>
              <li><i />wings create <span>downforce</span></li>
              <li><i />floor speeds up the airflow</li>
              <li><i />more grip, but also more drag</li>
            </ul>
          </Paper>
          <div className={styles.renderArrow} aria-hidden="true"><b>→</b><span className="hand">Explainora renders it</span></div>
          <article className={styles.renderedCard} ref={renderedSpotlight}>
            <div className={styles.renderedMedia}>
              <svg viewBox="0 0 360 210" aria-hidden="true">
                <path d="M27 158h306" stroke="#151515" strokeWidth="2" />
                <path d="M70 145c0-12 8-19 20-19h24l14-27h55l12 27h57l20-10h30v29z" fill="#edf1fc" stroke="#151515" strokeWidth="2.3" strokeLinejoin="round" />
                <circle cx="112" cy="145" r="17" fill="#fff" stroke="#151515" strokeWidth="2.3" /><circle cx="112" cy="145" r="5" fill="#151515" />
                <circle cx="267" cy="145" r="17" fill="#fff" stroke="#151515" strokeWidth="2.3" /><circle cx="267" cy="145" r="5" fill="#151515" />
                <g fill="none" stroke="#315fda" strokeWidth="2" strokeLinecap="round" strokeDasharray="13 8"><path d="M22 151h50c44 0 63-11 105-11s72 11 116 11h44"/><path d="M22 143h50c42 0 64-8 105-8s75 8 116 8h44" opacity=".55" /></g>
                <path d="M181 45v35m-8-10 8 10 8-10" fill="none" stroke="#151515" strokeWidth="2" strokeLinecap="round" />
                <text x="181" y="32" textAnchor="middle" fontFamily="Inter" fontSize="12" fontWeight="700" fill="#151515">low pressure pulls down</text>
                <text x="181" y="188" textAnchor="middle" fontFamily="Inter" fontSize="11" fill="#6f6f6a">air accelerates beneath the floor</text>
              </svg>
            </div>
            <div className={styles.renderedCaption}><b className="mono">04</b><div><strong>Ground effect</strong><p>Air accelerates beneath the floor, lowering pressure and pulling the car toward the track.</p></div></div>
          </article>
        </div>
      </div>
    </section>
  )
}
