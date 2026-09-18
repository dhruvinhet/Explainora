import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { clearAuthUser, useAuthUser } from '../../lib/auth'
import styles from './AccountMenu.module.css'

interface AccountMenuProps {
  className?: string
  signInClassName?: string
}

function initials(name: string) {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]).join('').toUpperCase() || 'S'
}

export function AccountMenu({ className, signInClassName }: AccountMenuProps) {
  const user = useAuthUser()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const closeOutside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', closeOutside)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('pointerdown', closeOutside)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [open])

  if (!user) return <Link className={signInClassName} to="/auth">Sign in</Link>

  const logout = async () => {
    await clearAuthUser()
    setOpen(false)
    navigate('/')
  }

  return (
    <div className={[styles.root, className].filter(Boolean).join(' ')} ref={rootRef}>
      <button aria-expanded={open} aria-haspopup="menu" aria-label={`Open account menu for ${user.name}`} className={styles.trigger} onClick={() => setOpen((value) => !value)} type="button">
        <span>{initials(user.name)}</span>
        <i aria-hidden="true">⌄</i>
      </button>
      <div aria-hidden={!open} className={`${styles.menu} ${open ? styles.open : ''}`} role="menu">
        <div className={styles.identity}><span>{initials(user.name)}</span><div><strong>{user.name}</strong><small>{user.email}</small></div></div>
        <div className={styles.plan}><span>Current plan</span><b>{user.plan}</b></div>
        <Link onClick={() => setOpen(false)} role="menuitem" to="/app/account"><span>My account</span><b aria-hidden="true">→</b></Link>
        <button onClick={() => void logout()} role="menuitem" type="button"><span>Log out</span><b aria-hidden="true">↗</b></button>
      </div>
    </div>
  )
}
