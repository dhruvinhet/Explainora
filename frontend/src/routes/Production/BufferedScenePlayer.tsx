import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Scene } from '../../lib/types'
import styles from './Production.module.css'

interface Props {
  scenes: Scene[]
  jobDone: boolean
  onSceneChange: (sceneIndex: number) => void
}

function expiresSoon(url: string) {
  try {
    const expires = Number(new URL(url, window.location.href).searchParams.get('expires'))
    return Boolean(expires) && expires * 1000 < Date.now() + 60_000
  } catch {
    return false
  }
}

export function BufferedScenePlayer({ scenes, jobDone, onSceneChange }: Props) {
  const urlCache = useRef(new Map<number, string>())
  const firstVideo = useRef<HTMLVideoElement>(null)
  const secondVideo = useRef<HTMLVideoElement>(null)
  const videos = useMemo(() => [firstVideo, secondVideo] as const, [])
  const [activeSlot, setActiveSlot] = useState<0 | 1>(0)
  const [sceneIndex, setSceneIndex] = useState(0)
  const [started, setStarted] = useState(false)
  const [waiting, setWaiting] = useState(false)
  const [finished, setFinished] = useState(false)

  const ordered = useMemo(() => [...scenes].sort((a, b) => a.index - b.index), [scenes])
  for (const scene of ordered) {
    if (!scene.videoUrl) continue
    const cached = urlCache.current.get(scene.index)
    if (!cached || expiresSoon(cached)) urlCache.current.set(scene.index, scene.videoUrl)
  }

  let readyCount = 0
  while (urlCache.current.has(readyCount)) readyCount += 1
  // Start as soon as scene 1 is playable. Its viewing time becomes the buffer
  // in which the parallel workers finish and preload the following scenes.
  const initialReserve = 1
  const canStart = readyCount >= initialReserve || (jobDone && readyCount > 0)
  const currentUrl = urlCache.current.get(sceneIndex)
  const nextUrl = urlCache.current.get(sceneIndex + 1)

  const advance = useCallback(() => {
    if (!nextUrl) {
      if (jobDone && sceneIndex >= ordered.length - 1) setFinished(true)
      else setWaiting(true)
      return
    }
    setWaiting(false)
    setSceneIndex((value) => value + 1)
    setActiveSlot((value) => value === 0 ? 1 : 0)
  }, [jobDone, nextUrl, ordered.length, sceneIndex])

  const requestAdvance = useCallback(() => {
    if (!nextUrl) {
      advance()
      return
    }
    const nextVideo = videos[activeSlot === 0 ? 1 : 0].current
    if (nextVideo && nextVideo.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) {
      advance()
    } else {
      setWaiting(true)
    }
  }, [activeSlot, advance, nextUrl, videos])

  useEffect(() => {
    onSceneChange(sceneIndex)
    if (started && currentUrl) {
      const video = videos[activeSlot].current
      if (video) void video.play().catch(() => undefined)
    }
  }, [activeSlot, currentUrl, onSceneChange, sceneIndex, started, videos])

  useEffect(() => {
    if (!waiting || !nextUrl) return
    const nextVideo = videos[activeSlot === 0 ? 1 : 0].current
    if (nextVideo && nextVideo.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) advance()
  }, [activeSlot, advance, nextUrl, waiting, videos])

  const start = () => {
    if (!canStart) return
    setStarted(true)
    const video = videos[activeSlot].current
    if (video) void video.play().catch(() => undefined)
  }

  const sources = activeSlot === 0
    ? [currentUrl, nextUrl]
    : [nextUrl, currentUrl]

  return (
    <div className={styles['stream-player']}>
      {[0, 1].map((slot) => {
        const visible = slot === activeSlot
        return (
          <video
            aria-hidden={!visible}
            className={`${styles['scene-video']} ${visible ? styles.visible : ''}`}
            controls={visible && started}
            key={slot}
            onCanPlay={() => {
              if (!visible && waiting) advance()
            }}
            onEnded={visible ? requestAdvance : undefined}
            playsInline
            poster={visible ? ordered[sceneIndex]?.imageUrl : undefined}
            preload="auto"
            src={sources[slot]}
          />
        )
      })}

      {!started && (
        <div className={styles['stream-overlay']}>
          {ordered[0]?.imageUrl && <img alt={ordered[0].title} src={ordered[0].imageUrl} />}
          <div>
            <b>{canStart ? 'Your explanation is ready to begin' : 'Building a smooth playback buffer'}</b>
            <span>{readyCount > 0 ? 'Opening scene ready' : 'Preparing the opening scene'}</span>
            {canStart && <button onClick={start} type="button">▶ Start watching</button>}
          </div>
        </div>
      )}

      {waiting && <div className={styles['buffer-note']}>Preparing the next scene…</div>}
      {finished && <div className={styles['buffer-note']}>Explanation complete</div>}
      {started && !finished && (
        <div className={styles['stream-status']}>
          Scene {sceneIndex + 1} of {ordered.length} · {Math.max(0, readyCount - sceneIndex - 1)} ahead
        </div>
      )}
    </div>
  )
}
