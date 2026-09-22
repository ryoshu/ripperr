import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

const apiServer = (
  globalThis as typeof globalThis & { process?: { env?: Record<string, string | undefined> } }
).process?.env?.RIPPERR_API_SERVER ?? "http://127.0.0.1:8765"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    allowedHosts: ["nyarlathotep.taile4827e.ts.net"],
    proxy: {
      "/healthz": apiServer,
      "/v1": apiServer,
    },
  },
})
