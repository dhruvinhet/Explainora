import { useEffect, useRef, useState, type CSSProperties, type PointerEvent } from 'react'
import { useCountUp, useMagnetic } from '../../../hooks'
import { Button, ButtonLink } from '../../../components/Button/Button'
import styles from './Hero.module.css'

type DemoPhase = 'typing' | 'generating' | 'ready'
export type DemoKind = 'car' | 'compiler' | 'moon'
type DemoStyle = CSSProperties & { '--fill'?: number; '--px'?: string; '--py'?: string }

const DEMOS: ReadonlyArray<{ question: string; kind: DemoKind; duration: string }> = [
  { question: 'How do F1 cars corner so fast?', kind: 'car', duration: '1:32' },
  { question: 'How does a compiler understand code?', kind: 'compiler', duration: '2:11' },
  { question: "Why doesn't the Moon fall to Earth?", kind: 'moon', duration: '2:05' },
]

export function DemoArt({ kind }: { kind: DemoKind }) {
  if (kind === 'compiler') return (
    <svg viewBox="0 0 360 200" aria-hidden="true">
      <g fontFamily="Inter" fill="none" strokeLinecap="round">
        <rect x="26" y="66" width="92" height="62" rx="9" fill="#f1f1ed" stroke="#d2d2cc" />
        <rect x="144" y="42" width="72" height="34" rx="8" fill="#edf1fc" stroke="#315fda" />
        <rect x="144" y="86" width="72" height="34" rx="8" fill="#edf1fc" stroke="#315fda" />
        <rect x="244" y="66" width="90" height="62" rx="9" fill="#fff" stroke="#151515" />
        <path d="M118 97h20m78 0h22M289 79v18m0 0-20 17m20-17 20 17" stroke="#151515" strokeWidth="1.6" />
        <text x="72" y="102" textAnchor="middle" fill="#151515" fontSize="13" fontFamily="JetBrains Mono">x = a+b</text>
        <text x="180" y="63" textAnchor="middle" fill="#315fda" fontSize="11">TOKENS</text>
        <text x="180" y="107" textAnchor="middle" fill="#315fda" fontSize="11">PARSER</text>
        <text x="289" y="151" textAnchor="middle" fill="#6f6f6a" fontSize="11">syntax tree</text>
      </g>
    </svg>
  )
  if (kind === 'moon') return (
    <svg viewBox="0 0 360 200" aria-hidden="true">
      <ellipse cx="174" cy="102" rx="132" ry="66" fill="none" stroke="#d2d2cc" strokeDasharray="6 7" />
      <circle cx="174" cy="102" r="43" fill="#edf1fc" stroke="#151515" strokeWidth="2" />
      <circle cx="306" cy="102" r="14" fill="#fff" stroke="#151515" strokeWidth="2" />
      <path d="M290 102h-28m8-7-8 7 8 7M306 83V59m-7 9 7-9 7 9" fill="none" stroke="#315fda" strokeWidth="2" strokeLinecap="round" />
      <text x="174" y="107" textAnchor="middle" fill="#151515" fontFamily="Inter" fontSize="13" fontWeight="700">Earth</text>
      <text x="254" y="121" textAnchor="middle" fill="#6f6f6a" fontFamily="Inter" fontSize="11">gravity</text>
    </svg>
  )
  return (
    <svg viewBox="0 0 360 200" aria-hidden="true">
      <path d="M24 146h312" stroke="#d2d2cc" strokeWidth="2" />
      <path d="M68 135c0-13 8-21 21-21h22l14-28h56l12 28h58l21-10h30v31z" fill="#edf1fc" stroke="#151515" strokeWidth="2.5" strokeLinejoin="round" />
      <circle cx="111" cy="135" r="19" fill="#fff" stroke="#151515" strokeWidth="2.5" /><circle cx="111" cy="135" r="6" fill="#151515" />
      <circle cx="267" cy="135" r="19" fill="#fff" stroke="#151515" strokeWidth="2.5" /><circle cx="267" cy="135" r="6" fill="#151515" />
      <g fill="none" stroke="#315fda" strokeWidth="2" strokeLinecap="round" strokeDasharray="15 9">
        <path d="M18 62h55c30 0 38-16 66-16" /><path d="M18 82h50c26 0 34-11 58-11" /><path d="M18 103h44" />
      </g>
      <path d="M182 30v39m-8-11 8 11 8-11" fill="none" stroke="#151515" strokeWidth="2.3" strokeLinecap="round" />
      <text x="182" y="22" textAnchor="middle" fill="#151515" fontFamily="Inter" fontSize="12" fontWeight="700">downforce</text>
    </svg>
  )
}

export function LandingHero() {
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  const [demoIndex, setDemoIndex] = useState(reduced ? DEMOS.length - 1 : 0)
  const [phase, setPhase] = useState<DemoPhase>(reduced ? 'ready' : 'typing')
  const [typed, setTyped] = useState(reduced ? DEMOS[DEMOS.length - 1].question : '')
  const [generation, setGeneration] = useState(reduced ? 100 : 0)
  const publicDemoUrl = String(import.meta.env.VITE_PUBLIC_DEMO_URL || '').trim()
  const publicDemoPoster = String(import.meta.env.VITE_PUBLIC_DEMO_POSTER_URL || '').trim()
  const publicDemoTitle = String(import.meta.env.VITE_PUBLIC_DEMO_TITLE || '').trim()
  const demoVideo = publicDemoUrl ? {
    poster: publicDemoPoster || undefined,
    title: publicDemoTitle || 'How binary search finds an answer',
    url: publicDemoUrl,
  } : null
  const heroRef = useRef<HTMLElement>(null)
  const demoRef = useRef<HTMLDivElement>(null)
  const dialogRef = useRef<HTMLDialogElement>(null)
  const videoRef = useRef<HTMLVideoElement>(null)
  const primaryRef = useMagnetic<HTMLAnchorElement>(.18, 72, 1)
  const count = useCountUp(2847)
  const demo = DEMOS[demoIndex]

  const restartDemo = () => {
    setTyped('')
    setGeneration(0)
    setPhase(reduced ? 'ready' : 'typing')
  }

  const openVideoDemo = () => {
    if (!demoVideo) return
    dialogRef.current?.showModal()
    // Keep play() in the original click activation. Deferring it to an
    // animation frame causes stricter mobile browsers to reject playback.
    videoRef.current?.play().catch(() => undefined)
  }

  const closeVideoDemo = () => {
    videoRef.current?.pause()
    if (videoRef.current) videoRef.current.currentTime = 0
    dialogRef.current?.close()
  }

  useEffect(() => {
    if (reduced) return
    if (phase === 'typing') {
      if (typed.length < demo.question.length) {
        const timer = window.setTimeout(() => setTyped(demo.question.slice(0, typed.length + 1)), 38)
        return () => window.clearTimeout(timer)
      }
      const timer = window.setTimeout(() => { setGeneration(3); setPhase('generating') }, 220)
      return () => window.clearTimeout(timer)
    }
    if (phase === 'generating') {
      if (generation < 100) {
        const timer = window.setTimeout(() => setGeneration((value) => Math.min(100, value + 4)), 38)
        return () => window.clearTimeout(timer)
      }
      const timer = window.setTimeout(() => setPhase('ready'), 180)
      return () => window.clearTimeout(timer)
    }
    const timer = window.setTimeout(() => {
      setDemoIndex((value) => (value + 1) % DEMOS.length)
      setTyped('')
      setGeneration(0)
      setPhase('typing')
    }, 1500)
    return () => window.clearTimeout(timer)
  }, [demo.question, generation, phase, reduced, typed.length])

  const moveGrid = (event: PointerEvent<HTMLElement>) => {
    if (reduced) return
    const bounds = event.currentTarget.getBoundingClientRect()
    const x = ((event.clientX - bounds.left) / bounds.width - .5) * 12
    const y = ((event.clientY - bounds.top) / bounds.height - .5) * 12
    heroRef.current?.style.setProperty('--px', `${x}px`)
    heroRef.current?.style.setProperty('--py', `${y}px`)
  }

  return (
    <section className={styles.hero} onPointerMove={moveGrid} ref={heroRef} style={{ '--px': '0px', '--py': '0px' } as DemoStyle}>
      <div className={styles.grid} aria-hidden="true" />
      <div className={`shell ${styles.layout}`}>
        <div className={styles.copy}>
          <div className="eyebrow">AI visual explanations</div>
          <h1>Turn any question into a <span>video.</span></h1>
          <p>Ask a question. Explainora plans the explanation, creates every visual, narrates each scene, and gives you a complete video in minutes.</p>
          <div className={styles.actions}>
            <div className={styles.primaryGroup}>
              <ButtonLink arrow className={styles.primary} ref={primaryRef} to="/app" viewTransition>Start explaining</ButtonLink>
              <small>~6 min · no editing</small>
            </div>
            <Button className={styles.secondary} disabled={!demoVideo} onClick={openVideoDemo} variant="ghost">▶ <span>{demoVideo ? 'Watch a 30s demo' : 'Demo coming soon'}</span></Button>
          </div>
          <div className={styles.trust}>
            <span className={styles.heroAvatars} aria-label="Used by thousands of curious learners"><b>AK</b><b>MJ</b><b>RS</b></span>
            <span className={styles.trustCopy}><strong className="num">{count}</strong> explanations <i /> 9 scenes avg <i /> 4.9/5 clarity</span>
          </div>
        </div>

        <div className={`${styles.demoWrap} ${phase === 'generating' ? styles.thinkingWrap : ''}`} id="hero-demo" ref={demoRef}>
          <span className={`hand ${styles.scribble}`}>watch it think ✎</span>
          <div className={`${styles.demo} ${phase === 'generating' ? styles.thinking : ''}`}>
            <div className={styles.chrome}>
              <span className={styles.dots}><i /><i /><i /></span>
              <span className={styles.address}>explainora.dev</span>
              <span className={`${styles.status} ${phase === 'ready' ? styles.ready : ''}`}><i />{phase === 'ready' ? 'ready' : 'generating'}</span>
            </div>
            <div className={styles.ask}><b>ASK</b><span>{typed}</span>{phase === 'typing' && <i className={styles.caret} />}</div>
            <div className={styles.morph}>
              <div className={styles.generating} data-active={phase === 'generating'} aria-hidden={phase !== 'generating'}>
                <span className={styles.genRing} style={{ '--fill': generation } as DemoStyle}><b className="num">{generation}%</b></span>
                <small>Building the visual explanation…</small>
              </div>
              <div className={styles.scene} data-active={phase === 'ready'} aria-hidden={phase !== 'ready'}>
                <DemoArt kind={demo.kind} />
                <button aria-label="Play sample explanation" className={styles.play} disabled={!demoVideo} onClick={openVideoDemo} type="button">▶</button>
                <span className={styles.duration}>{demo.duration}</span>
                {phase === 'ready' && <svg className={styles.cursor} viewBox="0 0 24 28" aria-hidden="true"><path d="M3 2v20l5.4-5.1 4.2 8.5 4-2-4.1-8.1H20z" /></svg>}
              </div>
            </div>
            <div className={styles.progress} aria-hidden="true">
              {DEMOS.map((item, index) => <i key={item.question} style={{ '--fill': index < demoIndex ? 1 : index === demoIndex ? (phase === 'ready' ? 1 : phase === 'generating' ? generation / 100 : .08) : 0 } as DemoStyle} />)}
            </div>
            <button className={styles.restart} onClick={restartDemo} type="button">↻ Restart demo</button>
          </div>
        </div>
      </div>
      <a className={styles.scrollCue} href="#how"><span>Scroll to explore</span><i aria-hidden="true">↓</i></a>
      <dialog aria-labelledby="demo-video-title" className={styles.lightbox} onClick={(event) => { if (event.target === event.currentTarget) closeVideoDemo() }} onClose={() => { videoRef.current?.pause(); if (videoRef.current) videoRef.current.currentTime = 0 }} ref={dialogRef}>
        <div className={styles.lightboxCard}>
          <div className={styles.lightboxHead}><div><span className="eyebrow">30 second demo</span><strong id="demo-video-title">{demoVideo?.title || 'Explainora explanation'}</strong></div><button aria-label="Close demo" onClick={closeVideoDemo} type="button">×</button></div>
          {demoVideo && <video controls onTimeUpdate={(event) => { if (event.currentTarget.currentTime >= 30) event.currentTarget.pause() }} poster={demoVideo.poster} preload="metadata" ref={videoRef} src={demoVideo.url}>Your browser does not support video playback.</video>}
          <div className={styles.lightboxFoot}><span><i />Real video generated by Explainora</span><ButtonLink arrow to="/app" viewTransition>Make your own</ButtonLink></div>
        </div>
      </dialog>
    </section>
  )
}
