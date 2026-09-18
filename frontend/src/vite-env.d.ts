/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_PUBLIC_DEMO_POSTER_URL?: string
  readonly VITE_PUBLIC_DEMO_TITLE?: string
  readonly VITE_PUBLIC_DEMO_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
