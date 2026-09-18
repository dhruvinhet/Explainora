import { useState, type FormEvent } from 'react'
import { Link, Navigate, useNavigate } from 'react-router-dom'
import { changeAccountPassword, deleteAccount } from '../../lib/api'
import { clearAuthUser, clearDeletedAuthUser, useAuthUser } from '../../lib/auth'
import styles from './Account.module.css'

function initials(name: string) {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]).join('').toUpperCase() || 'S'
}

export function Account() {
  const user = useAuthUser()
  const navigate = useNavigate()
  const [passwordMessage, setPasswordMessage] = useState('')
  const [passwordError, setPasswordError] = useState('')
  const [changingPassword, setChangingPassword] = useState(false)
  const [confirmingDeletion, setConfirmingDeletion] = useState(false)
  const [deletionError, setDeletionError] = useState('')
  const [deleting, setDeleting] = useState(false)
  if (!user) return <Navigate replace to="/auth" />

  const submitPassword = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const form = event.currentTarget
    const data = new FormData(form)
    setPasswordError('')
    setPasswordMessage('')
    setChangingPassword(true)
    try {
      await changeAccountPassword(
        String(data.get('currentPassword') || ''),
        String(data.get('newPassword') || ''),
        String(data.get('newPasswordConfirmation') || ''),
      )
      setPasswordMessage('Password changed. Sign in again with your new password.')
      form.reset()
      window.setTimeout(() => void clearAuthUser().finally(() => navigate('/auth?password=changed', { replace: true })), 900)
    } catch (reason) {
      setPasswordError(reason instanceof Error ? reason.message : 'Password could not be changed.')
      setChangingPassword(false)
    }
  }

  const submitDeletion = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    setDeletionError('')
    setDeleting(true)
    try {
      await deleteAccount(
        String(data.get('deletePassword') || ''),
        String(data.get('deleteConfirmation') || ''),
      )
      await clearDeletedAuthUser()
      navigate('/auth?account=deleted', { replace: true })
    } catch (reason) {
      setDeletionError(reason instanceof Error ? reason.message : 'Account deletion could not be completed.')
      setDeleting(false)
    }
  }

  const joined = new Intl.DateTimeFormat('en', { month: 'long', year: 'numeric' }).format(new Date(user.joinedAt))
  const planDescription = user.plan === 'Pro Ultra'
    ? 'Maximum video capacity, premium exports, and the fastest rendering priority.'
    : user.plan === 'Pro'
      ? 'More videos, advanced exports, and priority rendering for regular creators.'
      : 'Everything you need to create and keep clear visual explanations.'

  return (
    <div className={`shell ${styles.page}`}>
      <div className={styles.heading}><span className="eyebrow">Account settings</span><h1>My account</h1><p>Profile details, membership, and plan information in one place.</p></div>
      <div className={styles.grid}>
        <section className={styles.profile} aria-labelledby="profile-title">
          <div className={styles.profileHead}><span>{initials(user.name)}</span><div><small>PROFILE</small><h2 id="profile-title">{user.name}</h2><p>{user.email}</p></div></div>
          <dl><div><dt>Full name</dt><dd>{user.name}</dd></div><div><dt>Email address</dt><dd>{user.email}</dd></div><div><dt>Member since</dt><dd>{joined}</dd></div></dl>
          <p className={styles.note}>Profile changes will sync here when account services are connected.</p>
        </section>

        <section className={styles.planCard} aria-labelledby="plan-title">
          <div className={styles.planTop}><div><small>CURRENT PLAN</small><h2 id="plan-title">{user.plan}</h2></div><span>ACTIVE</span></div>
          <p>{planDescription}</p>
          <ul><li><i>✓</i>Scene-by-scene generation</li><li><i>✓</i>Personal video library</li><li><i>✓</i>MP4 export and sharing</li></ul>
          <Link className={styles.upgrade} to="/app/plans"><span>Explore plans</span><b>→</b></Link>
        </section>
      </div>
      <section className={styles.security} aria-labelledby="security-title">
        <div className={styles.securityHead}><div><small>SECURITY</small><h2 id="security-title">Password & account</h2></div><span>REAUTHENTICATION REQUIRED</span></div>
        <div className={styles.securityGrid}>
          <form className={styles.securityForm} onSubmit={submitPassword}>
            <h3>Change password</h3>
            <p>Confirm your current password before setting a new one. You will be signed out afterward.</p>
            <label><span>Current password</span><input autoComplete="current-password" name="currentPassword" required type="password" /></label>
            <label><span>New password</span><input autoComplete="new-password" minLength={8} name="newPassword" required type="password" /></label>
            <label><span>Confirm new password</span><input autoComplete="new-password" minLength={8} name="newPasswordConfirmation" required type="password" /></label>
            {passwordError && <p className={styles.formError} role="alert">{passwordError}</p>}
            {passwordMessage && <p className={styles.formSuccess} role="status">{passwordMessage}</p>}
            <button disabled={changingPassword} type="submit">{changingPassword ? 'UPDATING…' : 'CHANGE PASSWORD'}</button>
          </form>

          <div className={styles.dangerZone}>
            <h3>Delete account</h3>
            <p>Permanently removes your profile, plan history, source files, videos, and private media. This cannot be undone.</p>
            {!confirmingDeletion ? <button className={styles.dangerButton} onClick={() => setConfirmingDeletion(true)} type="button">DELETE MY ACCOUNT</button> :
              <form className={styles.securityForm} onSubmit={submitDeletion}>
                <label><span>Current password</span><input autoComplete="current-password" name="deletePassword" required type="password" /></label>
                <label><span>Type DELETE MY ACCOUNT</span><input autoComplete="off" name="deleteConfirmation" pattern="DELETE MY ACCOUNT" required /></label>
                <label className={styles.irreversible}><input required type="checkbox" /><span>I understand that this action is permanent.</span></label>
                {deletionError && <p className={styles.formError} role="alert">{deletionError}</p>}
                <div className={styles.confirmActions}><button className={styles.dangerButton} disabled={deleting} type="submit">{deleting ? 'DELETING…' : 'PERMANENTLY DELETE'}</button><button onClick={() => { setConfirmingDeletion(false); setDeletionError('') }} type="button">CANCEL</button></div>
              </form>}
          </div>
        </div>
      </section>
      <div className={styles.actions}><Link to="/app">← Back to studio</Link><Link to="/app/videos">View my videos →</Link></div>
    </div>
  )
}
