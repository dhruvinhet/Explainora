import { Link, useNavigate } from 'react-router-dom'
import { useReveal } from '../../../hooks'
import { Button } from '../../../components/Button/Button'
import styles from '../Home.module.css'

export function Footer() {
  const navigate = useNavigate()
  const wordmark = useReveal<HTMLDivElement>()

  const startExplaining = () => {
    navigate('/app')
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  return (
    <footer className={styles.foot}>
      <div className={styles['foot-cta']}>
        <div className={`shell ${styles['foot-cta-in']}`}>
          <div>
            <h3>Explain anything → video</h3>
            <p>Turn any question, concept or idea into a clear visual explanation.</p>
          </div>
          <Button
            className={styles['btn-primary']}
            onClick={startExplaining}
            style={{ height: 50, padding: '0 26px', fontSize: 15.5 }}
            arrow
          >
            Start explaining
          </Button>
        </div>
      </div>

      <div className="shell">
        <div className={styles['foot-cols']}>
          <div>
            <h5>Product</h5>
            <Link to="/app">New video</Link><Link to="/app/videos">My videos</Link><Link to="/app/plans">Plans</Link>
          </div>
          <div>
            <h5>Examples</h5>
            <Link to="/app/videos">Ocean currents</Link><Link to="/app/videos">How GPS works</Link><Link to="/app/videos">Lexing &amp; parsing</Link>
          </div>
          <div>
            <h5>Help</h5>
            <Link to="/support">Support</Link><a href="mailto:support@explainora.dev">Email us</a><Link to="/app/account">My account</Link>
          </div>
          <div>
            <h5>Legal</h5>
            <Link to="/privacy">Privacy</Link><Link to="/terms">Terms</Link><Link to="/refunds">Refunds</Link>
          </div>
        </div>
        <div className={styles['foot-legal']}>
          <span>© 2026 Explainora</span>
          <span><Link to="/privacy">Privacy</Link> · <Link to="/terms">Terms</Link> · <Link to="/refunds">Refunds</Link></span>
          <span><span className={styles['status-dot']} />All systems operational</span>
        </div>
        <div
          className={`${styles.wordmark} ${styles.reveal} ${wordmark.className}`}
          ref={wordmark.ref}
          style={{ transitionDelay: '180ms' }}
        >
          EXPLAINORA
        </div>
      </div>
    </footer>
  )
}
