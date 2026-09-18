import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  type CSSProperties,
} from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useMagnetic, useScrollProgress } from '../../hooks'
import { Button } from '../Button/Button'
import { AccountMenu } from '../AccountMenu'
import styles from './Header.module.css'

interface HeaderProps {
  jobActive: boolean
  jobScene: string
  jobProgress: number
}

type ProgressStyle = CSSProperties & {
  '--p': string
}

export function Header({ jobActive, jobScene, jobProgress }: HeaderProps) {
  const location = useLocation()
  const navigate = useNavigate()
  const { stuck, progress } = useScrollProgress()
  const brandMarkRef = useMagnetic<HTMLSpanElement>(0.35)
  const navRef = useRef<HTMLElement>(null)
  const pillRef = useRef<HTMLSpanElement>(null)
  const newVideoRef = useRef<HTMLAnchorElement>(null)
  const myVideosRef = useRef<HTMLAnchorElement>(null)
  const videosActive = location.pathname.startsWith('/app/videos')
  const accountActive = location.pathname.startsWith('/app/account')
  const activeLinkRef = videosActive ? myVideosRef : newVideoRef
  const showJob = jobActive && location.pathname !== '/app/create'
  const clampedJobProgress = Math.min(100, Math.max(0, jobProgress))

  const movePill = useCallback(() => {
    const activeLink = activeLinkRef.current
    const pill = pillRef.current
    if (!activeLink || !pill) return

    pill.style.width = `${activeLink.offsetWidth}px`
    pill.style.transform = `translateX(${activeLink.offsetLeft}px)`
  }, [activeLinkRef])

  useLayoutEffect(() => {
    const timer = window.setTimeout(movePill, 60)
    return () => window.clearTimeout(timer)
  }, [movePill, location.pathname])

  useEffect(() => {
    const nav = navRef.current
    if (!nav) return

    const observer = new ResizeObserver(movePill)
    observer.observe(nav)
    window.addEventListener('resize', movePill)

    return () => {
      observer.disconnect()
      window.removeEventListener('resize', movePill)
    }
  }, [movePill])

  const topbarClassName = [
    styles.topbar,
    stuck ? styles['is-stuck'] : '',
  ].filter(Boolean).join(' ')

  const jobPillClassName = [
    styles['job-pill'],
    showJob ? styles.on : '',
  ].filter(Boolean).join(' ')

  return (
    <header className={topbarClassName}>
      <div className={`shell ${styles['topbar-in']}`}>
        <Link className={styles.brand} to="/app">
          <span className={styles['brand-mark']} ref={brandMarkRef}>S</span>
          <span>EXPLAINORA</span>
        </Link>

        <span className={jobPillClassName}>
          <span
            className={styles['mini-ring']}
            style={{ '--p': `${clampedJobProgress}%` } as ProgressStyle}
          />
          <span className="mono">{jobScene} · {clampedJobProgress}%</span>
        </span>

        <nav className={styles.nav} ref={navRef} aria-label="Primary navigation">
          <span className={`${styles['nav-pill']} ${accountActive ? styles.hidden : ''}`} ref={pillRef} />
          <Link
            className={!videosActive && !accountActive ? styles['is-active'] : undefined}
            ref={newVideoRef}
            to="/app"
          >
            New video
          </Link>
          <Link
            className={videosActive ? styles['is-active'] : undefined}
            ref={myVideosRef}
            to="/app/videos"
          >
            My videos
          </Link>
          <AccountMenu signInClassName={styles['auth-link']} />
          <Button arrow className={styles['hdr-cta']} onClick={() => navigate('/app')}>Start explaining</Button>
        </nav>
      </div>
      <div
        className={styles['scrollbar-progress']}
        style={{ width: `${progress * 100}%` }}
      />
    </header>
  )
}
