import {
  type KeyboardEvent,
  type MutableRefObject,
  type PointerEvent,
  type RefObject,
} from 'react'
import { useNavigate } from 'react-router-dom'
import { useReveal, useSpotlight } from '../../../hooks'
import { ArtMarkup } from './ArtMarkup'
import type { ArtKey } from '../../../assets/art'
import styles from '../Home.module.css'

interface ExampleCardProps {
  artKey: ArtKey
  category: string
  duration: string
  meta: string
  title: string
  big?: boolean
  delay: number
}

function mergeRefs<T>(...refs: RefObject<T>[]) {
  return (element: T | null) => {
    refs.forEach((ref) => {
      ;(ref as MutableRefObject<T | null>).current = element
    })
  }
}

function ExampleCard({
  artKey,
  category,
  duration,
  meta,
  title,
  big = false,
  delay,
}: ExampleCardProps) {
  const navigate = useNavigate()
  const reveal = useReveal<HTMLElement>()
  const spotlightRef = useSpotlight<HTMLElement>()

  const open = () => navigate('/app/videos')

  const handlePointerMove = (event: PointerEvent<HTMLElement>) => {
    if (!window.matchMedia('(hover: hover) and (prefers-reduced-motion: no-preference)').matches) return
    const bounds = event.currentTarget.getBoundingClientRect()
    const x = (event.clientX - bounds.left) / bounds.width - 0.5
    const y = (event.clientY - bounds.top) / bounds.height - 0.5
    event.currentTarget.style.transform = `translateY(-4px) rotateX(${-y * 6}deg) rotateY(${x * 6}deg)`
  }

  const handlePointerLeave = (event: PointerEvent<HTMLElement>) => {
    event.currentTarget.style.transform = ''
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      open()
    }
  }

  return (
    <article
      className={`${styles.vcard} ${big ? styles.big : ''} ${styles.reveal} ${reveal.className}`}
      onClick={open}
      onKeyDown={handleKeyDown}
      onPointerLeave={handlePointerLeave}
      onPointerMove={handlePointerMove}
      ref={mergeRefs(reveal.ref, spotlightRef)}
      role="link"
      style={{ transitionDelay: `${delay}ms` }}
      tabIndex={0}
    >
      <div className={styles.media}>
        <ArtMarkup artKey={artKey} />
        <span
          className={styles['play-btn']}
          style={big ? undefined : { width: 40, height: 40 }}
        >▶</span>
        <span className={styles['dur-pill']}>{duration}</span>
        <span className={styles.playline} />
      </div>
      <div className={styles.body}>
        <span className={styles.badge}>{category}</span>
        <h3>{title}</h3>
        <div className={styles.meta}>{meta}</div>
      </div>
    </article>
  )
}

export function ExamplesBento() {
  const navigate = useNavigate()
  const heading = useReveal<HTMLDivElement>()
  const more = useReveal<HTMLButtonElement>()

  return (
    <section className={styles.sec}>
      <div className="shell">
        <div
          className={`${styles['sec-head']} ${styles.reveal} ${heading.className}`}
          ref={heading.ref}
          style={{ transitionDelay: '0ms' }}
        >
          <h2>See ideas become explanations</h2>
          <p>Three questions, shaped into clear visual stories.</p>
        </div>
        <div className={styles.bento}>
          <ExampleCard
            artKey="nn"
            big
            category="Example · AI"
            delay={90}
            duration="1:32"
            meta="9 scenes · Beginner · Clean diagrams"
            title="How do neural networks learn?"
          />
          <div className={styles['bento-side']}>
            <ExampleCard
              artKey="parse"
              category="Example · Computer science"
              delay={180}
              duration="2:11"
              meta="9 scenes · Beginner"
              title="How does a compiler understand code?"
            />
            <ExampleCard
              artKey="moon"
              category="Example · Science"
              delay={270}
              duration="2:05"
              meta="7 scenes · Beginner"
              title="Why doesn't the Moon fall onto Earth?"
            />
          </div>
        </div>
        <button
          className={`${styles['tile-more']} ${styles.reveal} ${more.className}`}
          onClick={() => navigate('/app/videos')}
          ref={more.ref}
          style={{ marginTop: 'var(--s4)', transitionDelay: '0ms', width: '100%' }}
          type="button"
        >
          View all examples <span className={styles.arrow}>→</span>
        </button>
      </div>
    </section>
  )
}
