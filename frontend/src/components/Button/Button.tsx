import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react'
import { Link, type LinkProps } from 'react-router-dom'
import styles from './Button.module.css'

type ButtonVariant = 'primary' | 'ghost'

interface SharedProps {
  variant?: ButtonVariant
  arrow?: boolean
  className?: string
  children: ReactNode
}

export type ButtonProps = SharedProps & ButtonHTMLAttributes<HTMLButtonElement>
export type ButtonLinkProps = SharedProps & LinkProps

function classes(variant: ButtonVariant, className?: string) {
  return [styles.button, styles[variant], className].filter(Boolean).join(' ')
}

function content(children: ReactNode, arrow: boolean) {
  return <>{children}{arrow && <span className={styles.arrow} aria-hidden="true">→</span>}</>
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'primary', arrow = false, className, children, type = 'button', ...props },
  ref,
) {
  return <button {...props} className={classes(variant, className)} ref={ref} type={type}>{content(children, arrow)}</button>
})

export const ButtonLink = forwardRef<HTMLAnchorElement, ButtonLinkProps>(function ButtonLink(
  { variant = 'primary', arrow = false, className, children, ...props },
  ref,
) {
  return <Link {...props} className={classes(variant, className)} ref={ref}>{content(children, arrow)}</Link>
})
