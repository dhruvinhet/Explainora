import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { auditPasswordResetRequest, recordPasswordResetComplete } from '../../lib/api'
import {
  clearAuthUser,
  completePasswordRecovery,
  requestPasswordReset,
  usePasswordRecoveryReady,
} from '../../lib/auth'
import styles from './Auth.module.css'

function RecoveryShell({ children }: { children: React.ReactNode }) {
  return <main className={styles.page}>
    <div className={styles.grid} aria-hidden="true" />
    <header className={styles.header}>
      <Link className={styles.brand} to="/"><span>E</span><strong>EXPLAINORA</strong></Link>
      <Link className={styles.back} to="/auth">← Back to sign in</Link>
    </header>
    <div className={`shell ${styles.recoveryLayout}`}>{children}</div>
  </main>
}

export function ForgotPassword() {
  const [submitting, setSubmitting] = useState(false)
  const [message, setMessage] = useState('')

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const email = String(new FormData(event.currentTarget).get('email') || '').trim()
    setSubmitting(true)
    setMessage('')
    try {
      await auditPasswordResetRequest(email)
      await requestPasswordReset(email)
      setMessage('If an account exists for that email, a secure recovery link is on its way.')
      event.currentTarget.reset()
    } catch {
      setMessage('If an account exists for that email, a secure recovery link is on its way.')
    } finally {
      setSubmitting(false)
    }
  }

  return <RecoveryShell><section className={styles.card} aria-labelledby="forgot-title">
    <div className={styles.cardHead}><span>ACCOUNT RECOVERY</span><h1 id="forgot-title">Reset your password</h1><p>We will send a time-limited link to your account email.</p></div>
    <form className={styles.form} onSubmit={submit}>
      <label><span>Email address</span><input autoComplete="email" name="email" required type="email" /></label>
      {message && <p className={styles.success} role="status">{message}</p>}
      <button className={styles.submit} disabled={submitting} type="submit"><span>{submitting ? 'SENDING…' : 'SEND RECOVERY LINK'}</span><b>→</b></button>
    </form>
  </section></RecoveryShell>
}

export function ResetPassword() {
  const ready = usePasswordRecoveryReady()
  const navigate = useNavigate()
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    const password = String(data.get('password') || '')
    const confirmation = String(data.get('confirmation') || '')
    if (password !== confirmation) return setError('Passwords need to match.')
    setError('')
    setSubmitting(true)
    try {
      await completePasswordRecovery(password)
      await recordPasswordResetComplete().catch(() => undefined)
      await clearAuthUser()
      navigate('/auth?reset=success', { replace: true })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The password could not be reset.')
      setSubmitting(false)
    }
  }

  return <RecoveryShell><section className={styles.card} aria-labelledby="reset-title">
    <div className={styles.cardHead}><span>SECURE CALLBACK</span><h1 id="reset-title">Choose a new password</h1><p>The recovery session is valid only for this password change.</p></div>
    {!ready ? <div className={styles.invalid}><p>This recovery link is invalid or has expired.</p><Link to="/auth/forgot">Request a new link →</Link></div> :
      <form className={styles.form} onSubmit={submit}>
        <label><span>New password</span><input autoComplete="new-password" minLength={8} name="password" required type="password" /></label>
        <label><span>Confirm new password</span><input autoComplete="new-password" minLength={8} name="confirmation" required type="password" /></label>
        {error && <p className={styles.error} role="alert">{error}</p>}
        <button className={styles.submit} disabled={submitting} type="submit"><span>{submitting ? 'UPDATING…' : 'SET NEW PASSWORD'}</span><b>→</b></button>
      </form>}
  </section></RecoveryShell>
}
