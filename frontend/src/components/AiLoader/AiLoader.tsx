import styles from './AiLoader.module.css'

type AiLoaderProps = { label?: string; size?: number; className?: string }

export function AiLoader({ label = 'Generating', size = 112, className }: AiLoaderProps) {
  return (
    <div
      className={[styles.wrapper, className].filter(Boolean).join(' ')}
      style={{ ['--loader-size' as string]: `${size}px` }}
      role="status"
      aria-label={label}
    >
      {label.split('').map((ch, i) => (
        <span key={i} className={styles.letter} style={{ animationDelay: `${i * 0.1}s` }} aria-hidden>
          {ch === ' ' ? '\u00A0' : ch}
        </span>
      ))}
      <div className={styles.ring} aria-hidden />
    </div>
  )
}
