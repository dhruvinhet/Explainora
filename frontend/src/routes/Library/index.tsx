import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
} from 'react'
import { useNavigate } from 'react-router-dom'
import { listVideos } from '../../lib/api'
import type { VideoSummary } from '../../lib/types'
import { PencilLoader } from '../../components/ui/PencilLoader'
import { Button } from '../../components/Button/Button'
import styles from './Library.module.css'

const FILTERS = ['All', 'Beginner', 'Intermediate', 'Developer'] as const
type Filter = typeof FILTERS[number]

function formatDuration(seconds: number) {
  return `${Math.floor(seconds / 60)}:${String(Math.round(seconds % 60)).padStart(2, '0')}`
}

function relativeDate(createdAt: string) {
  const elapsedDays = Math.max(0, Math.floor((Date.now() - Date.parse(createdAt)) / 86_400_000))
  if (elapsedDays === 0) return 'Today'
  if (elapsedDays === 1) return 'Yesterday'
  return `${elapsedDays} days ago`
}

function titleAudience(audience: string) {
  return audience ? `${audience[0].toUpperCase()}${audience.slice(1)}` : 'Beginner'
}

interface LibraryFilterProps {
  value: Filter
  onChange: (filter: Filter) => void
}

function LibraryFilter({ value, onChange }: LibraryFilterProps) {
  const rootRef = useRef<HTMLDivElement>(null)
  const buttonRefs = useRef<Array<HTMLButtonElement | null>>([])
  const [pillStyle, setPillStyle] = useState<CSSProperties>({})

  const movePill = useCallback(() => {
    const index = FILTERS.indexOf(value)
    const selected = buttonRefs.current[index]
    if (!selected) return
    setPillStyle({ width: selected.offsetWidth, transform: `translateX(${selected.offsetLeft - 3}px)` })
  }, [value])

  useLayoutEffect(() => {
    const timer = window.setTimeout(movePill, 80)
    return () => window.clearTimeout(timer)
  }, [movePill])

  useEffect(() => {
    const root = rootRef.current
    if (!root) return
    const observer = new ResizeObserver(movePill)
    observer.observe(root)
    return () => observer.disconnect()
  }, [movePill])

  return (
    <div aria-label="Filter videos" className={styles.fchips} ref={rootRef} role="listbox">
      <span className={styles.pill} style={pillStyle} />
      {FILTERS.map((filter, index) => (
        <button aria-selected={filter === value} key={filter} onClick={() => onChange(filter)} ref={(element) => { buttonRefs.current[index] = element }} role="option" type="button">{filter}</button>
      ))}
    </div>
  )
}

function Poster({ title, audience, url }: { title: string; audience: string; url?: string }) {
  const [loaded, setLoaded] = useState(false)
  if (!url) {
    return <div className={styles.fallback}><span className={styles.badge}>{titleAudience(audience)}</span><p>{title}</p></div>
  }
  return (
    <>
      <img alt="" onLoad={() => setLoaded(true)} src={url} />
      <div className={`${styles.skel} ${loaded ? styles.gone : ''}`}><div className={styles.shimmer} /></div>
    </>
  )
}

function keyboardNavigate(event: KeyboardEvent<HTMLElement>, navigate: () => void) {
  if (event.key !== 'Enter' && event.key !== ' ') return
  event.preventDefault()
  navigate()
}

export function Library() {
  const navigate = useNavigate()
  const searchRef = useRef<HTMLInputElement>(null)
  const [videos, setVideos] = useState<VideoSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<Filter>('All')
  const [hidden, setHidden] = useState<Set<string>>(new Set())

  const loadVideos = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const result = await listVideos()
      setVideos([...result].sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt)))
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : 'Could not load your videos.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void loadVideos() }, [loadVideos])

  useEffect(() => {
    const focusSearch = (event: globalThis.KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 'k') return
      event.preventDefault()
      searchRef.current?.focus()
    }
    window.addEventListener('keydown', focusSearch)
    return () => window.removeEventListener('keydown', focusSearch)
  }, [])

  const matches = useMemo(() => {
    const loweredQuery = query.toLowerCase()
    return videos.map((video) => (
      (filter === 'All' || video.audience.toLowerCase() === filter.toLowerCase())
      && video.title.toLowerCase().includes(loweredQuery)
    ))
  }, [filter, query, videos])

  useLayoutEffect(() => {
    setHidden((current) => {
      const next = new Set(current)
      matches.forEach((match, index) => { if (match) next.delete(String(index)) })
      return next
    })
    const timer = window.setTimeout(() => {
      setHidden(new Set(matches.flatMap((match, index) => match ? [] : [String(index)])))
    }, 200)
    return () => window.clearTimeout(timer)
  }, [matches])

  const visibilityClass = (index: number) => [
    matches[index] ? '' : styles.filtered,
    hidden.has(String(index)) ? styles.hide : '',
  ].filter(Boolean).join(' ')

  return (
    <section aria-label="Video library">
      <div className="shell">
        <div className={styles['lib-head']}>
          <div><div className="eyebrow">Your library</div><h1>My videos <em className="num">· {videos.length}</em></h1></div>
          <Button className={styles['btn-primary']} onClick={() => navigate('/app')}>＋ New video</Button>
        </div>

        <div className={styles.toolbar}>
          <label className={styles.search}>
            <span className={styles['search-icon']}>⌕</span>
            <input aria-label="Search explanations" onChange={(event) => setQuery(event.target.value)} placeholder="Search explanations…" ref={searchRef} type="search" value={query} />
            <span className={styles.kbd}>⌘K</span>
          </label>
          <LibraryFilter value={filter} onChange={setFilter} />
          <button aria-label="Sort videos: Newest" className={styles.sortsel} type="button">Newest <span className={styles['sort-arrow']}>▾</span></button>
        </div>

        {error && <div className={styles['library-error']} role="alert"><span>{error}</span><Button className={styles['btn-primary']} onClick={() => void loadVideos()}>Try again</Button></div>}

        <div className={styles.grid}>
          {loading && Array.from({ length: 6 }, (_, index) => (
            <article className={styles.lcard} key={index} aria-hidden="true">
              <div className={styles.media}><div className={styles.skel}><div className={styles.shimmer} /></div></div>
              <div className={styles.body}><div className={styles['text-skeleton']} /><div className={styles['meta-skeleton']} /></div>
            </article>
          ))}

          {!loading && videos.map((video, index) => {
            const generating = video.status === 'generating'
            const progress = Math.max(3, Math.round(video.progressPercent ?? 0))
            const open = () => navigate(generating ? `/app/create?job=${encodeURIComponent(video.id)}` : `/app/videos/${encodeURIComponent(video.id)}`)
            return (
              <article className={`${styles.lcard} ${generating ? styles.working : ''} ${visibilityClass(index)}`} key={video.id} onClick={open} onKeyDown={(event) => keyboardNavigate(event, open)} role="link" tabIndex={0}>
                <div className={styles.media}>
                  {generating
                    ? <div className={styles['working-media']}><PencilLoader label="Generating" progress={progress} size={76} /><div className={styles['working-copy']}>Creating your explanation</div></div>
                    : <Poster audience={video.audience} title={video.title} url={video.posterUrl} />}
                </div>
                {!generating && <button aria-label={`More options for ${video.title}`} className={styles['menu-btn']} onClick={(event) => event.stopPropagation()} type="button">⋯</button>}
                <div className={styles.body}>
                  <h3>{video.title}</h3>
                  <div className={styles.meta}>
                    {generating && <span className={styles['live-tag']}><i />Generating</span>}
                    <span className="mono">{formatDuration(video.durationSec)}</span> · {titleAudience(video.audience)} · {relativeDate(video.createdAt)}
                  </div>
                </div>
              </article>
            )
          })}

          {!loading && !error && videos.length === 0 && <div className={styles.empty}>No videos yet. Start with any question or idea.</div>}
        </div>
      </div>
    </section>
  )
}
