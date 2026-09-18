import { useState } from 'react'
import { Link, Navigate, useSearchParams } from 'react-router-dom'
import { useAuthUser, type AuthUser } from '../../lib/auth'
import { startBillingCheckout } from '../../lib/api'
import styles from './Checkout.module.css'

type PaidPlan = Exclude<AuthUser['plan'], 'Free'>
type CheckoutStatus = 'ready' | 'processing'

const checkoutPlans: Record<PaidPlan, { price: string; color: string; videos: number }> = {
  Pro: { price: '19.00', color: 'royal', videos: 10 },
  'Pro Ultra': { price: '39.00', color: 'purple', videos: 20 },
}

const planRank: Record<AuthUser['plan'], number> = { Free: 0, Pro: 1, 'Pro Ultra': 2 }

export function Checkout() {
  const user = useAuthUser()
  const [searchParams] = useSearchParams()
  const requested = searchParams.get('plan')
  const plan: PaidPlan | null = requested === 'Pro' || requested === 'Pro Ultra' ? requested : null
  const [status, setStatus] = useState<CheckoutStatus>('ready')
  const [error, setError] = useState('')

  if (!user) return <Navigate replace to="/auth" />
  if (!plan || (status === 'ready' && planRank[plan] <= planRank[user.plan])) return <Navigate replace to="/app/plans" />

  const details = checkoutPlans[plan]
  const submit = async () => {
    setStatus('processing')
    setError('')
    try {
      const checkout = await startBillingCheckout(plan)
      window.location.assign(checkout.checkout_url)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Secure billing is unavailable.')
      setStatus('ready')
    }
  }

  return (
    <main className={`${styles.page} ${styles[details.color]}`}>
      <div aria-hidden="true" className={styles.grid} />
      <header className={styles.header}>
        <Link className={styles.brand} to="/app/plans"><span>E</span><strong>EXPLAINORA PAY</strong></Link>
        <span>SECURE CHECKOUT</span>
      </header>

      <div className={`shell ${styles.layout}`}>
        <section className={styles.summary}>
          <span className={styles.kicker}>Plan upgrade</span>
          <h1>Upgrade to<br /><em>{plan}.</em></h1>
          <p>Your plan changes only after the payment provider confirms the transaction.</p>
          <div className={styles.order}>
            <div><span>PLAN</span><strong>{plan}</strong></div>
            <div><span>ALLOWANCE</span><strong>{details.videos} videos</strong></div>
            <div><span>BILLING</span><strong>Monthly</strong></div>
            <div className={styles.total}><span>TOTAL TODAY</span><strong>${details.price}</strong></div>
          </div>
          <Link className={styles.cancel} to="/app/plans">← Cancel and return to plans</Link>
        </section>

        <section className={styles.paymentCard} aria-labelledby="payment-title">
          <div className={styles.cardHead}><span>PAYMENT PROVIDER</span><h2 id="payment-title">Continue securely</h2><p>Card information will be collected only by the configured payment provider. Explainora never receives raw card details.</p></div>
          <div className={styles.form}>
            <div className={styles.demoNotice}><b>PROTECTED</b><span>Your plan changes only after a signed payment confirmation.</span></div>
            {error && <div aria-live="polite" className={styles.demoNotice} role="alert"><b>NOT READY</b><span>{error}</span></div>}
            <button className={styles.payButton} disabled={status !== 'ready'} onClick={() => void submit()} type="button"><span>{status === 'ready' ? `Continue · $${details.price}` : 'Opening secure checkout…'}</span><b aria-hidden="true">→</b></button>
          </div>
        </section>
      </div>
    </main>
  )
}
