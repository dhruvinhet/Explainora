import { useState } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { useAuthUser, type AuthUser } from '../../lib/auth'
import styles from './Plans.module.css'

type PlanName = AuthUser['plan']

interface Plan {
  name: PlanName
  tag: string
  price: string
  period: string
  description: string
  limit: string
  reset: string
  className: string
  stamp: string
  features: string[]
}

const plans: Plan[] = [
  {
    name: 'Free',
    tag: 'Starter',
    price: '0',
    period: 'forever',
    description: 'A focused starting point for turning occasional questions into visual explanations.',
    limit: '5 videos',
    reset: 'until plan reset',
    className: styles.free,
    stamp: 'Free',
    features: ['Videos up to 2 minutes', '5 videos until plan reset', 'Standard video quality', 'Personal video library', 'Shareable exports'],
  },
  {
    name: 'Pro',
    tag: 'Popular',
    price: '19',
    period: 'per month',
    description: 'More room to create, with priority processing and richer exports for regular use.',
    limit: '10 videos',
    reset: 'until token reset',
    className: styles.pro,
    stamp: 'Popular',
    features: ['Videos up to 4 minutes', '10 videos until token reset', 'High-quality exports', 'Priority generation', 'Everything in Free'],
  },
  {
    name: 'Pro Ultra',
    tag: 'Maximum',
    price: '39',
    period: 'per month',
    description: 'The highest-capacity plan for ambitious explainers and rapid creative workflows.',
    limit: '20 videos',
    reset: 'until token reset',
    className: styles.ultra,
    stamp: 'Ultra',
    features: ['Videos up to 6 minutes', '20 videos until token reset', 'Premium video quality', 'Fastest render priority', 'Everything in Pro'],
  },
]

function CheckIcon() {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24">
      <path d="m5 12 4 4L19 6" fill="none" stroke="currentColor" strokeLinecap="square" strokeWidth="3" />
    </svg>
  )
}

function DotPattern() {
  const dots = [[10, 10], [30, 10], [50, 10], [70, 10], [20, 20], [40, 20], [60, 20], [10, 30], [30, 30], [50, 30], [70, 30]]
  return <svg aria-hidden="true" viewBox="0 0 80 40">{dots.map(([cx, cy]) => <circle cx={cx} cy={cy} fill="currentColor" key={`${cx}-${cy}`} r="3" />)}</svg>
}

export function Plans() {
  const user = useAuthUser()
  const navigate = useNavigate()
  const location = useLocation()
  const upgradedTo = (location.state as { upgradedTo?: PlanName } | null)?.upgradedTo
  const [notice, setNotice] = useState(upgradedTo ? `Payment confirmed. Your ${upgradedTo} plan is active.` : '')
  if (!user) return <Navigate replace to="/auth" />

  const choosePlan = (plan: PlanName) => {
    if (plan === user.plan) return
    const planRank: Record<PlanName, number> = { Free: 0, Pro: 1, 'Pro Ultra': 2 }
    if (planRank[plan] > planRank[user.plan]) {
      navigate(`/checkout?plan=${encodeURIComponent(plan)}`)
      return
    }
    setNotice('Plan downgrades will be available through the verified billing portal once billing is configured.')
  }

  return (
    <div className={`shell ${styles.page}`}>
      <header className={styles.heading}>
        <span className="eyebrow">Simple, flexible pricing</span>
        <h1>Choose your plan.</h1>
        <p>Pick the video allowance that fits your ideas. Prices are placeholders while billing is being connected.</p>
      </header>

      {upgradedTo && <div className={styles.successBanner} role="status"><span>✓</span><div><strong>Upgrade complete</strong><p>Your payment was confirmed and {upgradedTo} is now active across Explainora.</p></div></div>}

      <div aria-label="Available plans" className={styles.plansGrid}>
        {plans.map((plan) => {
          const current = user.plan === plan.name
          return (
            <article className={`${styles.card} ${plan.className} ${current ? styles.current : ''}`} key={plan.name}>
              <div aria-hidden="true" className={styles.cardPatternGrid} />
              <div aria-hidden="true" className={styles.cardOverlayDots} />
              <div aria-hidden="true" className={styles.boldPattern}>
                <svg viewBox="0 0 100 100"><path d="M0,0 L100,0 L100,100 L0,100 Z" fill="none" stroke="currentColor" strokeDasharray="15 10" strokeWidth="10" /></svg>
              </div>

              <div className={styles.cardTitleArea}>
                <span>{plan.name}</span>
                <span className={styles.cardTag}>{current ? 'Current' : plan.tag}</span>
              </div>

              <div className={styles.cardBody}>
                <p className={styles.cardDescription}>{plan.description}</p>
                <div className={styles.allowance}><strong>{plan.limit}</strong><span>{plan.reset}</span></div>
                <div className={styles.featureGrid}>
                  {plan.features.map((feature) => <div className={styles.featureItem} key={feature}><span className={styles.featureIcon}><CheckIcon /></span><span>{feature}</span></div>)}
                </div>
                <div className={styles.cardActions}>
                  <div className={styles.price}><span>$</span>{plan.price}<small>{plan.period}</small></div>
                  <button aria-pressed={current} className={styles.cardButton} disabled={current} onClick={() => choosePlan(plan.name)} type="button">{current ? 'Current plan' : `Choose ${plan.name}`}</button>
                </div>
              </div>

              <div aria-hidden="true" className={styles.dotsPattern}><DotPattern /></div>
              <div aria-hidden="true" className={styles.accentShape} />
              <div aria-hidden="true" className={styles.cornerSlice} />
              <div aria-hidden="true" className={styles.stamp}>{plan.stamp}</div>
            </article>
          )
        })}
      </div>

      <div aria-live="polite" className={styles.footerRow}>
        <Link to="/app/account">← Back to account</Link>
        <span>{notice || 'Cancel or change your plan at any time.'}</span>
      </div>
    </div>
  )
}
