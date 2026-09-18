import { createContext } from 'react'
import type { Job } from '../lib/types'

export interface ProductionContextValue {
  job: Job | null
  generating: boolean
  setJob: (job: Job | null) => void
  trackJob: (jobId: string | null) => void
}

export const ProductionContext = createContext<ProductionContextValue | null>(null)
