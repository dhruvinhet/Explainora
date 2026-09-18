import { useState, type CSSProperties } from 'react'
import { useReveal, useSpotlight } from '../../../hooks'
import styles from '../Sections.module.css'

const FEATURES = [
  {
    title: 'Scene-by-scene',
    copy: 'Every explanation is planned as a clear sequence, with a focused visual and narration for each idea.',
    icon: <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="8" height="7" rx="2"/><rect x="13" y="4" width="8" height="7" rx="2"/><rect x="3" y="13" width="8" height="7" rx="2"/><rect x="13" y="13" width="8" height="7" rx="2"/></svg>,
  },
  {
    title: 'Any topic',
    copy: 'From science and engineering to history and code, Explainora shapes the format around whatever you ask.',
    icon: <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 1.4 4.1L17.5 8.5l-4.1 1.4L12 14l-1.4-4.1-4.1-1.4 4.1-1.4z"/><path d="m18.5 14 .8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z"/></svg>,
  },
  {
    title: 'Export & share',
    copy: 'Download a finished MP4, copy a link, or revisit every scene from your personal video library.',
    icon: <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 15V3m0 0L7.5 7.5M12 3l4.5 4.5"/><path d="M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7"/></svg>,
  },
] as const

export function Features() {
  const reveal = useReveal<HTMLDivElement>()
  const styleSpotlight = useSpotlight<HTMLElement>()
  const [skin, setSkin] = useState<'ocean' | 'forest' | 'sunset'>('ocean')
  return (
    <section className={styles.section} id="features">
      <div className="shell">
        <div className={styles.sectionHead}>
          <div className="eyebrow">Built for clarity</div>
          <h2>Everything an explanation needs</h2>
          <p>Thoughtful structure, topic-flexible visuals, and a finished video ready to use anywhere.</p>
        </div>
        <div className={`${styles.featuresGrid} ${styles.reveal} ${reveal.className}`} ref={reveal.ref}>
          {FEATURES.map((feature, index) => <FeatureCard feature={feature} index={index} key={feature.title} />)}
          <article className={`${styles.featureCard} ${styles.styleFeature}`} data-skin={skin} ref={styleSpotlight}>
            <div><span className={styles.category}>Interactive</span><h3>Choose your visual voice</h3><p>Preview a coherent art direction before Explainora renders the first scene.</p></div>
            <div className={styles.stylePreview} aria-live="polite"><i /><b /><span /></div>
            <div className={styles.skinSwitch} aria-label="Preview style"><button aria-pressed={skin === 'ocean'} onClick={() => setSkin('ocean')} type="button">Ocean</button><button aria-pressed={skin === 'forest'} onClick={() => setSkin('forest')} type="button">Forest</button><button aria-pressed={skin === 'sunset'} onClick={() => setSkin('sunset')} type="button">Sunset</button></div>
          </article>
        </div>
      </div>
    </section>
  )
}

function FeatureCard({ feature, index }: { feature: typeof FEATURES[number]; index: number }) {
  const spotlight = useSpotlight<HTMLElement>()
  return (
    <article className={styles.featureCard} ref={spotlight} style={{ '--stagger': `${index * 110}ms` } as CSSProperties}>
      <span className={styles.featureIcon}>{feature.icon}</span>
      <h3>{feature.title}</h3>
      <p>{feature.copy}</p>
    </article>
  )
}
