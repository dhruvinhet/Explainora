import { useEffect } from 'react'
import { Link } from 'react-router-dom'
import { ButtonLink } from '../../../components/Button/Button'
import { useMagnetic, useReveal } from '../../../hooks'
import styles from '../Sections.module.css'

export function LandingFooter() {
  const wordmark = useReveal<HTMLDivElement>()
  const ctaReveal = useReveal<HTMLDivElement>()
  const ctaRef = useMagnetic<HTMLAnchorElement>(.18, 70, 1)

  useEffect(() => {
    const mark = wordmark.ref.current
    if (!mark) return
    const update = () => {
      const bounds = mark.getBoundingClientRect()
      const progress = Math.min(1, Math.max(0, (window.innerHeight - bounds.top) / (window.innerHeight * .45)))
      mark.style.setProperty('--footer-shift', `${Math.round((1 - progress) * 34)}px`)
    }
    update()
    window.addEventListener('scroll', update, { passive: true })
    return () => window.removeEventListener('scroll', update)
  }, [wordmark.ref])

  return (
    <>
      <section className={styles.ctaSection}>
        <div className={`shell ${styles.ctaBand} ${styles.reveal} ${ctaReveal.className}`} ref={ctaReveal.ref}>
          <div><h2>What do you want to understand?</h2><p>Turn the next question on your mind into a visual explanation.</p></div>
          <div className={styles.ctaAction}><ButtonLink arrow className={styles.ctaButton} ref={ctaRef} to="/app" viewTransition>Open the studio</ButtonLink><small>No card needed · most videos take ~6 min</small></div>
        </div>
      </section>
      <footer className={styles.footer}>
        <div className="shell">
          <div className={styles.footerGrid}>
            <div className={styles.footerBrand}><Link to="/"><span>E</span><b>EXPLAINORA</b></Link><p>Ideas, made visible.</p><div className={styles.socials}><Link to="/support" aria-label="Contact Explainora support">?</Link><a href="mailto:support@explainora.dev" aria-label="Email Explainora support">@</a></div></div>
            <div><h3>Product</h3><Link to="/app">New video</Link><Link to="/app/videos">My videos</Link><a href="#features">Features</a></div>
            <div><h3>Explore</h3><a href="#how">How it works</a><a href="#examples">Examples</a><Link to="/app/videos">Video library</Link></div>
            <div><h3>Legal & help</h3><Link to="/support">Support</Link><Link to="/privacy">Privacy</Link><Link to="/terms">Terms</Link><Link to="/refunds">Refunds</Link></div>
          </div>
          <div className={styles.legal}><span>© 2026 Explainora</span><span><Link to="/privacy">Privacy</Link> · <Link to="/terms">Terms</Link> · <Link to="/refunds">Refunds</Link></span><span><i />All systems operational</span></div>
          <div className={`${styles.footerWordmark} ${styles.reveal} ${wordmark.className}`} ref={wordmark.ref}>EXPLAINORA</div>
        </div>
      </footer>
    </>
  )
}
