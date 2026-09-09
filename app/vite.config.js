import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The build lands in app/static/, which FastAPI serves. One service, one URL,
// no CORS in production. During development `npm run dev` proxies /api to the
// Python server on 8000 so the same fetch paths work in both places.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'static',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})