export type Feed = {
  id: number
  url: string
  title: string | null
}

export type Episode = {
  guid: string
  source_guid: string
  feed: Feed
  title: string | null
  summary: string | null
  published: string | null
  audio_url: string | null
  source_url: string | null
  duration: number | null
  status: "new" | "downloaded" | "done" | "error" | string
  error?: string | null
  updated_at: string
  revision: number
  merged_at: string | null
}

export type Turn = {
  idx: number
  speaker: string
  start: number
  end: number
  text: string
}

export type Correction = {
  heard: string
  fixed: string
  count: number
}

export type SpeakerName = {
  episode_guid: string
  speaker: string
  name: string
  method: string
  confidence: number | null
  updated_at: string
}

export type SpeakerMatch = {
  episode_guid: string
  speaker: string
  name: string
  score: number
  sample_count: number
}

export type GuestHint = {
  episode_guid: string
  name: string
  source: string
  evidence: string
  confidence: number
}

export type EpisodeDetail = Episode & {
  corrections: Correction[]
  speaker_names: SpeakerName[]
  speaker_matches: SpeakerMatch[]
  guest_hints: GuestHint[]
  turns: Turn[]
}

type EpisodePage = {
  episodes: Episode[]
  has_more: boolean
}

// The local dev server injects the token at build time. A built dashboard
// served by ripperr carries no secret: it asks once and keeps the token in
// this browser.
const TOKEN_KEY = "ripperr-token"

function storedToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? ""
  } catch {
    return ""
  }
}

function rememberToken(value: string) {
  try {
    if (value) localStorage.setItem(TOKEN_KEY, value)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    // Private windows may refuse storage; the token then lasts for this page only.
  }
}

let token = import.meta.env.VITE_RIPPERR_TOKEN || storedToken()

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const send = () => {
    const headers = new Headers(init?.headers)
    if (token) headers.set("Authorization", `Bearer ${token}`)
    if (init?.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json")
    return fetch(path, { ...init, headers })
  }
  let response = await send()
  if (response.status === 401) {
    const entered = window.prompt("Ripperr API token")?.trim() ?? ""
    if (entered) {
      token = entered
      response = await send()
      rememberToken(response.status === 401 ? "" : entered)
    }
  }
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    try {
      const body = await response.json() as { error?: string }
      if (body.error) message = body.error
    } catch {
      // Keep the HTTP status when the server did not return JSON.
    }
    throw new Error(message)
  }
  return response.status === 204 ? (undefined as T) : response.json() as Promise<T>
}

export type Worker = {
  name: string
  created_at: string
  last_seen: string | null
  lease_guid: string | null
}

export async function getWorkers() {
  return (await request<{ workers: Worker[] }>("/v1/workers")).workers
}

export const getHealth = () => request<{ ok: boolean; change_seq: number }>("/healthz")

export async function getFeeds() {
  return (await request<{ feeds: Feed[] }>("/v1/feeds")).feeds
}

export async function getEpisodes() {
  const page = await request<EpisodePage>("/v1/episodes?limit=1000")
  return page.episodes
}

export const getEpisode = (guid: string) =>
  request<EpisodeDetail>(`/v1/episodes/${encodeURIComponent(guid)}`)

export const addFeed = (feed: { url: string; title?: string }) =>
  request<{ feed: Feed }>("/v1/feeds", { method: "POST", body: JSON.stringify(feed) })

export const updateFeed = (id: number, feed: { url: string; title?: string }) =>
  request<{ feed: Feed }>(`/v1/feeds/${id}`, { method: "PUT", body: JSON.stringify(feed) })

export const deleteFeed = (id: number) =>
  request<{ deleted: number }>(`/v1/feeds/${id}`, { method: "DELETE" })

export const updateSpeaker = (guid: string, speaker: string, name: string) =>
  request<{ speaker_name: SpeakerName }>(
    `/v1/episodes/${encodeURIComponent(guid)}/speakers/${encodeURIComponent(speaker)}`,
    { method: "PUT", body: JSON.stringify({ name }) },
  )

export const deleteSpeaker = (guid: string, speaker: string) =>
  request<{ deleted: string }>(
    `/v1/episodes/${encodeURIComponent(guid)}/speakers/${encodeURIComponent(speaker)}`,
    { method: "DELETE" },
  )
