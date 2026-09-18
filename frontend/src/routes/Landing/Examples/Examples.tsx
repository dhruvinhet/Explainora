import { type CSSProperties, type MouseEvent, type PointerEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useReveal, useSpotlight } from '../../../hooks'
import { DemoArt, type DemoKind } from '../Hero/Hero'
import styles from '../Sections.module.css'

const EXAMPLES: ReadonlyArray<{ title: string; category: string; duration: string; kind: DemoKind }> = [
  { title: 'How do F1 cars corner so fast?', category: 'Engineering', duration: '1:32', kind: 'car' },
  { title: 'How does a compiler understand code?', category: 'Computer science', duration: '2:11', kind: 'compiler' },
  { title: "Why doesn't the Moon fall to Earth?", category: 'Science', duration: '2:05', kind: 'moon' },
]

export function LandingExamples() {
  const reveal = useReveal<HTMLDivElement>()
  return (
    <section className={styles.section} id="examples">
      <div className="shell">
        <div className={styles.sectionHead}>
          <div className="eyebrow">Examples</div>
          <h2>See ideas become explanations</h2>
          <p>Three questions, shaped into clear visual stories.</p>
        </div>
        <div className={`${styles.exampleGrid} ${styles.reveal} ${reveal.className}`} ref={reveal.ref}>
          <ExampleCard {...EXAMPLES[0]} big to="/app" />
          <div className={styles.exampleSide}>
            <ExampleCard {...EXAMPLES[1]} to="/app" />
            <ExampleCard {...EXAMPLES[2]} to="/app" />
          </div>
        </div>
      </div>
    </section>
  )
}

function ExampleCard({ title, category, duration, kind, big = false, to }: typeof EXAMPLES[number] & { big?: boolean; to: string }) {
  const spotlight = useSpotlight<HTMLAnchorElement>()
  const navigate = useNavigate()
  const transitionName = `video-${kind}`

  const playPreview = (event: PointerEvent<HTMLAnchorElement>) => {
    if (!window.matchMedia('(hover:hover) and (prefers-reduced-motion:no-preference)').matches) return
    event.currentTarget.querySelector('video')?.play().catch(() => undefined)
  }

  const stopPreview = (event: PointerEvent<HTMLAnchorElement>) => {
    event.currentTarget.style.transform = ''
    const video = event.currentTarget.querySelector('video')
    video?.pause()
    if (video) video.currentTime = 0
  }

  const tilt = (event: PointerEvent<HTMLAnchorElement>) => {
    if (!window.matchMedia('(hover:hover) and (prefers-reduced-motion:no-preference)').matches) return
    const bounds = event.currentTarget.getBoundingClientRect()
    const x = (event.clientX - bounds.left) / bounds.width - .5
    const y = (event.clientY - bounds.top) / bounds.height - .5
    event.currentTarget.style.transform = `rotateX(${-y * 3}deg) rotateY(${x * 3}deg)`
  }

  const openWithTransition = (event: MouseEvent<HTMLAnchorElement>) => {
    const transitionDocument = document as Document & { startViewTransition?: (update: () => void) => void }
    if (!transitionDocument.startViewTransition) return
    event.preventDefault()
    transitionDocument.startViewTransition(() => navigate(to))
  }

  return (
    <Link className={`${styles.exampleCard} ${big ? styles.exampleBig : ''}`} onClick={openWithTransition} onPointerEnter={playPreview} onPointerLeave={stopPreview} onPointerMove={tilt} ref={spotlight} style={{ viewTransitionName: transitionName } as CSSProperties} to={to}>
      <span className={styles.exampleMedia}>
        <DemoArt kind={kind} />
        <span className={styles.play}>▶</span>
        <span className={styles.duration}>{duration}</span>
      </span>
      <span className={styles.exampleBody}>
        <span className={styles.category}>{category}</span>
        <strong>{title}</strong>
      </span>
      <span className={styles.cardButton}>Watch video</span>
    </Link>
  )
}
