import { useState, type FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { signIn, signUp } from '../../lib/auth'
import styles from './Auth.module.css'

export function Auth() {
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const mode = searchParams.get('mode') === 'signup' ? 'signup' : 'signin'
  const signingUp = mode === 'signup'
  const [showPassword, setShowPassword] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const notice = searchParams.get('reset') === 'success'
    ? 'Password reset complete. Sign in with your new password.'
    : searchParams.get('password') === 'changed'
      ? 'Password changed. Sign in again to continue.'
      : searchParams.get('account') === 'deleted'
        ? 'Your account and private data were permanently deleted.'
        : ''

  const switchMode = (next: 'signin' | 'signup') => {
    setSearchParams(next === 'signup' ? { mode: 'signup' } : {}, { replace: true })
    setSubmitting(false)
  }

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    const email = String(data.get('email') || '').trim()
    const password = String(data.get('password') || '')
    const confirmation = String(data.get('confirmPassword') || '')
    if (signingUp && password !== confirmation) {
      setError('Passwords need to match.')
      return
    }
    const enteredName = String(data.get('name') || '').trim()
    const fallbackName = email.split('@')[0].replace(/[._-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()) || 'Explainora Creator'
    setError('')
    setSubmitting(true)
    try {
      const result = signingUp
        ? await signUp(enteredName || fallbackName, email, password)
        : await signIn(email, password)
      if (result.needsEmailConfirmation) {
        setError('Account created. Check your email to confirm it, then sign in.')
        setSubmitting(false)
        return
      }
      const returnTo = searchParams.get('returnTo')
      const safeReturnTo = returnTo?.startsWith('/app') ? returnTo : '/app'
      window.setTimeout(() => navigate(safeReturnTo), 420)
    } catch (authError) {
      setError(authError instanceof Error ? authError.message : 'Authentication failed. Please try again.')
      setSubmitting(false)
    }
  }

  return (
    <main className={styles.page}>
      <div className={styles.grid} aria-hidden="true" />
      <header className={styles.header}>
        <Link className={styles.brand} to="/" aria-label="Explainora home"><span>E</span><strong>EXPLAINORA</strong></Link>
        <Link className={styles.back} to="/">← Back to home</Link>
      </header>

      <div className={`shell ${styles.layout}`}>
        <section className={styles.intro}>
          <span className={styles.kicker}>{signingUp ? 'Start making' : 'Welcome back'}</span>
          <h1>{signingUp ? <>Ideas in.<br /><em>Videos out.</em></> : <>Pick up where<br /><em>you left off.</em></>}</h1>
          <p>{signingUp ? 'Create visual explanations from any question, with every scene planned, narrated, and rendered for you.' : 'Your videos, active renders, and next big questions are waiting in the studio.'}</p>
          <div className={styles.proof}>
            <span><b>2.8k+</b> explanations</span>
            <i />
            <span><b>~6 min</b> average render</span>
          </div>
        </section>

        <section className={styles.card} aria-labelledby="auth-title">
          <div className={styles.tabs} aria-label="Authentication mode">
            <button aria-pressed={!signingUp} onClick={() => switchMode('signin')} type="button">Sign in</button>
            <button aria-pressed={signingUp} onClick={() => switchMode('signup')} type="button">Sign up</button>
          </div>
          <div className={styles.cardHead}>
            <span>SECURE STUDIO ACCESS</span>
            <h2 id="auth-title">{signingUp ? 'Create your account' : 'Sign in to Explainora'}</h2>
            <p>{signingUp ? 'One account. Every explanation.' : 'Continue creating clear visual stories.'}</p>
          </div>

          <form className={styles.form} onSubmit={submit}>
            {signingUp && <label><span>Your name</span><input autoComplete="name" name="name" placeholder="Ada Lovelace" required /></label>}
            <label><span>Email address</span><input autoComplete="email" name="email" placeholder="you@example.com" required type="email" /></label>
            <label><span>Password</span><span className={styles.password}><input autoComplete={signingUp ? 'new-password' : 'current-password'} minLength={8} name="password" placeholder="At least 8 characters" required type={showPassword ? 'text' : 'password'} /><button aria-label={showPassword ? 'Hide password' : 'Show password'} onClick={() => setShowPassword((value) => !value)} type="button">{showPassword ? 'HIDE' : 'SHOW'}</button></span></label>
            {signingUp && <label><span>Confirm password</span><input autoComplete="new-password" minLength={8} name="confirmPassword" placeholder="Repeat your password" required type={showPassword ? 'text' : 'password'} /></label>}

            <div className={styles.options}>
              <label className={styles.check}><input required={signingUp} type="checkbox" /><span>{signingUp ? <>I agree to the <Link rel="noreferrer" target="_blank" to="/terms">Terms</Link> and <Link rel="noreferrer" target="_blank" to="/privacy">Privacy Policy</Link></> : 'Keep me signed in'}</span></label>
              {!signingUp && <Link to="/auth/forgot">Forgot password?</Link>}
            </div>
            {notice && <p className={styles.success} role="status">{notice}</p>}
            {error && <p className={styles.error} role="alert">{error}</p>}
            <button className={styles.submit} disabled={submitting} type="submit"><span>{submitting ? 'OPENING STUDIO…' : signingUp ? 'CREATE ACCOUNT' : 'SIGN IN'}</span><b aria-hidden="true">→</b></button>
          </form>

          <p className={styles.switchCopy}>{signingUp ? 'Already have an account?' : 'New to Explainora?'} <button onClick={() => switchMode(signingUp ? 'signin' : 'signup')} type="button">{signingUp ? 'Sign in' : 'Create an account'}</button></p>
        </section>
      </div>
    </main>
  )
}
