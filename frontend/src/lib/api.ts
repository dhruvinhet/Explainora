import type {
  Audience,
  GenerationInput,
  Job,
  JobStatus,
  Scene,
  SceneStatus,
  VideoDetail,
  VideoSummary,
} from './types'
import { getAccessToken, isSupabaseConfigured } from './supabase'

export const BACKEND_PATHS = {
  api: '/api',
  media: '/media',
  outputs: '/outputs',
  segments: '/segments',
  socket: '/socket.io',
} as const

function relativePath(base: string, path = '') {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`
  return path ? `${base}${normalizedPath}` : base
}

export function apiPath(path = '') {
  return relativePath(BACKEND_PATHS.api, path)
}

export function mediaPath(path = '') {
  return relativePath(BACKEND_PATHS.media, path)
}

export function outputPath(path = '') {
  return relativePath(BACKEND_PATHS.outputs, path)
}

export function segmentPath(path = '') {
  return relativePath(BACKEND_PATHS.segments, path)
}

export class ApiError extends Error {
  readonly status: number
  readonly payload: unknown

  constructor(status: number, payload: unknown) {
    const message = typeof payload === 'object'
      && payload !== null
      && 'error' in payload
      && typeof payload.error === 'string'
      ? payload.error
      : `Request failed with status ${status}`
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.payload = payload
  }
}

export async function apiRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  const accessToken = await getAccessToken()
  if (isSupabaseConfigured && !accessToken) {
    throw new ApiError(401, { error: 'Please sign in before using the studio.' })
  }
  if (accessToken && !headers.has('Authorization')) headers.set('Authorization', `Bearer ${accessToken}`)
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  const response = await fetch(apiPath(path), {
    ...init,
    headers,
  })
  const contentType = response.headers.get('content-type') ?? ''
  const payload: unknown = contentType.includes('application/json')
    ? await response.json()
    : await response.text()

  if (!response.ok) throw new ApiError(response.status, payload)
  return payload as T
}

interface RawScene {
  index?: number
  title?: string
  narration?: string
  status?: string
  image_url?: string | null
  imageUrl?: string | null
  video_url?: string | null
  videoUrl?: string | null
  duration_seconds?: number | null
  durationSec?: number | null
}

interface RawJob {
  job_id?: string
  prompt?: string
  audience?: string
  style?: string
  target_duration_seconds?: number
  status?: string
  progress?: number
  elapsed_sec?: number
  stage?: string
  detail?: string
  scenes?: RawScene[]
  error?: string
}

function normalizeAudience(value?: string): Audience {
  const audience = value?.toLowerCase()
  if (audience === 'intermediate' || audience === 'developer') return audience
  return 'beginner'
}

function normalizeJobStatus(value?: string): JobStatus {
  if (value === 'completed' || value === 'done') return 'done'
  if (value === 'failed' || value === 'error') return 'error'
  if (value === 'canceled' || value === 'cancelled') return 'canceled'
  if (value === 'running') return 'running'
  return 'queued'
}

function normalizeSceneStatus(value?: string): SceneStatus {
  if (value === 'done') return 'done'
  if (value === 'active') return 'active'
  return 'pending'
}

function normalizeScene(scene: RawScene, fallbackIndex: number): Scene {
  const index = Number.isFinite(scene.index) ? Number(scene.index) : fallbackIndex
  const imageUrl = scene.imageUrl ?? scene.image_url ?? undefined
  const videoUrl = scene.videoUrl ?? scene.video_url ?? undefined
  const durationSec = Number(scene.durationSec ?? scene.duration_seconds)
  return {
    index,
    title: scene.title?.trim() || `Scene ${index + 1}`,
    narration: scene.narration ?? '',
    status: normalizeSceneStatus(scene.status),
    ...(imageUrl ? { imageUrl } : {}),
    ...(videoUrl ? { videoUrl } : {}),
    ...(Number.isFinite(durationSec) && durationSec > 0 ? { durationSec } : {}),
  }
}

function normalizeJob(raw: RawJob, requestedId: string): Job {
  return {
    id: raw.job_id || requestedId,
    prompt: raw.prompt ?? '',
    audience: normalizeAudience(raw.audience),
    style: raw.style || 'default',
    durationSec: Math.max(0, Number(raw.target_duration_seconds) || 0),
    status: normalizeJobStatus(raw.status),
    percent: Math.min(100, Math.max(0, Number(raw.progress) || 0)),
    elapsedSec: Math.max(0, Number(raw.elapsed_sec) || 0),
    stage: raw.stage || 'queued',
    detail: raw.detail,
    scenes: (raw.scenes ?? []).map(normalizeScene),
    error: raw.error,
  }
}

function backendStyle(style: string) {
  const normalized = style.trim().toLowerCase()
  if (normalized === 'clean diagrams' || normalized === 'clean-diagrams') return 'clean-diagrams'
  if (normalized === 'whiteboard') return 'mono'
  if (normalized === 'blueprint') return 'ocean'
  return 'default'
}

export async function startGeneration(input: GenerationInput): Promise<{ id: string }> {
  const payload = {
    prompt: input.prompt,
    audience: input.audience,
    style: backendStyle(input.style),
    duration_seconds: input.durationSec,
    mode: 'streaming',
  }
  const body = input.sourceFile
    ? (() => {
        const form = new FormData()
        Object.entries(payload).forEach(([key, value]) => form.append(key, String(value)))
        form.append('document', input.sourceFile as File)
        return form
      })()
    : JSON.stringify(payload)
  const result = await apiRequest<{ job_id: string }>('/videos', {
    method: 'POST',
    body,
  })
  return { id: result.job_id }
}

export async function getJob(id: string): Promise<Job> {
  const raw = await apiRequest<RawJob>(`/jobs/${encodeURIComponent(id)}`)
  return normalizeJob(raw, id)
}

export function cancelGeneration(id: string) {
  return apiRequest<{ job_id: string; status: 'canceled' }>(
    `/jobs/${encodeURIComponent(id)}`,
    { method: 'DELETE' },
  )
}

export function subscribeJob(
  id: string,
  onUpdate: (job: Job) => void,
  onError?: (error: unknown) => void,
) {
  let stopped = false
  let timer: number | undefined
  let controller: AbortController | undefined

  const poll = async () => {
    controller = new AbortController()
    try {
      const raw = await apiRequest<RawJob>(`/jobs/${encodeURIComponent(id)}`, {
        signal: controller.signal,
      })
      if (stopped) return
      const job = normalizeJob(raw, id)
      onUpdate(job)
      if (job.status === 'done' || job.status === 'error' || job.status === 'canceled') return
    } catch (error) {
      if (stopped || (error instanceof DOMException && error.name === 'AbortError')) return
      onError?.(error)
    }
    if (!stopped) timer = window.setTimeout(poll, 1000)
  }

  void poll()
  return () => {
    stopped = true
    controller?.abort()
    if (timer !== undefined) window.clearTimeout(timer)
  }
}

export function listVideos(): Promise<VideoSummary[]> {
  return apiRequest<VideoSummary[]>('/videos')
}

export function getVideo(id: string): Promise<VideoDetail> {
  return apiRequest<VideoDetail>(`/videos/${encodeURIComponent(id)}`)
}

export function startBillingCheckout(plan: 'Pro' | 'Pro Ultra') {
  return apiRequest<{ checkout_url: string }>('/billing/checkout', {
    method: 'POST',
    body: JSON.stringify({ plan: plan === 'Pro Ultra' ? 'pro_ultra' : 'pro' }),
  })
}

export async function auditPasswordResetRequest(email: string) {
  await fetch(apiPath('/account/password-reset-request'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
  }).catch(() => undefined)
}

export function recordPasswordResetComplete() {
  return apiRequest<{ recorded: boolean }>('/account/password-reset-complete', { method: 'POST' })
}

export function changeAccountPassword(
  currentPassword: string,
  newPassword: string,
  confirmation: string,
) {
  return apiRequest<{ changed: boolean; reauthentication_required: boolean }>('/account/password', {
    method: 'POST',
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
      new_password_confirmation: confirmation,
    }),
  })
}

export function deleteAccount(currentPassword: string, confirmation: string) {
  return apiRequest<{ deleted: boolean }>('/account', {
    method: 'DELETE',
    body: JSON.stringify({ current_password: currentPassword, confirmation }),
  })
}
