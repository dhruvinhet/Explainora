import type { ReactNode } from 'react'
import styles from './Paper.module.css'

interface PaperProps {
  children: ReactNode
  className?: string
  tape?: boolean
  holes?: boolean
}

export function Paper({ children, className, tape = false, holes = true }: PaperProps) {
  return (
    <div className={[styles.paper, className].filter(Boolean).join(' ')}>
      {holes && <span className={styles.holes} aria-hidden="true"><i /><i /><i /></span>}
      {tape && <span className={styles.tape} aria-hidden="true" />}
      {children}
    </div>
  )
}
