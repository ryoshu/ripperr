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
} from "@heroui/react"
import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react"
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

function youtubeEmbedUrl(value: string) {
  try {
    const url = new URL(value)
    const host = url.hostname.toLowerCase()
    const videoId = host === "youtu.be"
      ? url.pathname.slice(1)
      : ["youtube.com", "www.youtube.com", "m.youtube.com"].includes(host)
        ? url.searchParams.get("v")
        : null
    return videoId ? `https://www.youtube-nocookie.com/embed/${encodeURIComponent(videoId)}` : null
  } catch {
    return null
  }
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

function Metadata({ episode }: { episode: EpisodeDetail }) {
  const embedUrl = episode.audio_url ? youtubeEmbedUrl(episode.audio_url) : null

  return (
    <dl className="metadata">
      <div><dt>Feed</dt><dd>{episode.feed.title || episode.feed.url}</dd></div>
      <div><dt>Published</dt><dd>{formatDate(episode.published)}</dd></div>
      <div><dt>Duration</dt><dd>{formatDuration(episode.duration)}</dd></div>
      <div><dt>Updated</dt><dd>{formatDate(episode.updated_at)}</dd></div>
      <div><dt>Revision</dt><dd>{episode.revision}</dd></div>
      <div><dt>Source GUID</dt><dd className="mono">{episode.source_guid}</dd></div>
      {episode.audio_url && <div className="metadata-media"><dt>Listen</dt><dd>{embedUrl ? <iframe title={`Play ${titleFor(episode)}`} src={embedUrl} allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowFullScreen /> : <audio controls preload="metadata" src={episode.audio_url}>Your browser does not support audio playback.</audio>}</dd></div>}
      {episode.source_url && <div><dt>Source</dt><dd><Link href={episode.source_url} isExternal showAnchorIcon>{episode.source_url}</Link></dd></div>}
    </dl>
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
    <Card shadow="sm" className="panel">
      <CardHeader className="panel-header"><div><h2>Speaker names</h2><p className="muted">Episode-scoped display names; raw diarization labels stay unchanged.</p></div><Chip variant="flat">{names.length} mapped</Chip></CardHeader>
      <Divider />
      <CardBody>
        <ErrorNotice message={error} />
        {labels.length ? <div className="speaker-list">{labels.map((speaker) => (
          <div className="speaker-row" key={speaker}>
            <span className="speaker-label">{speaker}</span>
            <Input aria-label={`Display name for ${speaker}`} placeholder="Display name" value={drafts[speaker] || ""} onValueChange={(value) => setDrafts((current) => ({ ...current, [speaker]: value }))} />
            <Button size="sm" color="primary" isLoading={saving === speaker} isDisabled={saving !== null} onPress={() => void save(speaker)}>Save</Button>
            {mapped[speaker] && <Button size="sm" variant="light" color="danger" isDisabled={saving !== null} onPress={() => void clear(speaker)}>Clear</Button>}
          </div>
        ))}</div> : <p className="empty">No speaker labels are available yet.</p>}
      </CardBody>
    </Card>
  )
}

function EpisodeDetailView({ episode, error, onBack, onReload }: { episode: EpisodeDetail; error: string | null; onBack: () => void; onReload: () => Promise<void> }) {
  const [names, setNames] = useState(episode.speaker_names)
  const [showCorrections, setShowCorrections] = useState(false)
  const speakerLabels = Object.fromEntries(names.map((item) => [item.speaker, item.name]))
  useEffect(() => setNames(episode.speaker_names), [episode.guid, episode.speaker_names])

  return (
    <div className="stack">
      <Button variant="light" className="back-button" onPress={onBack}>← Back to episodes</Button>
      <ErrorNotice message={error} />
      <div className="page-heading detail-heading">
        <div><p className="eyebrow">Episode detail</p><h1>{titleFor(episode)}</h1><p className="mono muted guid">{episode.guid}</p></div>
        <Chip color={statusColor(episode.status)} variant="flat">{episode.status}</Chip>
      </div>

      <Card shadow="sm" className="panel"><CardBody><Metadata episode={episode} /></CardBody></Card>

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

      <SpeakerEditor episode={{ ...episode, speaker_names: names }} onChange={setNames} />

      <Card shadow="sm" className="panel transcript-panel">
        <CardHeader className="panel-header"><div><h2>Transcript</h2><p className="muted">{episode.turns.length} turns</p></div><Button size="sm" variant="light" onPress={() => void onReload()}>Refresh</Button></CardHeader>
        <Divider />
        <CardBody className="flush-body">{episode.turns.length ? episode.turns.map((turn) => <div className="turn-row" key={turn.idx}><div className="turn-meta"><strong>{speakerLabels[turn.speaker] || turn.speaker}</strong>{speakerLabels[turn.speaker] && <span className="mono muted">{turn.speaker}</span>}<span className="muted">{formatDuration(turn.start)}–{formatDuration(turn.end)}</span></div><p>{turn.text}</p></div>) : <p className="empty">No transcript turns are available.</p>}</CardBody>
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
          {([ ["/", "Overview"], ["/feeds", "Feeds"], ["/episodes", "Episodes"] ] as const).map(([path, label]) => <Button key={path} size="sm" variant={location.section === (path === "/" ? "overview" : path.slice(1) as Section) ? "solid" : "light"} onPress={() => go(path)}>{label}</Button>)}
        </nav>
        <Button size="sm" variant="flat" onPress={() => void reload()} isLoading={loading}>Refresh</Button>
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
