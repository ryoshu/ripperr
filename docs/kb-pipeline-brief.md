# Knowledge Base Pipeline Brief

Oct 4, 2026 · @Ricky Bacon · status updated Oct 3, 2026

Build the layer that turns ripperr transcripts (and later scraped pages) into a searchable, citable knowledge base that agents query over MCP, and wire SearXNG podcast discovery into ripperr's feed registry.

## Status

| Item | State |
| --- | --- |
| W1. Split compute from ripperr; run ripperr on the Mac Pro | Done |
| W2. Indexer | Done: running on the Mac Pro under launchd |
| W6. Evaluation set | Done: 22 questions; hit@5 0.91, hit@10 0.95, mean rank 1.7 |
| Backfill | Done: `ripperr add --backfill N`, `POST /v1/feeds {"backfill": N}` |
| W3. MCP retrieval server | Done: `https://rickys-mac-pro.taile4827e.ts.net:8443/mcp`, own bearer token |
| W4. Discovery to subscription | Done: `kb discover`; first real subscription still to make |
| W5. Scraper | Next |

## Context

The goal is a personal knowledge base: SearXNG finds podcasts worth keeping, ripperr transcribes them, and agents (Claude Code and others) query the result with citations.

Data flow, end to end:

1. SearXNG (`podcasts` category: fyyd, podchaser) surfaces shows and episodes of interest.
2. A discovery helper resolves a result to an RSS feed URL; a human confirms; `POST /v1/feeds` subscribes it in ripperr.
3. Ripperr on the Mac Pro syncs feeds, downloads audio and converts it to 16 kHz WAV (`ripperr sync && ripperr prepare`, hourly).
4. `ripperr-worker` on the M5 claims a prepared episode, runs mlx-whisper and Senko, and uploads the raw output. Ripperr merges it (glossary, speakers, identity), writes turns to SQLite and records a change event.
5. The indexer, on the same Mac Pro, polls ripperr's change feed, chunks transcripts, and builds a hybrid (keyword plus vector) index.
6. An MCP server exposes search and transcript-span tools that return chunks with speaker, timestamp and a source link.
7. Later, the scraper feeds web pages into the same index through the same document schema.

### Repos

Each pair of neighbours shares exactly one HTTP contract and no code.

| Repo | Runs on | Owns | Contract with its neighbours |
| --- | --- | --- | --- |
| `ryoshu/ripperr` | Mac Pro (API, sync, prepare); M5 (dashboard, for now) | Feeds, downloads, audio prep, store, merge, glossary, speaker identity, change feed, dashboard | `docs/api.md` for consumers; `docs/worker-contract.md` for workers |
| `ryoshu/ripperr-worker` | M5 | ASR and diarization only; no database | `docs/worker-contract.md` (schema 1) |
| `ryoshu/ripperr-knowledge-base` | Mac Pro | Index, eval set, MCP server, discovery | Ripperr's change feed and episode API; MCP for agents |

Code is edited on the laptop and pushed to GitHub. The Mac Pro has no GitHub credentials: ripperr is public and pulled there; the knowledge base is private and pushed to the Mac Pro's checkout directly (`git push macpro main`, which updates its working tree).

### Machines and services

| Machine | Tailnet name | Runs | Notes |
| --- | --- | --- | --- |
| `vps-anodyne` (Ubuntu 22.04, 4 CPU, 6 GB RAM) | 100.84.153.34 | SearXNG at `https://search.ryoshu.com`, tailnet-only via nginx; `mcp-searxng` for Claude Code | Also runs `home-proxy.service`, an SSH SOCKS tunnel on `127.0.0.1:1080` that exits through the Mac Pro's residential IP |
| `rickys-mac-pro` (Intel, 12 cores, 32 GB, macOS 12.7.6, user `hastur`) | `rickys-mac-pro.taile4827e.ts.net` | Ripperr API (`tailscale serve` 443 → `127.0.0.1:8876`), hourly `sync && prepare`, the indexer, the MCP server (8443 → `127.0.0.1:8877`). All launchd agents | Always on, sleep disabled. Python 3.11 (uv-managed), static ffmpeg 9 in `~/.local/bin`. No Homebrew or uv. Audio is deleted after merge (`RIPPERR_KEEP_AUDIO=0`) |
| M5 Pro laptop (48 GB) | `nyarlathotep.taile4827e.ts.net` | `ripperr-worker` (launchd, installed as a uv tool), the Vite dashboard on `127.0.0.1:5174` proxying to the Mac Pro | Sleeps; a lease left by a sleeping worker expires after 2 h. Serves only srchr on `:8443`; nothing on Funnel |

### Why these choices

- **Compute is the only thing that needs the M5.** Ripperr's pipeline already cached model output per stage, and `remerge` rebuilt a transcript from that cache with no models. So the worker produces only that output, and everything else moved to the always-on Mac Pro, which cannot run Senko (macOS 14+) or mlx (Apple Silicon) itself.
- **Workers poll; nothing connects to the M5.** The laptop needs no inbound access, and the API and change feed stay up while it sleeps.
- **Own index, not ripperr's FTS5.** Ripperr's `search()` is keyword-only over turns, returns raw per-episode speaker labels, and has no HTTP endpoint. It cannot do semantic retrieval or combine sources.
- **The knowledge base reads only ripperr's HTTP API.** Ripperr's `docs/api.md` names the change feed as the canonical sync contract and says everything else is internal and may change.

## What ripperr provides

| Need | Ripperr surface |
| --- | --- |
| Subscribe to feeds | `POST /v1/feeds` `{"url", "title", "backfill"}`, idempotent; also `GET`, `PUT /v1/feeds/{id}`, `DELETE`. RSS or YouTube playlist. `backfill` (1 to 100) also records the feed's N newest episodes |
| Durable sync stream | `GET /v1/changes?after=SEQ&limit=N`: ascending `seq`, events for metadata, transcript and deleted. Safe to replay a page |
| Full transcript | `GET /v1/episodes/{guid}`: metadata, `audio_url`, `source_url`, `revision`, ordered turns (`idx`, `start`, `end`, `speaker`, text), `corrections`, `speaker_names`, `speaker_matches`, `guest_hints`. ETag plus `If-None-Match` returns 304 |
| Episode list | `GET /v1/episodes`: metadata and status only, no turns |
| Health and cursor | `GET /healthz` returns `ok` and the highest `change_seq` |
| New-consumer bootstrap | `POST /v1/changes/bootstrap` emits the current revision of every completed episode |
| Speaker display names | `PUT` and `DELETE /v1/episodes/{guid}/speakers/{speaker}` |
| Model work | `/v1/work/claim`, `/audio`, `/result`, `/fail`: see `docs/worker-contract.md` |

The server has a token configured, so every request needs `Authorization: Bearer`.

### Constraints that shape the design

- **Loopback only.** The server rejects non-loopback binds because bearer tokens do not encrypt traffic. Remote access goes through `tailscale serve`, which terminates TLS.
- **Revisions.** `revision` increases on every transcript rewrite, and `idx` values and text can change between revisions, so never keep turn positions across revisions. Metadata and speaker-name edits emit events without bumping `revision`, so re-read the episode on every metadata event.
- **Re-running models changes transcripts.** mlx-whisper is not deterministic across runs: a re-run of the same episode matched the original 96% on normalized words (punctuation, fillers, an occasional name). Avoid needless re-runs; `remerge` re-applies glossary and merge changes without them.
- **Speaker labels** (`SPEAKER_01`) are per episode and mean nothing across episodes. Display names live in a separate mapping, and `Hit.speaker` from `search()` does not apply it.
- **No summaries or chapters.** `Episode.summary` is only the feed's own description.
- **New feeds.** Without `backfill`, a new feed takes only its newest episode; later syncs take only entries newer than the newest known one.
- **Cursor loss.** A cursor older than the retained log gives `410 Gone` with `reset: true`. Recover with bootstrap, treat the re-emitted set as authoritative, and drop local episodes not re-emitted. Bootstrap appends one event per completed episode for every consumer, so do not call it repeatedly.
- **Feed validation.** Authenticated feed writes reject literal local or private hosts but do not resolve DNS or follow redirects, so this is not SSRF protection. Direct audio downloads are capped at 1 GiB.
- **One embedding space.** Ripperr matches speaker profiles only within `RIPPERR_EMBEDDING_SPACE` (`senko-campplus`); a worker with a different diarization model has its speaker vectors dropped.
- **Pre-1.0.** Both packages are 0.x. Rely only on the documented HTTP surfaces.

## Work items

### W1. Split compute from ripperr and move ripperr to the Mac Pro (done)

Ripperr runs everything except model inference; `ripperr-worker` runs ASR and diarization. The contract is `docs/worker-contract.md`: workers claim leased episodes, fetch the WAV, and upload model output with its model identity; ripperr validates it, applies it at most once per lease, and merges it. Each transcript records which model run produced it, so `remerge` and `prune_cache` work on a host with no models.

Verified end to end: an episode prepared on the Mac Pro was transcribed by the M5 worker, merged on the Mac Pro, and indexed. A worker re-run of an existing episode produced the same cache keys and, fed the original model output, the same words and diarization.

Ripperr no longer contains any model code: `ripperr run`, the in-process processor, `asr.py`, `diarize.py` and the `apple` and `linux` extras are gone, and `remerge` reads only cached worker output. Later, if wanted, serve the built dashboard from the Mac Pro instead of the laptop's Vite dev server.

### W2. Indexer service (done)

Runs on the Mac Pro as `com.ryoshu.ripperr-knowledge-base.indexer`, reading ripperr at `http://127.0.0.1:8876`.

- **Sync loop.** Polls `GET /v1/changes?after=<cursor>&limit=100`. The cursor is stored in the indexer's own SQLite database in the same transaction as the writes it covers, so a crash never advances past unprocessed events.
- **Per event.** For a transcript event, `GET /v1/episodes/{guid}` with `If-None-Match`; if the revision is newer than the stored one, delete that episode's chunks and insert the new set in one transaction. For a metadata event, re-read and update metadata and speaker names. For a deleted event, remove everything for that guid.
- **Reset.** On `410` with `reset: true`, call bootstrap and record both `after` and `next_cursor`. Replay every event from `after` through `next_cursor`, paging until that cutoff is reached; collect the guids of completed episodes re-emitted in that range. Only after the full range is committed, remove local podcast episodes absent from that set, then continue polling after `next_cursor`.
- **Chunking.** Consecutive turns grouped into windows of roughly 200 to 400 tokens, breaking on speaker change, with a little overlap. Chunk identity is a hash of guid, revision and chunk ordinal.
- **Speaker names.** The raw label is stored on each chunk and the episode's `speaker_names` joined at query time.
- **Generic document schema** so the scraper can plug in later: `documents` (`doc_id`, `source_type`, `source_id`, `url`, `title`, `published`, `fetched_at`, `revision`, `meta_json`) and `chunks` (`chunk_id`, `doc_id`, `revision`, `ordinal`, `start_s`, `end_s`, `speaker`, `text`).
- **Index.** SQLite with FTS5 plus `sqlite-vec` in one file, ranks combined with reciprocal rank fusion. Embeddings: Model2Vec `minishlab/potion-retrieval-32M` (512 dimensions, static, CPU-only). A model or chunker change requires a new index and a bootstrap.

### W3. MCP retrieval server (done)

**Why.** MCP is how Claude Code already reaches SearXNG, and the point of the knowledge base is that agents can ask it questions and quote the answer back.

**What.**

- Tools: `kb_search(query, k, filters)` with filters for feed, date range, speaker and source type; `kb_get_span(guid, start, end)` for surrounding transcript; `kb_list_sources()` for what is indexed.
- Every result carries a citation: show title, feed, published date, speaker display name, start and end seconds, `source_url`, and the episode `guid` and `revision`. For YouTube sources append the start time to the link; for audio feeds, give the timestamp in text.
- Transport: streamable HTTP (stateless, JSON responses) on `127.0.0.1:8877`, exposed with `tailscale serve` on 8443, with its own bearer token separate from ripperr's. DNS-rebinding protection allows only localhost and the tailnet name. Responses carry at most 24,000 characters of quotes.
- Built with the official `mcp` SDK 2.x (`MCPServer`). `cryptography` is held below 49, the last line with Intel macOS wheels.
- Transcript text is untrusted input. Return it clearly marked as quoted source material so a podcast host saying "ignore previous instructions" is not treated as an instruction.

**Done when.** Claude Code on the laptop can call `kb_search` and `kb_get_span` and get cited results, and a result's timestamp lands on the right passage in the audio. Verified with an MCP client from the laptop over the tailnet; registering it in Claude Code is the last step (command in the knowledge-base README).

### W4. Discovery to subscription (done)

**Why.** SearXNG's podcast engines return show and episode pages, but ripperr subscribes to feeds. Something has to resolve a search result to an RSS URL, and a person should approve the subscription, because each feed costs transcription time and disk.

**What.**

- A small helper in the knowledge-base repo (CLI first, MCP tool later) that runs a SearXNG query in the `podcasts` category, resolves each result to a feed URL (the fyyd or Podcast Index API, an iTunes lookup, or the page's `<link rel="alternate" type="application/rss+xml">`), and prints candidates with title, latest episode and feed URL.
- On confirmation, `POST /v1/feeds` with the URL, title and an optional `backfill`. Ripperr's call is idempotent, so a repeat is harmless.
- Be polite to hosts: no parallel bulk downloads, and no automated subscribing from search results.

**Built.** `kb discover QUERY [--backfill N]` in the knowledge-base repo. Podchaser results are already feed URLs; Apple Podcasts links resolve through the iTunes lookup API and other pages through their `<link rel="alternate">` feed. Each candidate shows SearXNG's episode count and the feed's newest episode, and feeds ripperr already follows are marked. Subscribing happens only at an interactive prompt. An MCP version is deferred: an agent finding feeds is useful, but subscribing should stay a deliberate step.

**Known limits.** SearXNG's fyyd engine currently crashes ("unexpected crash"), so results come from Podchaser alone. Podchaser matches titles and descriptions, so a host's name finds unrelated shows; topic queries work well.

**Done when.** A query for a topic yields feed candidates, confirming one creates a feed in ripperr, and its episodes flow through to the index with no manual steps beyond the confirmation. Candidates verified; the first real subscription (also the first RSS, rather than YouTube, feed on the Mac Pro) is still to make.

### W5. Scraper hookup (after W3)

**Why.** The knowledge base should also hold web pages, and SearXNG returns only links and snippets. The earlier fetch tests showed a plain HTTP client is not enough for some sites.

**Evidence so far.** `old.reddit.com` returned 403 from the VPS and 200 through the home IP, so egress matters there. Medium and the NYT were blocked either way with `curl`, so they need a real browser. Brave, DuckDuckGo and Google blocking was identical from both IPs, so a residential IP does not fix search-engine scraping.

**What.**

- Crawl4AI on the VPS in its own virtualenv with Chromium, one page at a time (6 GB RAM). Optionally route through `socks5://127.0.0.1:1080` (the home tunnel) for sites that need a residential address.
- Send cleaned markdown over the tailnet to a small authenticated ingestion endpoint on the Mac Pro, where the indexer owns the SQLite writes. Do not access its SQLite file remotely. Deduplicate on canonical URL plus a content hash, write `source_type = web` into the same `documents` and `chunks` schema, and record `fetched_at` so staleness is visible.
- Out of scope here: crawling at scale. The user does not run swarms.

### W6. Evaluation set (done)

`eval/podcast_questions.jsonl` in the knowledge-base repo: 22 questions tied to real passages, with episode guids and approximate timestamps. `kb eval` reports hit rate at k=5 and k=10 and mean rank. Rerun it after any chunking, embedding or ranking change, and add questions as the collection grows.

## Decisions

| Decision | Chosen | Why |
| --- | --- | --- |
| Where model inference runs | `ripperr-worker` on the M5, pulling work from ripperr | Only inference needs Apple Silicon and macOS 14+; everything else belongs on an always-on host |
| Ripperr host | Mac Pro | Always on; 32 GB; base ripperr, ffmpeg, yt-dlp and deno all run on Intel macOS 12 |
| Indexer host | Mac Pro, next to ripperr | Same machine as its only data source; the VPS is tight on RAM |
| Vector store | `sqlite-vec` in the indexer's SQLite file | One file, no extra service |
| Embedding model | Model2Vec `potion-retrieval-32M` | Static CPU model that runs on Intel macOS 12, where torch and onnxruntime wheels are unreliable; content stays local |
| MCP transport | Streamable HTTP on the tailnet, own token | The index is on the Mac Pro and the first client is Claude Code on the laptop; a separate token keeps MCP clients away from ripperr writes |
| Backfill | Built into ripperr, bounded to 100 | Without it a discovered feed contributes only its newest episode |
| Audio retention | Deleted after merge on the Mac Pro | Transcripts and `remerge` need only the database and raw model output |

## Risks and non-goals

**Risks**

- **M5 offline.** Transcription stalls; prepared episodes wait in the queue. Everything else, including search, stays up.
- **Interfaces change under a consumer.** All three repos are 0.x. Change an HTTP contract only with a version bump on that contract, and rerun the end-to-end check after each change.
- **Speaker names change without a revision bump.** Re-read on every metadata event and join names at query time, or citations will show stale names.
- **Diarization is imperfect.** Overlapping speech and per-episode labels mean speaker attribution in citations is a hint, not a fact.
- **Prompt injection through transcripts and scraped pages.** Treat all retrieved text as untrusted quoted material in the MCP responses.
- **Secrets.** The ripperr token, any DeepInfra token, and the SearXNG and Cloudflare credentials stay in mode-600 files or the service environment, never in repos or chat.
- **Index drift.** Changing the embedding model or chunking silently degrades results unless the index records them and the eval set is rerun.
- **macOS privacy controls.** launchd jobs cannot read `~/Documents` unless the program has been granted access, which a background job cannot request. Install long-running jobs outside protected folders (the worker runs as a uv tool for this reason).

**Non-goals**

- Replacing ripperr's FTS5 search.
- SearXNG result caching (deferred on purpose until the existing setup has been pressure-tested).
- Public exposure of any service. Everything stays on the tailnet; no Funnel.
- Crawling or transcribing at scale. Requests stay polite and sequential.
- Republishing transcripts or fetched pages; this is a private research index.
