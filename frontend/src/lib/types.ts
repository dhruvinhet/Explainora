export type Audience = 'beginner' | 'intermediate' | 'developer'
export type JobStatus = 'queued' | 'running' | 'done' | 'error' | 'canceled'
export type SceneStatus = 'pending' | 'active' | 'done'

export interface Scene {
  index: number
  title: string
  narration: string
  status: SceneStatus
  imageUrl?: string
  videoUrl?: string
  durationSec?: number
}

export interface Job {
  id: string
  prompt: string
  audience: Audience
  style: string
  durationSec: number
  status: JobStatus
  percent: number
  elapsedSec: number
  stage: string
  detail?: string
  scenes: Scene[]
  error?: string
}

export interface VideoSummary {
  id: string
  title: string
  durationSec: number
  audience: string
  createdAt: string
  posterUrl?: string
  status: 'ready' | 'generating'
  progressPercent?: number
}

export interface VideoDetail {
  id: string
  title: string
  description: string
  audience: string
  durationSec: number
  style: string
  scenes: Scene[]
  coverage: string[]
  coverageSceneMap: number[]
  videoUrl: string
  posterUrl?: string
}

export interface GenerationInput {
  prompt: string
  audience: Audience
  style: string
  durationSec: number
  sourceFile?: File
}
