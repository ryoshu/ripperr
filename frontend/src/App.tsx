import {
  Button,
  Card,
  CardBody,
  CardHeader,
  Chip,
  Divider,
  Input,
  Link,
  Modal,
  ModalBody,
  ModalContent,
  ModalFooter,
  ModalHeader,
  Spinner,
  Textarea,
  Tooltip,
} from "@heroui/react"
import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react"
import { useRef } from "react"
import {
  addFeed,
  deleteFeed,
  deleteSpeaker,
  getEpisode,
  getEpisodes,
  getFeeds,
  getHealth,
  updateFeed,
  updateSpeaker,
  type Episode,
  type EpisodeDetail,
  type Feed,
  type SpeakerName,
} from "./api"

type Section = "overview" | "feeds" | "episodes"

function readLocation(): { section: Section; guid: string | null } {
  const path = window.location.pathname.replace(/\/+$/, "") || "/"
  const match = path.match(/^\/episodes\/(.+)$/)
  if (match) return { section: "episodes", guid: decodeURIComponent(match[1]) }
  if (path === "/feeds") return { section: "feeds", guid: null }
  if (path === "/episodes") return { section: "episodes", guid: null }
  return { section: "overview", guid: null }
}

function navigate(path: string) {
  window.history.pushState({}, "", path)
  window.dispatchEvent(new PopStateEvent("popstate"))
}

function formatDate(value: string | null | undefined) {
  if (!value) return "—"
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString()
}

function formatDuration(value: number | null | undefined) {
  if (value == null) return "—"
  const total = Math.max(0, Math.round(value))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  return hours ? `${hours}h ${minutes}m` : `${minutes}m ${seconds}s`
}

function youtubeVideoId(value: string) {
  try {
    const url = new URL(value)
    const host = url.hostname.toLowerCase()
    return host === "youtu.be"
      ? url.pathname.slice(1)
      : ["youtube.com", "www.youtube.com", "m.youtube.com"].includes(host)
        ? url.searchParams.get("v")
        : null
  } catch {
    return null
  }
}

type YouTubePlayer = {
  getCurrentTime: () => number
  seekTo: (seconds: number, allowSeekAhead: boolean) => void
  destroy: () => void
}

type YouTubeApi = {
  Player: new (element: HTMLElement, options: {
    videoId: string
    playerVars?: Record<string, number>
    events?: { onReady?: () => void }
  }) => YouTubePlayer
}

declare global {
  interface Window {
    YT?: YouTubeApi
    onYouTubeIframeAPIReady?: () => void
  }
}

let youtubeApiPromise: Promise<YouTubeApi> | null = null

function loadYouTubeApi() {
  if (window.YT?.Player) return Promise.resolve(window.YT)
  if (youtubeApiPromise) return youtubeApiPromise

  youtubeApiPromise = new Promise<YouTubeApi>((resolve, reject) => {
    const previousReady = window.onYouTubeIframeAPIReady
    window.onYouTubeIframeAPIReady = () => {
      previousReady?.()
      if (window.YT?.Player) resolve(window.YT)
      else reject(new Error("YouTube Player API did not initialize"))
    }
    const existing = document.querySelector<HTMLScriptElement>('script[src="https://www.youtube.com/iframe_api"]')
    if (!existing) {
      const script = document.createElement("script")
      script.src = "https://www.youtube.com/iframe_api"
      script.async = true
      script.onerror = () => reject(new Error("Unable to load YouTube Player API"))
      document.head.append(script)
    }
  })

  return youtubeApiPromise
}

type MediaController = { seekTo: (seconds: number) => void }

function SyncedMedia({
  audioUrl,
  title,
  onControllerReady,
  onTimeUpdate,
}: {
  audioUrl: string
  title: string
  onControllerReady: (controller: MediaController | null) => void
  onTimeUpdate: (seconds: number) => void
}) {
  const videoId = youtubeVideoId(audioUrl)
  const audioRef = useRef<HTMLAudioElement>(null)
  const playerHostRef = useRef<HTMLDivElement>(null)
  const playerRef = useRef<YouTubePlayer | null>(null)
  const pollRef = useRef<number | null>(null)

  useEffect(() => {
    if (!videoId) {
      onControllerReady({ seekTo: (seconds) => {
        if (audioRef.current) audioRef.current.currentTime = seconds
      } })
      return () => onControllerReady(null)
    }

    let cancelled = false
    onControllerReady({ seekTo: (seconds) => playerRef.current?.seekTo(seconds, true) })
    loadYouTubeApi()
      .then((api) => {
        if (cancelled || !playerHostRef.current) return
        playerRef.current = new api.Player(playerHostRef.current, {
          videoId,
          playerVars: { controls: 1, playsinline: 1, rel: 0 },
          events: {
            onReady: () => {
              onTimeUpdate(playerRef.current?.getCurrentTime() ?? 0)
              pollRef.current = window.setInterval(() => {
                if (playerRef.current) onTimeUpdate(playerRef.current.getCurrentTime())
              }, 250)
            },
          },
        })
      })
      .catch(() => {
        if (!cancelled) onControllerReady(null)
      })

    return () => {
      cancelled = true
      if (pollRef.current !== null) window.clearInterval(pollRef.current)
      pollRef.current = null
      playerRef.current?.destroy()
      playerRef.current = null
      onControllerReady(null)
    }
  }, [videoId, onControllerReady, onTimeUpdate])

  if (!videoId) {
    return <audio ref={audioRef} controls preload="metadata" src={audioUrl} onTimeUpdate={(event) => onTimeUpdate(event.currentTarget.currentTime)}>{"Your browser does not support audio playback."}</audio>
  }
  return <div ref={playerHostRef} className="youtube-player" title={`Play ${title}`} />
}

function titleFor(item: Episode) {
  return item.title || "Untitled episode"
}

function statusColor(status: string): "success" | "warning" | "danger" | "default" {
  if (status === "done") return "success"
  if (status === "error") return "danger"
  if (status === "downloaded") return "warning"
  return "default"
}

function ErrorNotice({ message }: { message: string | null }) {
  if (!message) return null
  return <div className="notice notice-error">{message}</div>
}

function Loading() {
  return <div className="loading"><Spinner size="sm" /> Loading…</div>
}

function CloseIcon() {
  return <svg aria-hidden="true" className="icon" viewBox="0 0 20 20" fill="none"><path d="m5 5 10 10M15 5 5 15" stroke="currentColor" strokeLinecap="round" strokeWidth="1.8" /></svg>
}

function StatCard({ label, value, tone = "" }: { label: string; value: number | string; tone?: string }) {
  return (
    <Card shadow="sm" className={`stat-card ${tone}`}>
      <CardBody>
        <p className="eyebrow">{label}</p>
        <p className="stat-value">{value}</p>
      </CardBody>
    </Card>
  )
}

function EpisodeRow({ episode, onOpen }: { episode: Episode; onOpen: () => void }) {
  return (
    <button className="episode-row" type="button" onClick={onOpen}>
      <div className="episode-row-main">
        <div className="episode-title-line">
          <span className="episode-title">{titleFor(episode)}</span>
          <Chip size="sm" variant="flat" color={statusColor(episode.status)}>{episode.status}</Chip>
        </div>
        <span className="episode-feed">{episode.feed.title || episode.feed.url}</span>
      </div>
      <div className="episode-row-meta">
        <span>{formatDate(episode.published)}</span>
        <span>{formatDuration(episode.duration)}</span>
      </div>
    </button>
  )
}

function Overview({
  feeds,
  episodes,
  health,
  onOpenEpisode,
  onNavigate,
}: {
  feeds: Feed[]
  episodes: Episode[]
  health: { ok: boolean; change_seq: number } | null
  onOpenEpisode: (guid: string) => void
  onNavigate: (path: string) => void
}) {
  const counts = useMemo(() => ({
    done: episodes.filter((item) => item.status === "done").length,
    pending: episodes.filter((item) => item.status === "new" || item.status === "downloaded").length,
    errors: episodes.filter((item) => item.status === "error").length,
  }), [episodes])
  const recent = [...episodes].sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, 6)

  return (
    <div className="stack">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Workspace</p>
          <h1>Ripperr at a glance</h1>
          <p className="muted">A small control room for feeds, processing state, and transcripts.</p>
        </div>
        <Chip color={health?.ok ? "success" : "danger"} variant="flat">
          {health?.ok ? "Local API online" : "API unavailable"}
        </Chip>
      </div>

      <div className="stats-grid">
        <StatCard label="Feeds" value={feeds.length} />
        <StatCard label="Episodes" value={episodes.length} />
        <StatCard label="Transcribed" value={counts.done} tone="stat-success" />
        <StatCard label="Needs attention" value={counts.errors} tone={counts.errors ? "stat-danger" : ""} />
      </div>

      <div className="content-grid">
        <Card shadow="sm" className="panel">
          <CardHeader className="panel-header">
            <div>
              <h2>Recent episodes</h2>
              <p className="muted">Latest database updates</p>
            </div>
            <Button size="sm" variant="flat" onPress={() => onNavigate("/episodes")}>View all</Button>
          </CardHeader>
          <Divider />
          <CardBody className="flush-body">
            {recent.length ? recent.map((episode) => (
              <EpisodeRow key={episode.guid} episode={episode} onOpen={() => onOpenEpisode(episode.guid)} />
            )) : <p className="empty">No episodes yet. Add a feed to get started.</p>}
          </CardBody>
        </Card>

        <Card shadow="sm" className="panel">
          <CardHeader className="panel-header">
            <div>
              <h2>Pipeline</h2>
              <p className="muted">Current queue at a glance</p>
            </div>
            <span className="change-seq">seq {health?.change_seq ?? "—"}</span>
          </CardHeader>
          <Divider />
          <CardBody className="pipeline-body">
            <div className="pipeline-stat"><span>Ready to process</span><strong>{counts.pending}</strong></div>
            <div className="pipeline-stat"><span>Completed</span><strong>{counts.done}</strong></div>
            <div className="pipeline-stat"><span>Errors</span><strong className={counts.errors ? "danger-text" : ""}>{counts.errors}</strong></div>
            <p className="muted small">Processing remains a CLI/API operation for now; this view focuses on inspection and feed administration.</p>
          </CardBody>
        </Card>
      </div>
    </div>
  )
}

function FeedForm({
  initial,
  onSave,
  onCancel,
}: {
  initial?: Feed
  onSave: (feed: { url: string; title?: string }) => Promise<void>
  onCancel?: () => void
}) {
  const [url, setUrl] = useState(initial?.url ?? "")
  const [title, setTitle] = useState(initial?.title ?? "")
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSaving(true)
    setError(null)
    try {
      await onSave({ url: url.trim(), ...(title.trim() ? { title: title.trim() } : {}) })
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to save feed")
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="feed-form" onSubmit={submit}>
      <Input label="Feed URL" placeholder="https://example.com/feed.xml" value={url} onValueChange={setUrl} type="url" isRequired />
      <Input label="Title" placeholder="Optional display name" value={title} onValueChange={setTitle} />
      <ErrorNotice message={error} />
      <div className="form-actions">
        {onCancel && <Button type="button" variant="light" onPress={onCancel}>Cancel</Button>}
        <Button color="primary" type="submit" isLoading={saving}>{initial ? "Save changes" : "Add feed"}</Button>
      </div>
    </form>
  )
}

function FeedsView({ feeds, episodes, onReload }: { feeds: Feed[]; episodes: Episode[]; onReload: () => Promise<void> }) {
  const [editing, setEditing] = useState<Feed | null>(null)
  const [deleting, setDeleting] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function create(feed: { url: string; title?: string }) {
    await addFeed(feed)
    await onReload()
  }

  async function save(feed: { url: string; title?: string }) {
    if (!editing) return
    await updateFeed(editing.id, feed)
    setEditing(null)
    await onReload()
  }

  async function remove(feed: Feed) {
    if (!window.confirm(`Delete ${feed.title || feed.url}? This also removes its stored episodes.`)) return
    setDeleting(feed.id)
    setError(null)
    try {
      await deleteFeed(feed.id)
      await onReload()
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to delete feed")
    } finally {
      setDeleting(null)
    }
  }

  return (
    <div className="stack">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Administration</p>
          <h1>Feeds</h1>
          <p className="muted">Manage the RSS and YouTube sources Ripperr polls.</p>
        </div>
        <Chip variant="flat">{feeds.length} registered</Chip>
      </div>

      <div className="content-grid feeds-grid">
        <Card shadow="sm" className="panel">
          <CardHeader><h2>Add a feed</h2></CardHeader>
          <Divider />
          <CardBody><FeedForm onSave={create} /></CardBody>
        </Card>

        <Card shadow="sm" className="panel">
          <CardHeader className="panel-header">
            <div><h2>Registered sources</h2><p className="muted">{episodes.length} episodes discovered</p></div>
          </CardHeader>
          <Divider />
          <CardBody className="flush-body">
            <ErrorNotice message={error} />
            {feeds.length ? feeds.map((feed) => {
              const count = episodes.filter((episode) => episode.feed.id === feed.id).length
              return (
                <div className="feed-row" key={feed.id}>
                  <div className="feed-row-main">
                    <div className="feed-name-line"><strong>{feed.title || "Untitled feed"}</strong><Chip size="sm" variant="flat">{count} episodes</Chip></div>
                    <Link href={feed.url} isExternal showAnchorIcon className="feed-url">{feed.url}</Link>
                  </div>
                  <div className="row-actions">
                    <Button size="sm" variant="light" onPress={() => setEditing(feed)}>Edit</Button>
                    <Button size="sm" variant="light" color="danger" isLoading={deleting === feed.id} onPress={() => void remove(feed)}>Delete</Button>
                  </div>
                </div>
              )
            }) : <p className="empty">No feeds registered yet.</p>}
          </CardBody>
        </Card>
      </div>

      <Modal isOpen={Boolean(editing)} onOpenChange={(open) => !open && setEditing(null)}>
        <ModalContent>
          {(close) => <>
            <ModalHeader>Edit feed</ModalHeader>
            <ModalBody>{editing && <FeedForm initial={editing} onSave={save} onCancel={close} />}</ModalBody>
            <ModalFooter />
          </>}
        </ModalContent>
      </Modal>
    </div>
  )
}

function EpisodesView({
  episodes,
  selectedGuid,
  onOpen,
  onBack,
  onReload,
}: {
  episodes: Episode[]
  selectedGuid: string | null
  onOpen: (guid: string) => void
  onBack: () => void
  onReload: () => Promise<void>
}) {
  const [query, setQuery] = useState("")
  const [status, setStatus] = useState("all")
  const [detail, setDetail] = useState<EpisodeDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!selectedGuid) {
      setDetail(null)
      return
    }
    setDetail(null)
    setLoading(true)
    setError(null)
    getEpisode(selectedGuid)
      .then(setDetail)
      .catch((err) => setError(err instanceof Error ? err.message : "Unable to load episode"))
      .finally(() => setLoading(false))
  }, [selectedGuid])

  if (selectedGuid) {
    if (loading && !detail) return <Loading />
    if (!detail) return <><ErrorNotice message={error} /><Button variant="light" onPress={onBack}>Back to episodes</Button></>
    return <EpisodeDetailView episode={detail} error={error} onBack={onBack} onReload={async () => { await onReload(); const fresh = await getEpisode(detail.guid); setDetail(fresh) }} />
  }

  const filtered = episodes
    .filter((episode) => status === "all" || episode.status === status)
    .filter((episode) => {
      const haystack = `${episode.title ?? ""} ${episode.feed.title ?? ""} ${episode.feed.url}`.toLowerCase()
      return haystack.includes(query.toLowerCase())
    })
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))

  return (
    <div className="stack">
      <div className="page-heading">
        <div><p className="eyebrow">Administration</p><h1>Episodes</h1><p className="muted">Review processing state and open transcript detail.</p></div>
        <Chip variant="flat">{episodes.length} total</Chip>
      </div>
      <Card shadow="sm" className="panel">
        <CardBody>
          <div className="filter-row">
            <Input aria-label="Search episodes" placeholder="Search title or feed…" value={query} onValueChange={setQuery} className="search-input" />
            <div className="status-filters">
              {(["all", "new", "downloaded", "done", "error"] as const).map((value) => (
                <Button key={value} size="sm" variant={status === value ? "solid" : "light"} color={value === "error" ? "danger" : "default"} onPress={() => setStatus(value)}>{value === "all" ? "All" : value}</Button>
              ))}
            </div>
          </div>
        </CardBody>
        <Divider />
        <CardBody className="flush-body">
          {filtered.length ? filtered.map((episode) => <EpisodeRow key={episode.guid} episode={episode} onOpen={() => onOpen(episode.guid)} />) : <p className="empty">No matching episodes.</p>}
        </CardBody>
      </Card>
    </div>
  )
}

function Metadata({ episode, speakerEditor, onControllerReady, onTimeUpdate }: { episode: EpisodeDetail; speakerEditor: ReactNode; onControllerReady: (controller: MediaController | null) => void; onTimeUpdate: (seconds: number) => void }) {
  return (
    <div className={`metadata-layout${episode.audio_url ? " has-media" : ""}`}>
      {episode.audio_url && <div className="metadata-media"><p className="metadata-label">Listen</p><SyncedMedia audioUrl={episode.audio_url} title={titleFor(episode)} onControllerReady={onControllerReady} onTimeUpdate={onTimeUpdate} /></div>}
      <div className="metadata-side">
        <dl className="metadata">
          <div><dt>Published</dt><dd>{formatDate(episode.published)}</dd></div>
          <div><dt>Duration</dt><dd>{formatDuration(episode.duration)}</dd></div>
          <div><dt>Updated</dt><dd>{formatDate(episode.updated_at)}</dd></div>
          <div><dt>Revision</dt><dd>{episode.revision}</dd></div>
          {episode.source_url && <div className="metadata-source"><dt>Source</dt><dd><Link href={episode.source_url} isExternal showAnchorIcon>{episode.source_url}</Link></dd></div>}
        </dl>
        {speakerEditor}
      </div>
    </div>
  )
}

function SpeakerEditor({ episode, onChange }: { episode: EpisodeDetail; onChange: (names: SpeakerName[]) => void }) {
  const [names, setNames] = useState(episode.speaker_names)
  const [drafts, setDrafts] = useState<Record<string, string>>(() => Object.fromEntries(names.map((item) => [item.speaker, item.name])))
  const [saving, setSaving] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const labels = Array.from(new Set(episode.turns.map((turn) => turn.speaker))).filter((speaker) => speaker !== "SPEAKER_?")
  const mapped = Object.fromEntries(names.map((item) => [item.speaker, item]))

  useEffect(() => {
    setNames(episode.speaker_names)
    setDrafts(Object.fromEntries(episode.speaker_names.map((item) => [item.speaker, item.name])))
  }, [episode.guid, episode.speaker_names])

  async function save(speaker: string) {
    const name = (drafts[speaker] || "").trim()
    if (!name) { setError("Enter a display name before saving."); return }
    setSaving(speaker)
    setError(null)
    try {
      const result = await updateSpeaker(episode.guid, speaker, name)
      const next = [...names.filter((item) => item.speaker !== speaker), result.speaker_name]
      setNames(next)
      onChange(next)
    } catch (err) { setError(err instanceof Error ? err.message : "Unable to save speaker name") }
    finally { setSaving(null) }
  }

  async function clear(speaker: string) {
    if (!window.confirm(`Clear the display name for ${speaker}?`)) return
    setSaving(speaker)
    setError(null)
    try {
      await deleteSpeaker(episode.guid, speaker)
      const next = names.filter((item) => item.speaker !== speaker)
      setNames(next)
      setDrafts((current) => ({ ...current, [speaker]: "" }))
      onChange(next)
    } catch (err) { setError(err instanceof Error ? err.message : "Unable to clear speaker name") }
    finally { setSaving(null) }
  }

  return (
    <section className="speaker-editor">
      <div className="speaker-editor-heading"><div><h2>Speaker names</h2><p className="muted">Episode-scoped display names; raw diarization labels stay unchanged.</p></div><Chip variant="flat">{names.length} mapped</Chip></div>
      <Divider />
      <div className="speaker-editor-body">
        <ErrorNotice message={error} />
        {labels.length ? <div className="speaker-list">{labels.map((speaker) => (
          <div className="speaker-row" key={speaker}>
            <span className="speaker-label">{speaker}</span>
            <Input aria-label={`Display name for ${speaker}`} placeholder="Display name" value={drafts[speaker] || ""} onValueChange={(value) => setDrafts((current) => ({ ...current, [speaker]: value }))} />
            <Button size="sm" color="primary" isLoading={saving === speaker} isDisabled={saving !== null} onPress={() => void save(speaker)}>Save</Button>
            {mapped[speaker] && <Tooltip content={`Clear name for ${speaker}`}><Button className="speaker-clear" size="sm" variant="light" color="danger" isIconOnly aria-label={`Clear name for ${speaker}`} isDisabled={saving !== null} onPress={() => void clear(speaker)}><CloseIcon /></Button></Tooltip>}
          </div>
        ))}</div> : <p className="empty">No speaker labels are available yet.</p>}
      </div>
    </section>
  )
}

function EpisodeDetailView({ episode, error, onBack, onReload }: { episode: EpisodeDetail; error: string | null; onBack: () => void; onReload: () => Promise<void> }) {
  const [names, setNames] = useState(episode.speaker_names)
  const [showCorrections, setShowCorrections] = useState(false)
  const [activeTurn, setActiveTurn] = useState<number | null>(null)
  const mediaControllerRef = useRef<MediaController | null>(null)
  const speakerLabels = Object.fromEntries(names.map((item) => [item.speaker, item.name]))
  useEffect(() => setNames(episode.speaker_names), [episode.guid, episode.speaker_names])
  useEffect(() => {
    if (activeTurn === null) return
    document.querySelector<HTMLElement>(`[data-turn-idx="${activeTurn}"]`)?.scrollIntoView({ behavior: "smooth", block: "nearest" })
  }, [activeTurn])

  const handleTimeUpdate = useCallback((seconds: number) => {
    const turn = episode.turns.find((item) => seconds >= item.start && seconds < item.end)
    setActiveTurn((current) => current === (turn?.idx ?? null) ? current : (turn?.idx ?? null))
  }, [episode.turns])
  const handleControllerReady = useCallback((controller: MediaController | null) => {
    mediaControllerRef.current = controller
  }, [])

  return (
    <div className="stack">
      <Button variant="light" className="back-button" onPress={onBack}>← Back to episodes</Button>
      <ErrorNotice message={error} />
      <div className="page-heading detail-heading">
        <div><p className="eyebrow">{episode.feed.title || episode.feed.url}</p><h1>{titleFor(episode)}</h1></div>
        <Chip color={statusColor(episode.status)} variant="flat">{episode.status}</Chip>
      </div>

      <Card shadow="sm" className="panel"><CardBody><Metadata episode={episode} speakerEditor={<SpeakerEditor episode={{ ...episode, speaker_names: names }} onChange={setNames} />} onControllerReady={handleControllerReady} onTimeUpdate={handleTimeUpdate} /></CardBody></Card>

      <Card shadow="sm" className="panel transcript-panel">
        <CardHeader className="panel-header"><div><h2>Transcript</h2><p className="muted">{episode.turns.length} turns · click a turn to jump</p></div><Button size="sm" variant="light" onPress={() => void onReload()}>Refresh</Button></CardHeader>
        <Divider />
        <CardBody className="flush-body">{episode.turns.length ? episode.turns.map((turn) => <div className={`turn-row${activeTurn === turn.idx ? " is-active" : ""}`} data-turn-idx={turn.idx} key={turn.idx} role="button" tabIndex={0} onClick={() => mediaControllerRef.current?.seekTo(turn.start)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); mediaControllerRef.current?.seekTo(turn.start) } }}><div className="turn-meta"><strong>{speakerLabels[turn.speaker] || turn.speaker}</strong>{speakerLabels[turn.speaker] && <span className="mono muted">{turn.speaker}</span>}<span className="muted">{formatDuration(turn.start)}–{formatDuration(turn.end)}</span></div><p>{turn.text}</p></div>) : <p className="empty">No transcript turns are available.</p>}</CardBody>
      </Card>

      <Card shadow="sm" className="panel">
        <CardHeader className="panel-header">
          <h2>Glossary corrections</h2>
          <div className="row-actions">
            <Chip variant="flat">{episode.corrections.length}</Chip>
            <Button size="sm" variant="light" aria-expanded={showCorrections} onPress={() => setShowCorrections((open) => !open)}>
              {showCorrections ? "Hide" : "Show"}
            </Button>
          </div>
        </CardHeader>
        {showCorrections && <>
          <Divider />
          <CardBody>{episode.corrections.length ? <div className="correction-list">{episode.corrections.map((item) => <div className="correction-row" key={`${item.heard}-${item.fixed}`}><span className="struck">{item.heard}</span><span>→</span><strong>{item.fixed}</strong><span className="muted">×{item.count}</span></div>)}</div> : <p className="empty">No corrections for this revision.</p>}</CardBody>
        </>}
      </Card>
    </div>
  )
}

function App() {
  const [location, setLocation] = useState(readLocation)
  const [feeds, setFeeds] = useState<Feed[]>([])
  const [episodes, setEpisodes] = useState<Episode[]>([])
  const [health, setHealth] = useState<{ ok: boolean; change_seq: number } | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const onPopState = () => setLocation(readLocation())
    window.addEventListener("popstate", onPopState)
    return () => window.removeEventListener("popstate", onPopState)
  }, [])

  const reload = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [loadedFeeds, loadedEpisodes, loadedHealth] = await Promise.all([getFeeds(), getEpisodes(), getHealth()])
      setFeeds(loadedFeeds)
      setEpisodes(loadedEpisodes)
      setHealth(loadedHealth)
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load Ripperr")
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void reload() }, [reload])

  function go(path: string) { navigate(path) }

  return (
    <div className="app-shell">
      <header className="topbar">
        <button className="brand" type="button" onClick={() => go("/")}><span className="brand-mark">r</span><span>ripperr</span></button>
        <nav className="topnav" aria-label="Primary navigation">
          {([ ["/", "Overview"], ["/feeds", "Feeds"], ["/episodes", "Episodes"] ] as const).map(([path, label]) => {
            const active = location.section === (path === "/" ? "overview" : path.slice(1) as Section)
            return <Button key={path} size="sm" className={`topnav-button${active ? " is-active" : ""}`} variant={active ? "solid" : "light"} onPress={() => go(path)}>{label}</Button>
          })}
        </nav>
        <Button size="sm" className="topbar-refresh" variant="flat" onPress={() => void reload()} isLoading={loading}>Refresh</Button>
      </header>
      <main className="main-content">
        <ErrorNotice message={error} />
        {loading && !feeds.length && !episodes.length ? <Loading /> : location.section === "overview" ? <Overview feeds={feeds} episodes={episodes} health={health} onOpenEpisode={(guid) => go(`/episodes/${encodeURIComponent(guid)}`)} onNavigate={go} /> : location.section === "feeds" ? <FeedsView feeds={feeds} episodes={episodes} onReload={reload} /> : <EpisodesView episodes={episodes} selectedGuid={location.guid} onOpen={(guid) => go(`/episodes/${encodeURIComponent(guid)}`)} onBack={() => go("/episodes")} onReload={reload} />}
      </main>
      <footer className="footer"><span>Local dashboard</span><span>•</span><span>change sequence {health?.change_seq ?? "—"}</span></footer>
    </div>
  )
}

export default App
