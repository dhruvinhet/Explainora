import { useEffect, useState, type CSSProperties, type PointerEvent } from 'react'
import { Paper } from '../../../components/Paper/Paper'
import { useReveal } from '../../../hooks'
import styles from '../Sections.module.css'

type PaperStyle = CSSProperties & { '--paper-rx'?: string; '--paper-ry'?: string; '--connector'?: number }

export function HowItWorks() {
  const reveal = useReveal<HTMLDivElement>()
  const [activeStep, setActiveStep] = useState(0)

  useEffect(() => {
    const stage = reveal.ref.current
    if (!stage) return
    const update = () => {
      const section = stage.closest('section') as HTMLElement | null
      const sticky = window.matchMedia('(min-width:901px)').matches && section
      const bounds = (sticky || stage).getBoundingClientRect()
      const travel = sticky
        ? Math.max(1, sticky.offsetHeight - window.innerHeight)
        : window.innerHeight * .72 + bounds.height * .35
      const progress = sticky
        ? Math.min(1, Math.max(0, (84 - bounds.top) / travel))
        : Math.min(1, Math.max(0, (window.innerHeight * .72 - bounds.top) / travel))
      setActiveStep(Math.min(2, Math.floor(progress * 3)))
    }
    update()
    window.addEventListener('scroll', update, { passive: true })
    window.addEventListener('resize', update)
    return () => {
      window.removeEventListener('scroll', update)
      window.removeEventListener('resize', update)
    }
  }, [reveal.ref])

  const tiltPaper = (event: PointerEvent<HTMLDivElement>) => {
    if (!window.matchMedia('(hover:hover) and (prefers-reduced-motion:no-preference)').matches) return
    const bounds = event.currentTarget.getBoundingClientRect()
    const x = (event.clientX - bounds.left) / bounds.width - .5
    const y = (event.clientY - bounds.top) / bounds.height - .5
    event.currentTarget.style.setProperty('--paper-rx', `${-y * 2}deg`)
    event.currentTarget.style.setProperty('--paper-ry', `${x * 2}deg`)
  }

  return (
    <section className={`${styles.section} ${styles.sequenceSection}`} id="how">
      <div className={`shell ${styles.sequenceInner}`}>
        <div className={styles.sectionHead}>
          <div className="eyebrow">How it works</div>
          <h2>From a question to a finished explanation</h2>
          <p>You bring the curiosity. Explainora plans the story, draws the visuals, and turns it into something worth watching.</p>
        </div>
        <div className={`${styles.paperStage} ${styles.reveal} ${reveal.className}`} onPointerMove={tiltPaper} onPointerLeave={(event) => { event.currentTarget.style.removeProperty('--paper-rx'); event.currentTarget.style.removeProperty('--paper-ry') }} ref={reveal.ref} style={{ '--connector': (activeStep + 1) / 3 } as PaperStyle}>
          <Paper className={styles.stepsPaper} tape>
            <span className={`hand ${styles.funPart}`}>the fun part →</span>
            <div className={styles.steps}>
              <svg className={styles.stepConnector} viewBox="0 0 900 70" preserveAspectRatio="none" aria-hidden="true"><path d="M70 36 C220 4 278 64 440 35 S690 7 830 35" /></svg>
              <article data-active={activeStep === 0} data-step="0">
                <span className={`hand ${styles.stepNumber}`}>01</span>
                <h3>You ask a question</h3>
                <p>Start with any idea, concept, or rough question you want to understand.</p>
                <div className={styles.sketch}>
                  <svg viewBox="0 0 220 94" aria-hidden="true"><rect x="18" y="22" width="184" height="50" rx="10"/><path d="M37 42h106M37 55h72"/><circle cx="180" cy="47" r="10"/><path d="m176 47 3 3 6-7"/></svg>
                </div>
              </article>
              <article data-active={activeStep === 1} data-step="1">
                <span className={`hand ${styles.stepNumber}`}>02</span>
                <h3>We sketch the scenes</h3>
                <p>Explainora finds the clearest sequence and writes narration for every visual beat.</p>
                <div className={styles.sketch}>
                  <svg viewBox="0 0 220 94" aria-hidden="true"><rect x="15" y="25" width="48" height="42" rx="7"/><rect x="86" y="25" width="48" height="42" rx="7"/><rect x="157" y="25" width="48" height="42" rx="7"/><path d="M63 46h17m54 0h17M74 41l6 5-6 5m71-10 6 5-6 5"/></svg>
                </div>
              </article>
              <article data-active={activeStep === 2} data-step="2">
                <span className={`hand ${styles.stepNumber}`}>03</span>
                <h3>It becomes a video</h3>
                <p>Diagrams, voice, pacing, and chapters arrive as one polished explanation.</p>
                <div className={styles.sketch}>
                  <svg viewBox="0 0 220 94" aria-hidden="true"><path d="M30 58c33-27 90-32 158-12"/><path d="M30 58c35-14 93-17 158-2"/><path className={styles.blueLine} d="M14 31c52-12 113-6 190 15M14 70c57 9 120 3 190-9"/></svg>
                </div>
              </article>
            </div>
          </Paper>
        </div>
      </div>
    </section>
  )
}
