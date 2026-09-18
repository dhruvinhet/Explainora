import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import type { Job } from '../../lib/types'
import { subscribeJob } from '../../lib/api'
import {
  ProductionContext,
  type ProductionContextValue,
} from '../../hooks/productionContext'

export function ProductionProvider({ children }: { children: ReactNode }) {
  const [job, setJob] = useState<Job | null>(null)
  const [activeJobId, setActiveJobId] = useState<string | null>(() => {
    const stored = sessionStorage.getItem('simi_active_job_id')
    return stored && /^[0-9a-f]{12}$/.test(stored) ? stored : null
  })
  const generating = job?.status === 'queued' || job?.status === 'running'

  const trackJob = useCallback((jobId: string | null) => {
    setActiveJobId((current) => {
      if (current !== jobId) setJob(null)
      return jobId
    })
    if (jobId) sessionStorage.setItem('simi_active_job_id', jobId)
    else sessionStorage.removeItem('simi_active_job_id')
  }, [])

  useEffect(() => {
    if (!activeJobId) return
    return subscribeJob(
      activeJobId,
      (nextJob) => {
        setJob(nextJob)
        if (nextJob.status === 'done' || nextJob.status === 'error' || nextJob.status === 'canceled') {
          sessionStorage.removeItem('simi_active_job_id')
          setActiveJobId(null)
        }
      },
      () => {
        sessionStorage.removeItem('simi_active_job_id')
        setActiveJobId(null)
        setJob(null)
      },
    )
  }, [activeJobId])

  useEffect(() => {
    document.title = generating
      ? `${Math.max(3, job?.percent ?? 0)}% · Explainora`
      : 'Explainora — Ideas, made visible'
  }, [generating, job?.percent])

  const value = useMemo<ProductionContextValue>(() => ({
    job,
    generating,
    setJob,
    trackJob,
  }), [generating, job, trackJob])

  return (
    <ProductionContext.Provider value={value}>
      {children}
    </ProductionContext.Provider>
  )
}
