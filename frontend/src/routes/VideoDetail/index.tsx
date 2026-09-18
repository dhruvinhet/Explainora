import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { getVideo, listVideos } from '../../lib/api'
import type { VideoDetail as VideoDetailData, VideoSummary } from '../../lib/types'
import { AiLoader } from '../../components/AiLoader/AiLoader'
import { Button, ButtonLink } from '../../components/Button/Button'
import styles from './VideoDetail.module.css'

type FillStyle = CSSProperties & { '--f': number }

function formatTime(time: number) {
  return `${Math.floor(time / 60)}:${String(Math.floor(time % 60)).padStart(2, '0')}`
}

function relativeDate(value: string) {
  const days = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 86_400_000))
  if (days === 0) return 'today'
  if (days === 1) return 'yesterday'
  return `${days} days ago`
}

export function VideoDetail() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const videoRef = useRef<HTMLVideoElement>(null)
  const railRef = useRef<HTMLDivElement>(null)
  const railWrapRef = useRef<HTMLDivElement>(null)
  const copyTimerRef = useRef<number | null>(null)
  const [video, setVideo] = useState<VideoDetailData | null>(null)
  const [upNext, setUpNext] = useState<VideoSummary[]>([])
  const [error, setError] = useState('')
  const [playing, setPlaying] = useState(false)
  const [time, setTime] = useState(0)
  const [mediaDuration, setMediaDuration] = useState(0)
  const [mediaReady, setMediaReady] = useState(false)
  const [displayScene, setDisplayScene] = useState(0)
  const [swapping, setSwapping] = useState(false)
  const [linkedScene, setLinkedScene] = useState<number | null>(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let current = true
    setVideo(null)
    setError('')
    setMediaReady(false)
    Promise.all([getVideo(id), listVideos()])
      .then(([detail, videos]) => {
        if (!current) return
        setVideo(detail)
        setUpNext(videos.filter((item) => item.id !== id && item.status === 'ready').slice(0, 4))
      })
      .catch((reason: unknown) => {
        if (current) setError(reason instanceof Error ? reason.message : 'Unable to load this video.')
      })
    return () => { current = false }
  }, [id])

  const duration = mediaDuration || video?.durationSec || 1
  const sceneCount = video?.scenes.length || 1
  const scene = Math.min(sceneCount - 1, Math.floor((time / duration) * sceneCount))
  const sceneStart = scene * duration / sceneCount
  const inScene = Math.max(0, Math.min(1, (time - sceneStart) / (duration / sceneCount)))

  useEffect(() => {
    if (scene === displayScene || !video) return
    setSwapping(true)
    const timer = window.setTimeout(() => {
      setDisplayScene(scene)
      setSwapping(false)
    }, 200)
    return () => window.clearTimeout(timer)
  }, [displayScene, scene, video])

  useEffect(() => {
    const rail = railRef.current
    const active = rail?.querySelector<HTMLElement>(`[data-scene="${scene}"]`)
    if (!rail || !active) return
    rail.scrollTo({
      left: active.offsetLeft - rail.clientWidth / 2 + active.offsetWidth / 2,
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
    })
  }, [scene])

  useEffect(() => () => {
    if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current)
  }, [])

  const selected = video?.scenes[Math.min(displayScene, Math.max(0, sceneCount - 1))]
  const poster = video?.posterUrl || video?.scenes.find((item) => item.imageUrl)?.imageUrl
  const detailsJson = useMemo(() => video ? JSON.stringify({
    id: video.id,
    audience: video.audience,
    style: video.style,
    duration_seconds: video.durationSec,
    scenes: video.scenes.length,
    video_url: video.videoUrl,
  }, null, 2) : '', [video])

  const selectScene = (index: number) => {
    if (!videoRef.current || !video) return
    const nextTime = index * duration / Math.max(1, video.scenes.length)
    videoRef.current.currentTime = nextTime
    setTime(nextTime)
  }

  const togglePlayback = () => {
    const player = videoRef.current
    if (!player) return
    if (player.paused) void player.play().catch(() => undefined)
    else player.pause()
  }

  const selectCoverageScene = (index: number) => {
    selectScene(index)
    railWrapRef.current?.scrollIntoView({
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
      block: 'center',
    })
  }

  const copyLink = () => {
    void navigator.clipboard?.writeText(window.location.href).catch(() => undefined)
    setCopied(true)
    if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current)
    copyTimerRef.current = window.setTimeout(() => setCopied(false), 1600)
  }

  if (error) return (
    <section className={`shell ${styles.state}`} aria-label="Video detail error">
      <div><div className="eyebrow">Unable to load video</div><h1>{error}</h1><ButtonLink className={styles['btn-primary']} to="/app/videos">Back to library</ButtonLink></div>
    </section>
  )

  if (!video) return (
    <section className={`shell ${styles.state}`} aria-label="Loading video" aria-busy="true">
      <AiLoader label="Loading" size={112} />
    </section>
  )

  return (
    <section aria-label="Video detail">
      <div className={`shell ${styles['vd-top']}`}>
        <Link className={styles.back} to="/app/videos">← My videos</Link>
        <div className={styles['vd-metarow']}>
          <span className={styles.mchip}>{video.audience}</span>
          <span className={`${styles.mchip} mono`}>{formatTime(video.durationSec)}</span>
          <span className={styles.mchip}>{video.scenes.length} scenes</span>
          <span className={styles.mchip}>{video.style}</span>
        </div>
        <h1>{video.title}</h1>
        <p className={styles.desc}>{video.description}</p>

        <div className={styles.player}>
          {!poster && <div className={styles['poster-fallback']}><span>{video.title}</span></div>}
          <video
            className={styles['video-element']}
            onDurationChange={(event) => setMediaDuration(Number.isFinite(event.currentTarget.duration) ? event.currentTarget.duration : 0)}
            onEnded={() => setPlaying(false)}
            onLoadedData={() => setMediaReady(true)}
            onPause={() => setPlaying(false)}
            onPlay={() => setPlaying(true)}
            onTimeUpdate={(event) => setTime(event.currentTarget.currentTime)}
            poster={poster}
            preload="metadata"
            ref={videoRef}
            src={video.videoUrl}
          />
          {!mediaReady && <div className={styles['player-loading']}><AiLoader label="Loading" size={112} /></div>}
          <div className={styles.pcontrols}>
            <div className={styles.chapters} aria-label="Video chapters" role="group">
              {video.scenes.map((item, index) => (
                <button aria-current={index === scene ? 'true' : undefined} aria-label={`Seek to scene ${index + 1}: ${item.title}`} className={styles.chapter} data-chapter={index + 1} key={`${item.index}-${item.title}`} onClick={() => selectScene(index)} style={{ '--f': index < scene ? 1 : index === scene ? inScene : 0 } as FillStyle} type="button" />
              ))}
            </div>
            <div className={styles['pctl-row']}>
              <button aria-label={playing ? 'Pause' : 'Play'} className={styles.pbtn} onClick={togglePlayback} type="button">{playing ? '❚❚' : '▶'}</button>
              <span className={styles.ptime}>{formatTime(time)} / {formatTime(duration)}</span>
              <span className={styles.pnow}>{String(scene + 1).padStart(2, '0')} — {video.scenes[scene]?.title}</span>
              <button aria-label="Captions" className={styles.pbtn} type="button">CC</button>
              <button aria-label="Playback speed: 1×" className={styles.pbtn} type="button">1×</button>
              <button aria-label="Fullscreen" className={styles.pbtn} onClick={() => void videoRef.current?.requestFullscreen()} type="button">⛶</button>
            </div>
          </div>
        </div>

        <div className={styles['vd-actions']}>
          <Button className={styles['btn-ghost']} onClick={() => navigate(`/app?prompt=${encodeURIComponent(video.title)}`)} variant="ghost">↺ Edit &amp; regenerate</Button>
          <Button className={styles['btn-ghost']} onClick={() => navigate(`/app?prompt=${encodeURIComponent(video.title)}`)} variant="ghost">⧉ Duplicate</Button>
          <div className={styles.right}>
            <Button className={`${styles['btn-ghost']} ${copied ? styles.copied : ''}`} onClick={copyLink} variant="ghost">{copied ? '✓ Copied' : '⇗ Copy link'}</Button>
            <ButtonLink arrow className={styles['btn-primary']} download to={video.videoUrl}>Export MP4</ButtonLink>
          </div>
        </div>

        <div className={styles['scenes-section']}>
          <div className={styles['sec-head']}><h2>Scenes</h2><p className="mono">{video.scenes.length} chapters</p></div>
          <div className={styles['rail-wrap']} ref={railWrapRef}>
            <div className={styles.rail} ref={railRef}>
              {video.scenes.map((item, index) => (
                <button className={[styles.scene,index === scene ? styles.on : '',index === linkedScene ? styles.linked : ''].filter(Boolean).join(' ')} data-scene={index} key={`${item.index}-${item.title}`} onClick={() => selectScene(index)} type="button">
                  <span className={styles.th}>{item.imageUrl ? <img alt="" src={item.imageUrl} /> : <span className={styles['scene-fallback']}>{item.title}</span>}</span>
                  <span className={styles.cap}><b>{String(index + 1).padStart(2, '0')}</b>{item.title}</span>
                  <span className={styles.sfill} style={{ width: index === scene ? `${inScene * 100}%` : 0 }} />
                </button>
              ))}
            </div>
          </div>

          {selected && <div className={`${styles.sel} ${swapping ? styles.swap : ''}`}><div className="eyebrow">Selected scene</div><h3>{String(displayScene + 1).padStart(2, '0')} — {selected.title}</h3><p>{selected.narration}</p></div>}

          <div className={styles['coverage-section']}>
            <h2>What this video covers</h2>
            <div className={styles.cov} onPointerLeave={() => setLinkedScene(null)}>
              {video.coverage.map((coverage, index) => {
                const mappedScene = Math.min(sceneCount - 1, Math.max(0, video.coverageSceneMap[index] ?? index))
                return <button key={`${index}-${coverage}`} onClick={() => selectCoverageScene(mappedScene)} onFocus={() => setLinkedScene(mappedScene)} onBlur={() => setLinkedScene(null)} onPointerEnter={() => setLinkedScene(mappedScene)} type="button"><svg className={styles.ck} viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.4 6.4 12 13 4" /></svg><span>{coverage}</span><span className={styles.jump}>scene {String(mappedScene + 1).padStart(2, '0')} →</span></button>
              })}
            </div>
          </div>

          <details className={styles.dev}><summary><span className={styles.chev}>›</span> Developer details</summary><div className={styles['dev-content']}><div className={styles['dev-content-inner']}><pre>{detailsJson}</pre></div></div></details>
        </div>
      </div>

      <div className={styles.upnext}><div className="shell"><div className={`eyebrow ${styles['upnext-label']}`}>Up next from your library</div><div className={styles.row}>
        {upNext.map((item) => <button className={styles.lcard} key={item.id} onClick={() => navigate(`/app/videos/${item.id}`)} type="button"><span className={styles.media}>{item.posterUrl ? <img alt="" src={item.posterUrl} /> : <span className={styles.fallback}><span className={styles.badge}>{item.audience}</span><span>{item.title}</span></span>}</span><span className={styles['card-body']}><strong>{item.title}</strong><span className={styles.meta}><span className="mono">{formatTime(item.durationSec)}</span> · {item.audience} · {relativeDate(item.createdAt)}</span></span></button>)}
      </div></div></div>
    </section>
  )
}
