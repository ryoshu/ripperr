import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"

const apiServer = (
  globalThis as typeof globalThis & { process?: { env?: Record<string, string | undefined> } }
).process?.env?.RIPPERR_API_SERVER ?? "http://127.0.0.1:8765"

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/healthz": apiServer,
      "/v1": apiServer,
    },
  },
})
