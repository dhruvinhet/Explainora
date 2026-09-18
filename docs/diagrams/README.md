# PencilVid / Simi Studio — System Diagram Pack

This pack documents the current repository architecture and the primary video-generation workflow. The diagrams are written in Mermaid so they remain version-controlled, reviewable, and easy to export from Mermaid-compatible editors.

## Scope and assumptions

- The product name varies across the repository (`PencilVid`, `Simi Studio`, and `Explainora`). These diagrams use **PencilVid / Simi Studio** as the neutral system label.
- Production uses the durable Supabase queue and a separate worker. Local development may execute jobs in-process.
- The project timeline is a **proposed 12-week delivery plan**, starting 21 September 2026. It is not a reconstruction of repository history.
- Billing currently includes demo-oriented flows in parts of the implementation; production payment-provider webhook integration is shown as a hardening task.

## Diagram index

1. [Use Case Diagram](use-case-diagram.mmd)
2. [Video Generation Activity Diagram](activity-diagram.mmd)
3. [Class Diagram](class-diagram.mmd)
4. [High-Level System Architecture](high-level-architecture.mmd)
5. [Low-Level Generation Architecture](low-level-architecture.mmd)
6. [Proposed Project Timeline](project-timeline.mmd)

## 1. Use Case Diagram

```mermaid
flowchart LR
    visitor[Visitor]
    user[Registered User]
    admin[Operator / Admin]
    payment[Payment Provider]
    llm[LLM Provider]

    subgraph system["PencilVid / Simi Studio"]
        browse([Browse landing page and examples])
        auth([Sign up, sign in, recover password])
        manage([Manage profile and preferences])
        create([Create explainer-video request])
        upload([Upload source document])
        configure([Choose audience, style, duration, and quality])
        monitor([Monitor scene-by-scene progress])
        cancel([Cancel generation])
        preview([Preview buffered scenes])
        library([View personal video library])
        watch([Watch and download completed video])
        plans([View plans and upgrade subscription])
        account([Change password or delete account])
        operations([Inspect health, logs, jobs, usage, and key-pool status])
        generate([Generate script, narration, visuals, captions, and MP4])
        quota([Authenticate, rate-limit, and reserve quota])
    end

    visitor --> browse
    visitor --> auth
    user --> manage
    user --> create
    user --> monitor
    user --> cancel
    user --> preview
    user --> library
    user --> watch
    user --> plans
    user --> account
    admin --> operations

    create -. includes .-> upload
    create -. includes .-> configure
    create -. includes .-> quota
    create -. triggers .-> generate
    generate --> llm
    plans --> payment
    generate -. publishes progress .-> monitor
    generate -. produces .-> preview
    generate -. stores .-> library
```

## 2. Activity Diagram

```mermaid
flowchart TD
    start((Start)) --> compose[Enter prompt or attach document]
    compose --> config[Choose audience, style, duration, captions, and quality]
    config --> submit[Submit generation request]
    submit --> valid{Request valid and authenticated?}
    valid -- No --> reject[Show validation or authentication error] --> stop1((End))
    valid -- Yes --> quota{Rate limit and quota available?}
    quota -- No --> limit[Return limit or upgrade guidance] --> stop2((End))
    quota -- Yes --> reserve[Atomically reserve quota and create queued job]
    reserve --> worker[Worker claims job]
    worker --> extract{Source document attached?}
    extract -- Yes --> parse[Extract and sanitize document text]
    extract -- No --> plan
    parse --> plan[Director builds scope-locked scene plan]
    plan --> preflight{Plan passes duration, coverage, and factual checks?}
    preflight -- No, repairable --> revise[Repair or revise plan] --> preflight
    preflight -- No, terminal --> fail[Persist failure and diagnostics] --> refund[Release reserved quota where applicable] --> stop3((End))
    preflight -- Yes --> scenes[Persist planned scenes]

    scenes --> parallel{{For each scene in parallel}}
    parallel --> tts[Generate narration audio and timestamps]
    parallel --> visuals[Generate and validate storyboard / SVG]
    tts --> ready{Audio and visual ready?}
    visuals --> ready
    ready -- No, retry available --> retry[Repair or fallback scene asset] --> parallel
    ready -- No, exhausted --> fail
    ready -- Yes --> segment[Render animated scene segment and captions]
    segment --> publish[Persist scene status and publish buffered progress]
    publish --> more{More scenes?}
    more -- Yes --> parallel
    more -- No --> concat[Concatenate segments and mix final media]
    concat --> quality{Final quality and duration acceptable?}
    quality -- No, recoverable --> repairfinal[Apply bounded repair or tempo correction] --> quality
    quality -- No, terminal --> fail
    quality -- Yes --> store[Upload video, poster, manifest, and evaluation]
    store --> complete[Mark job complete and record usage]
    complete --> notify[Client receives final URLs and library entry]
    notify --> stop4((End))

    cancel{Cancellation requested?}
    worker -. checks .-> cancel
    parallel -. checks .-> cancel
    concat -. checks .-> cancel
    cancel -- Yes --> canceled[Stop safely and mark canceled] --> stop5((End))
```

## 3. Class Diagram

```mermaid
classDiagram
    direction LR

    class ProductionClient {
        +startGeneration(input) JobRef
        +subscribeJob(id, onUpdate)
        +cancelGeneration(id)
        +listVideos() VideoSummary[]
        +getVideo(id) VideoDetail
    }

    class FlaskAPI {
        +createVideo(request) JobRef
        +jobStatus(jobId) Job
        +cancelJob(jobId)
        +listVideos() VideoSummary[]
        +videoDetail(jobId) VideoDetail
        +billingCheckout(plan) Checkout
    }

    class SupabaseStore {
        +authenticate(token) User
        +createJob(userId, jobId, payload) Job
        +claimJob(workerId) Job
        +heartbeatJob(jobId, workerId)
        +replaceScenes(userId, jobId, scenes)
        +updateScene(jobId, index, changes)
        +completeVideo(userId, jobId, manifest) Video
        +cancelJob(userId, jobId) Job
        +quotaStatus(userId) Quota
    }

    class RenderWorker {
        +runOnce() bool
        +main()
        -hydratePayload(job) Payload
        -heartbeat(jobId)
        -watchCancellation(jobId)
    }

    class JobRuntimeConfig {
        +style String
        +qualityMode String
        +captions bool
        +fromPayload(payload) JobRuntimeConfig
        +diagnostics() Map
    }

    class Director {
        +createVideoPlan(prompt, duration, audience) VideoPlan
        +revisePlanForDuration(plan, actual, target) VideoPlan
        +desiredSceneCount(duration) int
        +narrationWordCount(plan) int
    }

    class SegmentPipeline {
        +runStreamingPipeline(jobId, plan, runtime) Manifest
        -buildSegment(scene) Segment
        -makeAnimationTimeline(scene) Timeline
        -buildEmergencySegment(scene) Segment
    }

    class VisualEngine {
        +createVisualPlan(text, constraints) VisualPlan
        +normalizeVisualPlan(plan) VisualPlan
        +evaluatePreRenderQualityGate(plan) Report
        +renderVisualPlan(plan, sketch) SVG
    }

    class StoryboardEngine {
        +sanitizeStoryboard(raw) Storyboard
        +buildStoryboardScene(raw) VisualPlan
    }

    class TTSEngine {
        +resolveBackend() String
        +synthesize(text, output) Audio
        +audioDurationSeconds(path) float
        +adjustTempo(source, target, tempo) Audio
        +wordTimestamps(audio) WordTimestamp[]
    }

    class VideoEngine {
        +svgToPng(svg, path) Image
        +renderSceneVideo(image, audio, path) Segment
        +renderSceneVideoSequence(frames, audio, path) Segment
        +concatSceneVideos(segments, output) Video
        +writeManifest(path, manifest)
    }

    class LLMProvider {
        +getLLMConfig(role) LLMConfig
        +createLLMClient(config) Client
        +pooledCreate(client, config, request) Response
        +fitCompletionTokenBudget(config, messages, requested) int
    }

    class APIKeyPool {
        +checkout(tokens) KeyEntry
        +record(entry, tokens)
        +reconcile(entry, reserved, actual)
        +quarantine(entry, severity)
        +secondsUntilCapacity(tokens) float
    }

    class VideoPlan {
        +title String
        +learningGoal String
        +mustCover String[]
        +outOfScope String[]
        +scenes Scene[]
    }

    class Scene {
        +id int
        +teachingBeat String
        +narration String
        +visualPrompt String
        +durationSeconds float
        +status String
    }

    class Manifest {
        +jobId String
        +title String
        +durationSeconds float
        +scenes SceneAsset[]
        +coverage String[]
    }

    ProductionClient --> FlaskAPI : HTTPS / polling
    FlaskAPI --> SupabaseStore : auth, quota, jobs, library
    FlaskAPI --> RenderWorker : local mode only
    RenderWorker --> SupabaseStore : claim, heartbeat, persist
    RenderWorker --> JobRuntimeConfig : activates
    RenderWorker --> Director : plans
    RenderWorker --> SegmentPipeline : renders
    Director --> LLMProvider
    SegmentPipeline --> VisualEngine
    SegmentPipeline --> TTSEngine
    SegmentPipeline --> VideoEngine
    VisualEngine --> StoryboardEngine
    VisualEngine --> LLMProvider
    LLMProvider --> APIKeyPool
    Director --> VideoPlan : creates
    VideoPlan *-- Scene
    SegmentPipeline --> Manifest : creates
    SupabaseStore --> Manifest : stores
```

## 4. High-Level System Architecture

```mermaid
flowchart TB
    user[Web user]

    subgraph edge[Delivery layer]
        https[HTTPS / reverse proxy]
        spa[React + Vite single-page application]
    end

    subgraph app[Application layer]
        api[Flask API\nAuth · validation · quota · job APIs]
        local[Optional in-process executor\nDevelopment mode]
        worker[Render worker pool\nQueue claim · heartbeat · cancellation]
    end

    subgraph data[Supabase platform]
        auth[Supabase Auth]
        db[(Postgres\nprofiles · plans · jobs · scenes · videos · events)]
        storage[(Private object storage\nsource · audio · images · MP4 · manifests)]
        queue[Durable job queue\nPostgres claim with SKIP LOCKED]
    end

    subgraph external[Generation dependencies]
        models[LLM providers\nNVIDIA · Groq · OpenRouter · Cerebras]
        icons[Iconify API + local cache]
        voice[Local Piper TTS]
        media[FFmpeg / browser renderer]
    end

    user --> https --> spa
    spa -->|Bearer token + JSON / multipart| api
    api --> auth
    api --> db
    api --> queue
    api -->|signed media URLs| storage
    api -. local development .-> local
    local --> models
    local --> voice
    local --> media
    worker --> queue
    worker --> db
    worker --> storage
    worker --> models
    worker --> icons
    worker --> voice
    worker --> media
    storage -->|private/signed delivery| spa
```

## 5. Low-Level Generation Architecture

```mermaid
flowchart LR
    subgraph client[React client]
        composer[Composer]
        production[ProductionProvider / progress UI]
        player[BufferedScenePlayer]
        library[Library and video detail]
        apiClient[API client + auth token]
        composer --> apiClient
        apiClient --> production
        production --> player
        apiClient --> library
    end

    subgraph web[Flask web container]
        routes[API routes]
        security[Auth · ownership · rate limit · quota]
        store[SupabaseStore adapter]
        routes --> security --> store
    end

    subgraph persistence[Supabase]
        auth[Auth]
        jobs[(video_jobs + generation_events)]
        catalog[(profiles + plans + subscriptions + usage)]
        metadata[(videos + video_scenes + video_assets)]
        objects[(private storage buckets)]
    end

    subgraph worker[Worker container]
        claim[Job claimer + heartbeat + cancellation watcher]
        runtime[JobRuntimeConfig]
        orchestrator[Job orchestrator]
        extract[Document extraction]
        director[Director + narration compiler]
        quality[Explanation / continuity / quantity quality gates]
        pipeline[Segment pipeline]
        assembler[Incremental video assembler]
        report[Manifest + quality report + run log]

        claim --> runtime --> orchestrator
        orchestrator --> extract --> director --> quality --> pipeline
        pipeline --> assembler --> report
    end

    subgraph scene[Per-scene parallel pipeline]
        narration[TTS engine\nPiper + word timing]
        visual[Visual engine\nLLM plan + deterministic storyboard]
        validate[Visual quality + freeform repair]
        animate[Animated scene + camera engine]
        raster[SVG / frames to PNG]
        encode[FFmpeg scene MP4 + captions]
        narration --> animate
        visual --> validate --> animate
        animate --> raster --> encode
    end

    subgraph ai[Provider abstraction]
        config[LLMConfig / role routing]
        keys[APIKeyPool\nreservation · limits · quarantine]
        providers[Provider APIs]
        config --> keys --> providers
    end

    apiClient --> routes
    security --> auth
    security --> catalog
    store --> jobs
    store --> metadata
    store --> objects
    claim --> jobs
    orchestrator --> store
    director --> config
    visual --> config
    pipeline --> narration
    pipeline --> visual
    encode --> assembler
    report --> metadata
    report --> objects
    jobs -. 1-second polling .-> production
    objects -. signed scene/media URLs .-> player
    objects -. completed assets .-> library
```

## 6. Proposed Project Timeline

```mermaid
gantt
    title Proposed 12-Week Delivery Plan — PencilVid / Simi Studio
    dateFormat  YYYY-MM-DD
    axisFormat  %d %b
    excludes    weekends

    section Discovery and design
    Requirements, use cases, success metrics       :a1, 2026-09-21, 5d
    UX flows and system architecture                :a2, after a1, 5d
    Architecture sign-off                           :milestone, m1, after a2, 0d

    section Platform foundation
    Supabase schema, Auth, RLS, storage              :b1, 2026-09-28, 10d
    React application shell and API client           :b2, 2026-09-28, 10d
    Flask API, validation, quotas, durable queue      :b3, 2026-10-05, 10d
    Foundation complete                              :milestone, m2, after b3, 0d

    section Generation pipeline
    Director, scope lock, narration planning          :c1, 2026-10-12, 10d
    TTS, timing, captions, duration calibration        :c2, 2026-10-19, 10d
    Visual planning, storyboard, SVG quality gates     :c3, 2026-10-19, 15d
    Scene animation, rendering, FFmpeg assembly        :c4, 2026-10-26, 15d
    End-to-end generation available                    :milestone, m3, after c4, 0d

    section Product integration
    Progress polling and buffered scene playback       :d1, 2026-11-02, 10d
    Library, video detail, download, cancellation       :d2, 2026-11-09, 10d
    Plans, quota UX, account lifecycle                  :d3, 2026-11-09, 10d

    section Verification and launch
    Automated unit, integration, security tests         :e1, 2026-10-19, 35d
    Load, failure recovery, and media quality testing   :e2, 2026-11-16, 10d
    Production payment webhook and operational review   :e3, 2026-11-16, 10d
    Deployment rehearsal, monitoring, runbooks          :e4, 2026-11-23, 5d
    Release candidate                                   :milestone, m4, 2026-11-27, 0d
    Pilot and stabilization                             :e5, 2026-11-30, 5d
    Production launch                                   :milestone, m5, 2026-12-04, 0d
```

