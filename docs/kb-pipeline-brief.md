# Knowledge Base Pipeline Brief

Oct 4, 2026 · @Ricky Bacon

Build the layer that turns ripperr transcripts (and later scraped pages) into a searchable, citable knowledge base that agents query over MCP, and wire SearXNG podcast discovery into ripperr's feed registry.

## Context

The goal is a personal knowledge base: SearXNG finds podcasts worth keeping, ripperr transcribes them, and agents (Claude Code and others) query the result with citations. Ripperr is already a complete ingest, transcribe, diarize and store service with a stable consumer contract, so this work is a consumer of ripperr, not a change to it.

Data flow, end to end:

1. SearXNG (`podcasts` category: fyyd, podchaser) surfaces shows and episodes of interest.
2. A discovery helper resolves a result to an RSS feed URL; a human confirms; `POST /v1/feeds` subscribes it in ripperr.
3. Ripperr on the M5 Pro syncs feeds, downloads, transcribes (mlx-whisper) and diarizes (Senko), then writes turns to SQLite and records a change event.
4. An always-on indexer polls ripperr's change feed over the tailnet, chunks transcripts, and builds a hybrid (keyword plus vector) index.
5. An MCP server exposes search and transcript-span tools that return chunks with speaker, timestamp and a source link.
6. Later, the scraper feeds web pages into the same index through the same document schema.

### Machines and services

| Machine | Tailnet address | Role today | Notes |
| --- | --- | --- | --- |
| `vps-anodyne` (Ubuntu 22.04, 4 CPU, 6 GB RAM) | 100.84.153.34 | SearXNG at `https://search.ryoshu.com`, tailnet-only via nginx; `mcp-searxng` for Claude Code | Also runs `home-proxy.service`, an SSH SOCKS tunnel on `127.0.0.1:1080` that exits through the Mac Pro's residential IP |
| `rickys-mac-pro` (Intel, macOS 12.7.6, user `hastur`) | 100.71.175.87 | Always-on home server; SOCKS tunnel exit | Sleep disabled (`pmset -a sleep 0`). Cannot run Senko (needs macOS 14+), so it cannot do transcription |
| M5 Pro laptop | tailnet name to confirm with `tailscale status` | Runs ripperr (needs the compute) and the dashboard | Not always on; any consumer must tolerate it being asleep or off the network |

Ripperr's own ports: `scripts/start-local.sh` runs the API on `127.0.0.1:8876` (`RIPPERR_API_PORT`) and the Vite dashboard on `127.0.0.1:5174`. A bare `ripperr serve` defaults to `8765`. Both bind loopback only; the server refuses any other `--host`.

### Why these choices

- **Consumer, not fork.** Ripperr's `docs/api.md` names the change feed as the canonical sync contract and says everything else is internal and may change. Depending only on the HTTP surface keeps the two repos independent.
- **Indexer off the M5.** The M5 sleeps and is where the GPU/NPU time goes. Retrieval should stay available when it is off, so the index lives on an always-on box.
- **Own index, not ripperr's FTS5.** Ripperr's `search()` is keyword-only over turns, returns raw per-episode speaker labels, and has no HTTP endpoint. It cannot do semantic retrieval or combine sources.

## What ripperr already provides

Source: `docs/api.md`, the README, and a read of the route list in `ripperr/server.py`. The internals of `store.py` were not read beyond the schema headings (schema version 8; tables for feeds, episodes, changes, turns, `turns_fts`, speaker names, embeddings and profiles).

| Need | Ripperr surface |
| --- | --- |
| Subscribe to feeds | `POST /v1/feeds` `{"url", "title"}`, idempotent; also `GET`, `PUT /v1/feeds/{id}`, `DELETE`. RSS or YouTube playlist |
| Durable sync stream | `GET /v1/changes?after=SEQ&limit=N`: ascending `seq`, events for metadata, transcript and deleted. Safe to replay a page |
| Full transcript | `GET /v1/episodes/{guid}`: metadata, `audio_url`, `source_url`, `revision`, ordered turns (`idx`, `start`, `end`, `speaker`, text), `corrections`, `speaker_names`, `speaker_matches`, `guest_hints`. ETag plus `If-None-Match` returns 304 |
| Episode list | `GET /v1/episodes`: metadata and status only, no turns |
| Health and cursor | `GET /healthz` returns `ok` and the highest `change_seq` |
| New-consumer bootstrap | `POST /v1/changes/bootstrap` (token required) emits the current revision of every completed episode |
| Speaker display names | `PUT` and `DELETE /v1/episodes/{guid}/speakers/{speaker}` (token required) |

### Constraints that shape the design

- **Loopback only.** The server rejects non-loopback binds because bearer tokens do not encrypt traffic. Remote access needs a TLS reverse proxy, tunnel or private-network gateway in front.
- **Writes need a token.** Feed writes and bootstrap are refused when the server has no token configured.
- **Revisions.** `revision` increases on every transcript rewrite, and `idx` values and text can change between revisions, so never keep turn positions across revisions. Metadata and speaker-name edits emit events without bumping `revision`, so re-read the episode on every metadata event.
- **Speaker labels** (`SPEAKER_01`) are per episode and mean nothing across episodes. Display names live in a separate mapping, and `Hit.speaker` from `search()` does not apply it.
- **No summaries or chapters.** `Episode.summary` is only the feed's own description. Ripperr does not generate summaries.
- **No backfill.** A new feed takes only its newest episode; later syncs take only entries newer than the newest known one.
- **Cursor loss.** A cursor older than the retained log gives `410 Gone` with `reset: true`. Recover with bootstrap, treat the re-emitted set as authoritative, and drop local episodes not re-emitted. Bootstrap appends one event per completed episode for every consumer, so do not call it repeatedly.
- **Feed validation.** Authenticated feed writes reject literal local or private hosts but do not resolve DNS or follow redirects, so this is not SSRF protection. Direct audio downloads are capped at 1 GiB.
- **Pre-1.0.** The package is 0.x. Pin a commit, and rely only on the documented HTTP surface.
- **Platform.** Senko needs macOS 14+ and Python below 3.14. Processing is exclusive per database, so run one `process` at a time.

## Work items

### W1. Expose ripperr on the tailnet and schedule it

**Why.** Ripperr is loopback-only, so the VPS and Mac Pro cannot reach it, and a bearer token alone would cross the network unencrypted. `tailscale serve` terminates TLS on the M5 and proxies to loopback, which keeps ripperr's own safety check intact.

**What.**

- Set `RIPPERR_API_TOKEN` in ripperr's `.env` (`start-local.sh` already passes it to the dashboard). Generate a long random value; store it with file mode 600 on each consumer; never commit it. Before enabling the token, update `start-local.sh`'s `/healthz` readiness probe to send the bearer header: the server authenticates health checks too, so the current unauthenticated probe reports a healthy API as a startup failure.
- Put `tailscale serve` in front of `127.0.0.1:8876` (check `tailscale serve --help` for the flag syntax of the installed version). MagicDNS and HTTPS certificates must be enabled in the Tailscale admin console.
- Do not bind ripperr to `0.0.0.0`, do not use Tailscale Funnel, and leave the Vite dashboard (`5174`) on loopback.
- Run the API as an API-only `launchd` service on the M5 rather than relying on the development script, which also starts Vite. Schedule `ripperr sync` and `ripperr run` with `launchd`, and keep the machine awake while processing (`caffeinate`). One `process` at a time.
- If the tailnet uses ACLs, allow only the indexer host to reach the M5 on 443.

**Done when.** From the indexer host, `curl -H "Authorization: Bearer $TOKEN" https://<m5-name>.<tailnet>.ts.net/healthz` returns `ok` and a sequence number, and the same request without the token is rejected.

### W2. Indexer service

**Why.** This is the missing piece: it turns ripperr's transcripts into something that supports semantic search, merges sources, and stays available when the M5 is off.

**Where.** Recommended: the Mac Pro, which is always on and otherwise idle (check its RAM first). The VPS is the alternative but has 6 GB RAM shared with SearXNG and valkey. Pick one before starting; see decisions below.

**What.**

- **Sync loop.** Poll `GET /v1/changes?after=<cursor>&limit=100`. Persist the cursor in the indexer's own SQLite database, in the same transaction as the writes it covers, so a crash never advances past unprocessed events. Back off and retry when the M5 is unreachable.
- **Per event.** For a transcript event, `GET /v1/episodes/{guid}` with `If-None-Match`; if the revision is newer than the stored one, delete that episode's chunks and insert the new set in one transaction. For a metadata event, always re-read and update metadata and speaker names. For a deleted event, remove everything for that guid.
- **Reset.** On `410` with `reset: true`, call bootstrap and record both `after` and `next_cursor`. Replay every event from `after` through `next_cursor`, paging until that cutoff is reached; collect the guids of completed episodes re-emitted in that range. Only after the full range is committed, remove local podcast episodes absent from that set, then continue polling after `next_cursor`. Do not use an incomplete page as the authoritative set. `GET /v1/episodes` can help reconcile the full episode list, but includes episodes without completed transcripts.
- **Chunking.** Group consecutive turns into windows of roughly 200 to 400 tokens, breaking on speaker change, with a little overlap. Keep `start` and `end` seconds, the speaker label, and the text. Chunk identity is a hash of guid, revision and chunk ordinal, since turn `idx` is not stable and split or overlapping chunks can share a start time.
- **Speaker names.** Store the raw label on each chunk and join the episode's `speaker_names` at query time, because names can change without a revision bump.
- **Generic document schema** so the scraper can plug in later: a `documents` table (`doc_id`, `source_type` such as `podcast` or `web`, `source_id` such as the guid, `url`, `title`, `published`, `fetched_at`, `revision`, `meta_json`) and a `chunks` table (`chunk_id`, `doc_id`, `revision`, `ordinal`, `start_s`, `end_s`, `speaker`, `text`).
- **Index.** SQLite with FTS5 for keywords plus `sqlite-vec` for vectors, in one file. Record the embedding model name and dimension in index metadata so a model change forces a rebuild. Combine keyword and vector ranks with reciprocal rank fusion. On macOS, make sure the Python build allows loadable SQLite extensions (use a Homebrew or `uv`-managed Python, not the system one).
- **Embeddings.** A small local model that runs acceptably on CPU. Batch it; transcripts arrive in bulk only at bootstrap.

**Done when.** A full rebuild from bootstrap yields the same chunk counts as incremental sync; a new revision replaces the old chunks with no duplicates; a deleted event removes all rows; and killing the process mid-page and restarting neither loses nor duplicates events.

### W3. MCP retrieval server

**Why.** MCP is how Claude Code already reaches SearXNG, and the point of the knowledge base is that agents can ask it questions and quote the answer back.

**What.**

- Tools: `kb_search(query, k, filters)` with filters for feed, date range, speaker and source type; `kb_get_span(guid, start, end)` for surrounding transcript; `kb_list_sources()` for what is indexed.
- Every result carries a citation: show title, feed, published date, speaker display name, start and end seconds, `source_url`, and the episode `guid` and `revision`. For YouTube sources append the start time to the link; for audio feeds, give the timestamp in text.
- Transport: because the index runs on the Mac Pro and Claude Code must query it from the laptop, expose authenticated streamable HTTP on the tailnet behind nginx or `tailscale serve` for the first usable version. Stdio is an optional local interface on the indexer host, or can be reached from the laptop through an explicit SSH launcher. Cap response size.
- Transcript text is untrusted input. Return it clearly marked as quoted source material so a podcast host saying "ignore previous instructions" is not treated as an instruction.

**Done when.** Claude Code on the laptop can call `kb_search` and `kb_get_span` and get cited results, and a result's timestamp lands on the right passage in the audio.

### W4. Discovery to subscription

**Why.** SearXNG's podcast engines return show and episode pages, but ripperr subscribes to feeds. Something has to resolve a search result to an RSS URL, and a person should approve the subscription, because each feed costs transcription time and disk.

**What.**

- A small helper (CLI first, MCP tool later) that runs a SearXNG query in the `podcasts` category, resolves each result to a feed URL (the fyyd or Podcast Index API, an iTunes lookup, or the page's `<link rel="alternate" type="application/rss+xml">`), and prints candidates with title, latest episode and feed URL.
- On confirmation, `POST /v1/feeds` with the URL and title. Ripperr's call is idempotent, so a repeat is harmless.
- Backfill is a gap: ripperr takes only the newest episode of a new feed. If older episodes matter, that is a change in the ripperr repo (a bounded backfill option), so decide before building around the limitation.
- Be polite to hosts: no parallel bulk downloads, and no automated subscribing from search results.

**Done when.** A query for a topic yields feed candidates, confirming one creates a feed in ripperr, and its newest episode flows through to the index with no manual steps beyond the confirmation.

### W5. Scraper hookup (after W1 to W3)

**Why.** The knowledge base should also hold web pages, and SearXNG returns only links and snippets. The earlier fetch tests showed a plain HTTP client is not enough for some sites.

**Evidence so far.** `old.reddit.com` returned 403 from the VPS and 200 through the home IP, so egress matters there. Medium and the NYT were blocked either way with `curl`, so they need a real browser. Brave, DuckDuckGo and Google blocking was identical from both IPs, so a residential IP does not fix search-engine scraping.

**What.**

- Crawl4AI on the VPS in its own virtualenv with Chromium, one page at a time (6 GB RAM). Optionally route through `socks5://127.0.0.1:1080` (the home tunnel) for sites that need a residential address.
- Send cleaned markdown over the tailnet to a small authenticated ingestion endpoint on the Mac Pro, where the indexer owns the SQLite writes. Do not access its SQLite file remotely. Deduplicate on canonical URL plus a content hash, write `source_type = web` into the same `documents` and `chunks` schema, and record `fetched_at` so staleness is visible.
- Out of scope here: crawling at scale. The user does not run swarms.

### W6. Evaluation set

**Why.** Chunk size, embedding model and ranking are all guesses until measured, and the only way to know a change helped is to rerun the same questions.

**What.** Write 20 to 30 questions whose answers are known to sit in specific episodes, each with the expected `guid` and an approximate timestamp. A script runs `kb_search` and reports hit rate at k=5 and k=10 plus mean rank. Keep it in the indexer repo and rerun it after any chunking, embedding or ranking change. Start it alongside W2, using whichever episodes are already transcribed.

## Order of work and decisions

Build W1, then W2 and W6 together, then W3, then W4, and leave W5 until retrieval works on podcasts alone. Each item is usable on its own: after W1 you can poll ripperr from anywhere; after W2 you can query the index from a script; after W3 agents can use it.

| Decision | Recommended default | Why |
| --- | --- | --- |
| Indexer host | Mac Pro | Always on and idle; the VPS is tight on RAM. Confirm the Mac Pro's RAM and that a Python 3.11+ with loadable SQLite extensions installs on macOS 12 |
| Vector store | `sqlite-vec` in the indexer's SQLite file | One file, same tooling as ripperr, no extra service. LanceDB is the fallback if extension loading is a problem |
| Embedding model | A small local model that runs on CPU | Transcripts are text and volumes are modest; avoids sending content to a third party |
| MCP transport | Streamable HTTP on the tailnet first | The index is on the Mac Pro and the initial Claude Code client is on the laptop; stdio can be added locally or through an SSH launcher |
| Backfill of older episodes | Decide before W4 | Needs a change in the ripperr repo; without it a new feed contributes only its newest episode |
| Where the MCP and indexer code live | A new repo, separate from ripperr | Keeps the HTTP contract as the only coupling |

## Risks and non-goals

**Risks**

- **M5 offline.** Transcription stalls and the change feed is unreachable. The indexer must retry quietly and keep serving what it already has.
- **Ripperr changes under you.** The package is 0.x. Pin a commit, use only the documented HTTP surface, and rerun the end-to-end check after each ripperr update.
- **Speaker names change without a revision bump.** Re-read on every metadata event and join names at query time, or citations will show stale names.
- **Diarization is imperfect.** Overlapping speech and per-episode labels mean speaker attribution in citations is a hint, not a fact.
- **Prompt injection through transcripts and scraped pages.** Treat all retrieved text as untrusted quoted material in the MCP responses.
- **Secrets.** The ripperr token, any DeepInfra token, and the SearXNG and Cloudflare credentials stay in mode-600 files or the service environment, never in repos or chat.
- **Index drift.** Changing the embedding model or chunking silently degrades results unless the index records them and the eval set is rerun.

**Non-goals**

- Changing ripperr's internals, or replacing its FTS5 search.
- SearXNG result caching (deferred on purpose until the existing setup has been pressure-tested).
- Public exposure of any service. Everything stays on the tailnet; no Funnel.
- Crawling or transcribing at scale. Requests stay polite and sequential.
- Republishing transcripts or fetched pages; this is a private research index.
