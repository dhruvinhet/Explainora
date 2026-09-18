import { art, type ArtKey } from '../../../assets/art'

interface ArtMarkupProps {
  artKey: ArtKey
  className?: string
}

export function ArtMarkup({ artKey, className }: ArtMarkupProps) {
  return (
    <div
      className={className}
      dangerouslySetInnerHTML={{ __html: art[artKey] }}
    />
  )
}
