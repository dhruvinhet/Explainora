import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ButtonLink } from '../../../components/Button/Button'
import { AccountMenu } from '../../../components/AccountMenu'
import { useMagnetic, useScrollProgress } from '../../../hooks'
import { useAuthUser } from '../../../lib/auth'
import styles from './Nav.module.css'

const SECTIONS = [
  { id: 'how', label: 'How it works' },
  { id: 'examples', label: 'Examples' },
  { id: 'features', label: 'Features' },
] as const

function ScrollProgressBar({ progress }: { progress: number }) {
  return <span className={styles.progress} style={{ transform: `scaleX(${progress})` }} aria-hidden="true" />
}

function useActiveSection() {
  const [active, setActive] = useState('')

  useEffect(() => {
    const sections = SECTIONS.map(({ id }) => document.getElementById(id)).filter(Boolean) as HTMLElement[]
    const update = () => {
      const anchor = Math.min(220, window.innerHeight * .32)
      const current = [...sections].reverse().find((section) => section.getBoundingClientRect().top <= anchor)
      setActive(current?.id ?? '')
    }
    update()
    window.addEventListener('scroll', update, { passive: true })
    window.addEventListener('resize', update)
    return () => {
      window.removeEventListener('scroll', update)
      window.removeEventListener('resize', update)
    }
  }, [])

  return active
}

function AnimatedSectionIndicator({ active }: { active: string }) {
  return (
    <nav className={styles.links} aria-label="Marketing navigation">
      {SECTIONS.map(({ id, label }) => (
        <a className={active === id ? styles.active : ''} href={`#${id}`} key={id}>{label}</a>
      ))}
    </nav>
  )
}

export function LandingNav() {
  const { progress, stuck } = useScrollProgress()
  const active = useActiveSection()
  const brandRef = useMagnetic<HTMLAnchorElement>(.16, 48)
  const ctaRef = useMagnetic<HTMLAnchorElement>(.18, 64, 1)
  const user = useAuthUser()
  const [open, setOpen] = useState(false)
  const menuRef = useRef<HTMLButtonElement>(null)
  const sheetRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const close = () => setOpen(false)
    window.addEventListener('resize', close)
    return () => window.removeEventListener('resize', close)
  }, [])

  useEffect(() => {
    if (!open) return
    const focusFirstLink = window.requestAnimationFrame(() => sheetRef.current?.querySelector<HTMLElement>('a')?.focus())
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setOpen(false)
      menuRef.current?.focus()
    }
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      window.cancelAnimationFrame(focusFirstLink)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [open])

  return (
    <header className={`${styles.nav} ${stuck ? styles.stuck : ''}`}>
      <div className={`shell ${styles.inner}`}>
        <Link className={styles.brand} ref={brandRef} to="/" aria-label="Explainora home">
          <span className={styles.mark}>E</span>
          <span>EXPLAINORA</span>
        </Link>
        <div className={styles.desktopNav}>
          <AnimatedSectionIndicator active={active} />
          <AccountMenu signInClassName={styles.authLink} />
          <ButtonLink arrow className={styles.cta} ref={ctaRef} to="/app" viewTransition>Open the studio</ButtonLink>
        </div>
        {user && <AccountMenu className={styles.mobileAccount} />}
        <button className={`${styles.menuButton} ${open ? styles.open : ''}`} aria-expanded={open} aria-controls="mobile-navigation" aria-label={open ? 'Close navigation' : 'Open navigation'} onClick={() => setOpen((value) => !value)} ref={menuRef} type="button"><i /><i /></button>
      </div>
      <div aria-hidden={!open} aria-label="Mobile navigation" className={`${styles.mobileSheet} ${open ? styles.open : ''}`} id="mobile-navigation" ref={sheetRef} role="navigation">
        {SECTIONS.map(({ id, label }, index) => <a href={`#${id}`} key={id} onClick={() => setOpen(false)} tabIndex={open ? 0 : -1}><span>0{index + 1}</span>{label}</a>)}
        {!user && <Link className={styles.mobileAuth} to="/auth" onClick={() => setOpen(false)} tabIndex={open ? 0 : -1}>Sign in / Sign up</Link>}
        <ButtonLink arrow className={styles.mobileCta} to="/app" onClick={() => setOpen(false)} tabIndex={open ? 0 : -1} viewTransition>Open the studio</ButtonLink>
      </div>
      <ScrollProgressBar progress={progress} />
    </header>
  )
}
