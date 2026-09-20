import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import { replayPlugin } from './server/replay.ts'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), replayPlugin()],
})
