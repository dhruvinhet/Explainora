import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { cancelGeneration, startGeneration } from '../../lib/api'
import type { Audience, GenerationInput, Job, Scene } from '../../lib/types'
import { useProduction } from '../../hooks/useProduction'
import { PencilLoader } from '../../components/ui/PencilLoader'
import { Button } from '../../components/Button/Button'
import { BufferedScenePlayer } from './BufferedScenePlayer'
import styles from './Production.module.css'

type ProgressStyle = CSSProperties & {
  '--p'?: number
  '--sp'?: string
  '--f'?: number
}

const PIPELINE = [
  'Understanding your topic',
  'Planning the explanation',
  'Writing narration',
  'Creating visual scenes',
  'Generating narration',
  'Synchronising scenes',
  'Rendering final video',
] as const

function formatElapsed(seconds: number) {
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`
}

function formatStyle(style: string) {
  if (style === 'mono') return 'Whiteboard'
  if (style === 'ocean') return 'Blueprint'
  return 'Clean diagrams'
}

function pipelineStage(stage: string) {
  const normalized = stage.toLowerCase()
  if (normalized === 'queued') return 0
  if (normalized === 'planning') return 1
  if (normalized.includes('tts') || normalized.includes('narration')) return 2
  if (normalized.includes('visual')) return 3
  if (normalized.includes('stream')) return 5
  if (normalized.includes('encoding')) return 5
  if (normalized.includes('final') || normalized === 'completed') return 6
  return 0
}

function currentScene(scenes: Scene[]) {
  return scenes.find((scene) => scene.status === 'active')
    ?? scenes.find((scene) => scene.status === 'pending')
    ?? [...scenes].reverse().find((scene) => scene.status === 'done')
}

function retryInput(job: Job): GenerationInput {
  const stored = sessionStorage.getItem('simi_generation_input')
  if (stored) {
    try {
      return JSON.parse(stored) as GenerationInput
    } catch {
      // Fall through to the job payload reconstructed by the API.
    }
  }
  return {
    prompt: job.prompt,
    audience: job.audience as Audience,
    style: formatStyle(job.style),
    durationSec: job.durationSec,
  }
}

export function Production() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const jobId = searchParams.get('job') || sessionStorage.getItem('simi_last_job_id') || ''
  const { job, trackJob } = useProduction()
  const [requestError, setRequestError] = useState('')
  const [retrying, setRetrying] = useState(false)
  const [confirmCancel, setConfirmCancel] = useState(false)
  const [canceling, setCanceling] = useState(false)
  const [viewingSceneIndex, setViewingSceneIndex] = useState(0)
  const cancelTimer = useRef<number | null>(null)

  useEffect(() => {
    if (!jobId) {
      setRequestError('No generation job was provided. Start a new explanation from the home page.')
      return
    }
    setRequestError('')
    trackJob(jobId)
  }, [jobId, trackJob])

  useEffect(() => () => {
    if (cancelTimer.current !== null) window.clearTimeout(cancelTimer.current)
  }, [])

  const handleCancel = async () => {
    if (canceling) return
    if (confirmCancel) {
      setConfirmCancel(false)
      setCanceling(true)
      setRequestError('')
      try {
        await cancelGeneration(jobId)
        trackJob(null)
        if (sessionStorage.getItem('simi_last_job_id') === jobId) {
          sessionStorage.removeItem('simi_last_job_id')
        }
        sessionStorage.removeItem('simi_generation_input')
        navigate('/app', { replace: true })
      } catch (error) {
        setRequestError(error instanceof Error ? error.message : 'Could not cancel generation.')
        setCanceling(false)
      }
      return
    }
    setConfirmCancel(true)
    cancelTimer.current = window.setTimeout(() => setConfirmCancel(false), 3000)
  }

  const handleRetry = async () => {
    if (!job || retrying) return
    setRetrying(true)
    setRequestError('')
    try {
      const { id } = await startGeneration(retryInput(job))
      sessionStorage.setItem('simi_last_job_id', id)
      trackJob(id)
      navigate(`/app/create?job=${encodeURIComponent(id)}`, { replace: true })
    } catch (error) {
      setRequestError(error instanceof Error ? error.message : 'Could not restart generation.')
      setRetrying(false)
    }
  }

  const displayPercent = job?.status === 'done'
    ? 100
    : Math.max(3, Math.round(job?.percent ?? 0))
  const scenes = useMemo(() => job?.scenes ?? [], [job?.scenes])
  const playingScene = scenes.find((scene) => scene.index === viewingSceneIndex && scene.videoUrl)
  const activeScene = playingScene ?? currentScene(scenes)
  const sceneReady = activeScene?.status === 'done' && Boolean(activeScene.imageUrl)
  const activeIndex = activeScene ? scenes.findIndex((scene) => scene.index === activeScene.index) : 0
  const completedScenes = scenes.filter((scene) => scene.status === 'done').length
  const pipeStage = pipelineStage(job?.stage ?? 'queued')
  const activeProgress = scenes.length
    ? Math.max(0.08, ((displayPercent / 100) * scenes.length) % 1)
    : 0
  const failed = job?.status === 'error'
  const visibleError = job?.error || requestError

  return (
    <section aria-label="Production room">
      <div className={`shell ${styles['prod-wrap']}`}>
        <div aria-busy={!failed && job?.status !== 'done'} className={styles.prod}>
          <div className={styles['prod-head']}>
            <div>
              <span className={styles['live-tag']}><i /> Live production</span>
              <h2>{failed ? 'Generation paused' : 'Creating your explanation'}</h2>
              <p className={styles.q}>“{job?.prompt || 'Preparing your topic…'}”</p>
              <p className={styles.m}>
                {Math.max(1, Math.round((job?.durationSec ?? 120) / 60))} min · {' '}
                {job?.audience ? `${job.audience[0].toUpperCase()}${job.audience.slice(1)}` : 'Beginner'} · {' '}
                {formatStyle(job?.style ?? 'default')}
              </p>
            </div>
            <div className={styles['ring-box']}>
              <div
                aria-label={`${displayPercent}% complete`}
                aria-valuemax={100}
                aria-valuemin={3}
                aria-valuenow={displayPercent}
                className={styles.ring}
                role="progressbar"
                style={{ '--p': displayPercent } as ProgressStyle}
              >
                <span>{displayPercent}%</span>
              </div>
              <small>{formatElapsed(job?.elapsedSec ?? 0)} elapsed</small>
            </div>
          </div>

          {visibleError && (
            <div className={styles['error-panel']} role="alert">
              <div><b>Generation couldn’t continue.</b><p>{visibleError}</p></div>
              {job && <Button className={styles['btn-ghost']} disabled={retrying} onClick={() => void handleRetry()} variant="ghost">{retrying ? 'Retrying…' : 'Retry generation'}</Button>}
            </div>
          )}

          {!failed && (
            <div className={styles['prod-body']}>
              <div className={styles.pipeline}>
                <div className="eyebrow">Pipeline</div>
                {PIPELINE.map((title, index) => {
                  const state = index < pipeStage ? styles.done : index === pipeStage ? styles.act : ''
                  return (
                    <div className={`${styles.prow} ${state}`} key={title}>
                      <span className={styles.ic}>
                        <svg width="9" height="9" viewBox="0 0 12 12" aria-hidden="true"><path d="M2 6.4 4.6 9 10 3" /></svg>
                      </span>
                      <span>{title}</span>
                    </div>
                  )
                })}
              </div>

              <div className={styles.stage}>
                <div className={`eyebrow ${styles['stage-label']}`}>Live scene preview</div>
                <div className={styles.preview}>
                  {scenes.some((scene) => scene.videoUrl) && (
                    <BufferedScenePlayer
                      jobDone={job?.status === 'done'}
                      onSceneChange={setViewingSceneIndex}
                      scenes={scenes}
                    />
                  )}
                  <div className={`${styles['loader-layer']} ${scenes.some((scene) => scene.videoUrl) || sceneReady ? styles.hidden : ''}`}>
                    <PencilLoader className={styles['preview-loader']} label="Generating scene" progress={displayPercent} size={132} />
                  </div>
                  {!scenes.some((scene) => scene.videoUrl) && sceneReady && activeScene?.imageUrl && <img alt={activeScene.title} className={styles['preview-image']} key={activeScene.imageUrl} src={activeScene.imageUrl} />}
                </div>
                <div className={styles['stage-cap']}>
                  <div className={styles['stage-title']}>
                    <em>{activeScene ? String(activeIndex + 1).padStart(2, '0') : '—'}</em>
                    <span>{activeScene?.title || job?.detail || 'Planning your explanation'}</span>
                  </div>
                  <div aria-live="polite" className={styles.ticker}>
                    {activeScene?.narration || 'Your generated narration will appear here.'}
                    {activeScene?.status === 'active' && <span className={styles.caret} />}
                  </div>
                </div>
              </div>

              <div className={styles['scenes-panel']}>
                <div className={styles.ph}><span className="eyebrow">Scenes</span><b>{completedScenes}/{scenes.length || '—'}</b></div>
                {scenes.length === 0 && <div className={styles['planning-row']}>Planning scene structure…</div>}
                {scenes.map((scene, index) => {
                  const state = scene.status === 'done' ? styles.done : scene.status === 'active' ? styles.act : ''
                  const sceneProgress = scene.status === 'done' ? 100 : scene.status === 'active' ? activeProgress * 100 : 0
                  return (
                    <div className={`${styles.srow} ${state}`} key={scene.index}>
                      <span className={styles.n}>{String(index + 1).padStart(2, '0')}</span>
                      <span className={styles.tk} style={{ '--sp': `${sceneProgress}%` } as ProgressStyle} />
                      <span>{scene.title}</span>
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          <div className={styles['prod-foot']}>
            <div className={styles.segbar} aria-hidden="true">
              {(scenes.length ? scenes : Array.from({ length: 8 }, (_, index) => ({ index, status: 'pending' as const }))).map((scene) => {
                const fill = scene.status === 'done' ? 1 : scene.status === 'active' ? activeProgress : 0
                return <i className={`${styles.segment} ${scene.status === 'active' ? styles.act : ''}`} key={scene.index} style={{ '--f': fill } as ProgressStyle} />
              })}
            </div>
            <span className={`mono ${styles.pct}`}>{displayPercent}%</span>
            {job?.status !== 'done' && <Button className={`${styles['btn-ghost']} ${confirmCancel ? styles['cancel-confirm'] : ''}`} disabled={canceling} onClick={() => void handleCancel()} variant="ghost">{canceling ? 'Canceling…' : confirmCancel ? 'Click again to cancel' : 'Cancel'}</Button>}
            {job?.status === 'done' && <Button className={styles['btn-ghost']} onClick={() => navigate(`/app/videos/${encodeURIComponent(job.id)}`)} variant="ghost">Open finished video</Button>}
            <Button className={`${styles['btn-ghost']} ${styles.notify}`} variant="ghost">🔔 Notify me</Button>
          </div>
          <div className={styles['safe-note']}>
            <span className={styles['status-dot']} />
            <span>Your video is saved automatically — you can leave this page. Most videos finish in about 6 minutes.</span>
            <span className={styles['tab-title']}>tab title: “{displayPercent}% · Explainora”</span>
          </div>
        </div>
      </div>
    </section>
  )
}
