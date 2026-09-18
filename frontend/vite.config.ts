import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  base: '/',
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:5000',
        changeOrigin: true,
      },
      '/socket.io': {
        target: 'http://127.0.0.1:5000',
        changeOrigin: true,
        ws: true,
      },
      '/media': {
        target: 'http://127.0.0.1:5000',
        changeOrigin: true,
      },
      '/segments': {
        target: 'http://127.0.0.1:5000',
        changeOrigin: true,
      },
      '/outputs': {
        target: 'http://127.0.0.1:5000',
        changeOrigin: true,
      },
      '/stream': {
        target: 'http://127.0.0.1:5000',
        changeOrigin: true,
      },
      '/generate': {
        target: 'http://127.0.0.1:5000',
        changeOrigin: true,
      },
    },
  },
})
