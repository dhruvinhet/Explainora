import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
  type KeyboardEvent,
} from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useGhostType, useMagnetic } from '../../../hooks'
import { useProduction } from '../../../hooks/useProduction'
import { startGeneration } from '../../../lib/api'
import { useAuthUser, type PlanName } from '../../../lib/auth'
import { Button } from '../../../components/Button/Button'
import type { Audience } from '../../../lib/types'
import { SegmentedControl } from './SegmentedControl'
import { StyleSelect, type VisualStyle } from './StyleSelect'
import styles from '../Home.module.css'

const GHOST_PROMPTS = [
  'How do ocean currents shape the climate?',
  'Explain me lexing and parsing in computer science.',
  "Why doesn't the Moon fall onto Earth?",
  'How do neural networks learn?',
]

const EXAMPLES = [
  { icon: '≈', label: 'Ocean currents', prompt: 'How do ocean currents shape the climate?' },
  { icon: '◐', label: 'How GPS works', prompt: 'How does GPS work?' },
  { icon: '⟨⟩', label: 'Lexing & parsing', prompt: 'Explain me lexing and parsing in computer science.' },
  { icon: '◐', label: "Why the Moon doesn't fall", prompt: "Why doesn't the Moon fall onto Earth?" },
  { icon: '⟨⟩', label: 'How neural networks learn', prompt: 'How do neural networks learn?' },
] as const

const DURATION_OPTIONS = [
  { label: '2 min', value: 2 },
  { label: '4 min', value: 4 },
  { label: '6 min', value: 6 },
] as const

const PLAN_MAX_DURATION_MINUTES: Record<PlanName, 2 | 4 | 6> = {
  Free: 2,
  Pro: 4,
  'Pro Ultra': 6,
}

const DURATION_UNLOCK_PLAN: Record<2 | 4 | 6, PlanName> = {
  2: 'Free',
  4: 'Pro',
  6: 'Pro Ultra',
}

const AUDIENCE_OPTIONS = [
  { label: 'Beginner', value: 'beginner' },
  { label: 'Intermediate', value: 'intermediate' },
  { label: 'Developer', value: 'developer' },
] as const

const FORECAST = {
  2: ['8 scenes', '6 min', '2:00'],
  4: ['14 scenes', '10 min', '4:00'],
  6: ['21 scenes', '15 min', '6:00'],
} as const

export function Composer() {
  const navigate = useNavigate()
  const user = useAuthUser()
  const { trackJob } = useProduction()
  const [searchParams] = useSearchParams()
  const initialPrompt = searchParams.get('prompt')?.slice(0, 500) ?? ''
  const composerRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const sourceInputRef = useRef<HTMLInputElement>(null)
  const typingIntervalRef = useRef<number | null>(null)
  const flashTimerRef = useRef<number | null>(null)
  const generateRef = useMagnetic<HTMLButtonElement>(0.18, 90, 1)
  const { ghostHtml, onRealInput } = useGhostType(GHOST_PROMPTS)
  const [prompt, setPrompt] = useState(initialPrompt)
  const [duration, setDuration] = useState<2 | 4 | 6>(2)
  const [audience, setAudience] = useState<Audience>('beginner')
  const [visualStyle, setVisualStyle] = useState<VisualStyle>('Clean diagrams')
  const [forecast, setForecast] = useState<readonly [string, string, string]>(FORECAST[2])
  const [forecastMorphing, setForecastMorphing] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [flashing, setFlashing] = useState(false)
  const [starting, setStarting] = useState(false)
  const [sourceFile, setSourceFile] = useState<File | undefined>()
  const [error, setError] = useState('')
  const maxDuration = PLAN_MAX_DURATION_MINUTES[user?.plan ?? 'Free']
  const durationOptions = useMemo(() => DURATION_OPTIONS.map((option) => {
    const disabled = option.value > maxDuration
    return {
      ...option,
      disabled,
      hint: disabled ? `Available on ${DURATION_UNLOCK_PLAN[option.value]}` : undefined,
    }
  }), [maxDuration])

  useEffect(() => {
    if (duration <= maxDuration) return
    setDuration(2)
    setForecast(FORECAST[2])
    setForecastMorphing(false)
  }, [duration, maxDuration])

  useEffect(() => () => {
    if (typingIntervalRef.current !== null) window.clearInterval(typingIntervalRef.current)
    if (flashTimerRef.current !== null) window.clearTimeout(flashTimerRef.current)
  }, [])

  const autoGrow = () => {
    const textarea = textareaRef.current
    if (!textarea) return
    textarea.style.height = 'auto'
    textarea.style.height = `${Math.min(200, textarea.scrollHeight)}px`
  }

  useEffect(() => {
    if (!initialPrompt) return
    onRealInput(initialPrompt)
    const frame = window.requestAnimationFrame(() => {
      const textarea = textareaRef.current
      if (!textarea) return
      textarea.style.height = 'auto'
      textarea.style.height = `${Math.min(200, textarea.scrollHeight)}px`
    })
    return () => window.cancelAnimationFrame(frame)
  }, [initialPrompt, onRealInput])

  const handlePromptChange = (event: ChangeEvent<HTMLTextAreaElement>) => {
    const nextPrompt = event.target.value
    setPrompt(nextPrompt)
    onRealInput(nextPrompt)
    autoGrow()
  }

  const handleDurationChange = (nextDuration: 2 | 4 | 6) => {
    setDuration(nextDuration)
    setForecastMorphing(true)
    window.setTimeout(() => {
      setForecast(FORECAST[nextDuration])
      setForecastMorphing(false)
    }, 140)
  }

  const generate = async () => {
    if (starting) return
    const cleanPrompt = prompt.trim()
    if (!cleanPrompt) {
      setError('Enter a topic or question to generate a video.')
      textareaRef.current?.focus()
      return
    }
    setError('')
    setStarting(true)
    try {
      const input = {
        prompt: cleanPrompt,
        audience,
        style: visualStyle,
        durationSec: duration * 60,
        sourceFile,
      }
      const { id } = await startGeneration(input)
      sessionStorage.setItem('simi_last_job_id', id)
      trackJob(id)
      sessionStorage.setItem('simi_generation_input', JSON.stringify({
        prompt: cleanPrompt,
        audience,
        style: visualStyle,
        durationSec: duration * 60,
      }))
      navigate(`/app/create?job=${encodeURIComponent(id)}`)
    } catch (generationError) {
      setError(generationError instanceof Error ? generationError.message : 'Could not start generation.')
      setStarting(false)
    }
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') void generate()
  }

  const typeExample = (examplePrompt: string) => {
    if (typingIntervalRef.current !== null) window.clearInterval(typingIntervalRef.current)
    onRealInput(examplePrompt)
    setPrompt('')
    let index = 0

    typingIntervalRef.current = window.setInterval(() => {
      index += 1
      setPrompt(examplePrompt.slice(0, index))

      if (index >= examplePrompt.length) {
        if (typingIntervalRef.current !== null) window.clearInterval(typingIntervalRef.current)
        typingIntervalRef.current = null
        setFlashing(true)
        flashTimerRef.current = window.setTimeout(() => setFlashing(false), 700)
      }
    }, 22)

    composerRef.current?.scrollIntoView({
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
      block: 'center',
    })
  }

  const handleDrag = (event: DragEvent<HTMLDivElement>, nextDragging: boolean) => {
    event.preventDefault()
    setDragging(nextDragging)
  }

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    handleDrag(event, false)
    const file = event.dataTransfer.files[0]
    if (file) setSourceFile(file)
  }

  const handleSourceKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'Enter' && event.key !== ' ') return
    event.preventDefault()
    sourceInputRef.current?.click()
  }

  const morphStyle = {
    opacity: forecastMorphing ? 0.25 : 1,
    transition: 'opacity 140ms',
  }

  return (
    <div className="shell">
      <div
        className={`${styles.composer} ${flashing ? styles.flash : ''}`}
        ref={composerRef}
      >
        <div className={styles['composer-label']}>What do you want to understand?</div>
        <div className={styles['field-wrap']}>
          <textarea
            aria-label="What do you want to understand?"
            className={styles.prompt}
            maxLength={500}
            onChange={handlePromptChange}
            onKeyDown={handleKeyDown}
            ref={textareaRef}
            rows={2}
            value={prompt}
          />
          {!prompt && (
            <div
              className={styles.ghost}
              dangerouslySetInnerHTML={{ __html: ghostHtml }}
            />
          )}
          <div
            className={styles.counter}
            style={{ color: prompt.length > 450 ? 'var(--danger)' : undefined }}
          >
            <span>{prompt.length}</span>/500
          </div>
        </div>

        <div className={styles.util}>
          <div
            aria-label="Add source material. PDF, TXT, or MD"
            className={`${styles.dropzone} ${dragging ? styles.drag : ''}`}
            onDragEnter={(event) => handleDrag(event, true)}
            onDragLeave={(event) => handleDrag(event, false)}
            onDragOver={(event) => handleDrag(event, true)}
            onDrop={handleDrop}
            onClick={() => sourceInputRef.current?.click()}
            onKeyDown={handleSourceKeyDown}
            role="button"
            tabIndex={0}
          >
            <span style={{ fontSize: 15 }}>＋</span> Add source material{' '}
            <span style={{ color: 'var(--text-tertiary)', fontSize: 11.5 }}>
              {sourceFile?.name || 'PDF, TXT, MD'}
            </span>
          </div>
          <input
            accept=".pdf,.txt,.md"
            className={styles['source-input']}
            onChange={(event) => setSourceFile(event.target.files?.[0])}
            ref={sourceInputRef}
            tabIndex={-1}
            type="file"
          />
          <div className={styles['kbd-hint']}>
            press <span className={styles.kbd}>⌘</span><span className={styles.kbd}>↵</span> to generate
          </div>
        </div>

        <div className={styles.forecast}>
          <span className={styles.fx}>≈ <b style={morphStyle}>{forecast[0]}</b></span>
          <span className={styles['dot-sep']} />
          <span className={styles.fx}>≈ <b style={morphStyle}>{forecast[1]}</b> to generate</span>
          <span className={styles['dot-sep']} />
          <span className={styles.fx}>≈ <b style={morphStyle}>{forecast[2]}</b> final runtime</span>
          <span style={{ marginLeft: 'auto', color: 'var(--text-tertiary)', fontSize: 12 }}>
            estimates update as you change settings
          </span>
        </div>

        <div className={styles.controls}>
          <div className={styles.ctl}>
            <label>Duration</label>
            <SegmentedControl ariaLabel={`Duration. ${user?.plan ?? 'Free'} supports up to ${maxDuration} minutes`} options={durationOptions} value={duration} onChange={handleDurationChange} />
          </div>
          <div className={styles.ctl}>
            <label>Audience</label>
            <SegmentedControl ariaLabel="Audience" options={AUDIENCE_OPTIONS} value={audience} onChange={setAudience} />
          </div>
          <StyleSelect value={visualStyle} onChange={setVisualStyle} />
          <Button className={styles.gen} disabled={starting} onClick={() => void generate()} ref={generateRef}>
            {starting ? <><span className={styles.spinner} /> Starting…</> : <>Generate video <span className={styles.arrow}>→</span></>}
          </Button>
        </div>
        {error && <p className={styles['form-error']} role="alert">{error}</p>}
      </div>

      <div className={styles.chips}>
        <span>Try an example</span>
        {EXAMPLES.map((example) => (
          <button className={styles.chip} key={example.label} onClick={() => typeExample(example.prompt)} type="button">
            <i>{example.icon}</i> {example.label}
          </button>
        ))}
      </div>

      <div className={styles['scroll-cue']}>↓</div>
    </div>
  )
}
