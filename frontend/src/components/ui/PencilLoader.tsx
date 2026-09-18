import { useId, type CSSProperties } from 'react'
import styles from './PencilLoader.module.css'

interface PencilLoaderProps {
  className?: string
  label?: string
  progress?: number
  size?: number
}

type LoaderStyle = CSSProperties & { '--pencil-size': string }

export function PencilLoader({ className, label = 'Generating video', progress, size = 116 }: PencilLoaderProps) {
  const clipId = `pencil-eraser-${useId().replace(/:/g, '')}`
  const status = progress === undefined ? label : `${label} · ${Math.round(progress)}%`

  return (
    <div
      aria-label={label}
      className={[styles.loader, className].filter(Boolean).join(' ')}
      role="status"
      style={{ '--pencil-size': `${size}px` } as LoaderStyle}
    >
      <svg aria-hidden="true" className={styles.pencil} viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <clipPath id={clipId}><rect height="30" rx="5" ry="5" width="30" /></clipPath>
        </defs>
        <circle className={styles.stroke} fill="none" r="70" stroke="currentColor" strokeDasharray="439.82 439.82" strokeDashoffset="439.82" strokeLinecap="round" strokeWidth="2" />
        <g className={styles.rotate}>
          <g fill="none">
            <circle className={styles.body1} r="64" stroke="hsl(223 90% 50%)" strokeDasharray="402.12 402.12" strokeDashoffset="402" strokeWidth="30" />
            <circle className={styles.body2} r="74" stroke="hsl(223 90% 60%)" strokeDasharray="464.96 464.96" strokeDashoffset="465" strokeWidth="10" />
            <circle className={styles.body3} r="54" stroke="hsl(223 90% 40%)" strokeDasharray="339.29 339.29" strokeDashoffset="339" strokeWidth="10" />
          </g>
          <g className={styles.eraser}>
            <g className={styles.eraserSkew}>
              <rect fill="hsl(223 90% 70%)" height="30" rx="5" ry="5" width="30" />
              <rect clipPath={`url(#${clipId})`} fill="hsl(223 90% 60%)" height="30" width="5" />
              <rect fill="hsl(223 10% 90%)" height="20" width="30" />
              <rect fill="hsl(223 10% 70%)" height="20" width="15" />
              <rect fill="hsl(223 10% 80%)" height="20" width="5" />
              <rect fill="hsl(223 10% 10% / .2)" height="2" width="30" y="6" />
              <rect fill="hsl(223 10% 10% / .2)" height="2" width="30" y="13" />
            </g>
          </g>
          <g className={styles.point}>
            <polygon fill="hsl(33 90% 70%)" points="15 0,30 30,0 30" />
            <polygon fill="hsl(33 90% 50%)" points="15 0,6 30,0 30" />
            <polygon fill="hsl(223 10% 10%)" points="15 0,20 10,10 10" />
          </g>
        </g>
      </svg>
      <span aria-hidden="true" className={styles.label}>{status}</span>
    </div>
  )
}
