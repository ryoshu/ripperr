# Ripperr API contract

`ripperr.api.Ripperr` is the interface other code should depend on. This page says
what it promises. Anything not stated here is not promised.

Public surface: `ripperr.api`, `ripperr.models` and `ripperr.config.Config`.
Everything else (`store`, `pipeline`, `merge`, ...) is internal and may change
without notice. The package is 0.x: expect breaking changes, and pin a version.

```python
from ripperr.api import Ripperr
from ripperr.config import Config

with Ripperr(Config(root=path), log=print) as rip:
    ...
```

`log` receives progress lines. Pass `lambda _: None` to silence it.

## Identity

| Term | Meaning |
| --- | --- |
| `Episode.guid` | The stable public key. Use it to refer to an episode from another system. It is opaque: derived from the feed's URL and the feed's own id for the episode, and unique across all feeds. |
| `Episode.source_guid` | The feed's own id for the episode (the RSS guid, or a hash of the audio URL if the feed has none; `yt:<video id>` for YouTube). Only unique within one feed. |
| `Episode.id` | A local integer. It is meaningful only within one database. Methods that take a `ref` accept a guid (`str`) or a local id (`int`). |
| Turn key | `(guid, idx)`. `idx` is 0-based and contiguous within one revision. |

Two feeds that use the same `source_guid` (RSS guids are often short, like `1`) get
two episodes with different public `guid`s. The public guid is fixed when the
episode is first recorded, so it does not change if the feed's URL later does.
Databases created before `source_guid` existed keep their old `guid` values, which
were the feed's own ids.

## Methods

### Ingest

| Method | Behaviour |
| --- | --- |
| `add_feed(url, title=None) -> Feed` | Idempotent: adding a known URL returns the existing feed. The URL may be an RSS feed or a YouTube playlist. |
| `feeds() -> list[Feed]` | In id order. |
| `feed(feed_id) -> Feed \| None` | Returns one feed, or `None` if unknown. |
| `update_feed(feed_id, url, title=None) -> Feed` | Replaces a feed's URL and title. Raises `LookupError` if unknown and `ValueError` if the URL is already registered. |
| `delete_feed(feed_id) -> None` | Removes a feed and its stored episodes. Raises `LookupError` if unknown and `ProcessingBusyError` if model processing is active. |
| `prune_cache() -> int` | Removes stale ASR/diarization cache variants and caches for unknown episodes. Raises `ProcessingBusyError` if model processing is active. |
| `sync() -> int` | Polls every feed, records episodes not yet seen and returns how many were new. A new feed starts with only its newest episode; later syncs take only entries ahead of the newest known episode, so a historical playlist is not backfilled. For episodes already known it refreshes `title`, `summary`, `published` and `audio_url` when the feed now gives a different value, so corrected metadata reaches the next retry; a value the feed no longer provides never erases the stored one. Status, audio, transcript and `revision` are untouched. A feed that fails to load is logged and skipped; `sync` does not raise for it. |
| `process(limit=None, *, glossary=None, retry_errors=False, force=False) -> list[Episode]` | Downloads, transcribes, diarizes and merges pending episodes, then returns them as they now stand. Processing is exclusive across processes sharing the same database, so a concurrent run returns no work immediately. See "Processing". |
| `remerge(ref, *, glossary=None) -> Episode` | Redoes the glossary and merge steps from cached model output and returns the episode. Raises `LookupError` for an unknown episode and `FileNotFoundError` if it has no usable cached model output (missing, or unreadable). |

### Read

| Method | Behaviour |
| --- | --- |
| `episode(ref) -> Episode \| None` | `None` if unknown. |
| `episodes(*, status=None, updated_since=None, after=0, limit=None) -> list[Episode]` | In id order. `updated_since` is an ISO 8601 UTC timestamp and is **inclusive**. `after` is an exclusive local-id cursor; `limit` caps the returned page. |
| `transcript(ref) -> Transcript \| None` | The episode, its turns in `idx` order, glossary corrections applied to this revision, and episode-scoped speaker names. `None` if unknown. An episode that is not done normally has no turns. |
| `speaker_names(ref) -> list[SpeakerName]` | Episode-scoped display names for raw diarization labels. |
| `speaker_embeddings(ref) -> list[SpeakerEmbedding]` | Locally stored per-episode Mac/Senko voice samples, when available. These are not exposed by the HTTP episode response. |
| `set_speaker_name(ref, speaker, name) -> SpeakerName` | Stores a manual display name without changing the raw transcript turn labels. `speaker` must occur in the episode's current turns; otherwise it raises `ValueError`. |
| `delete_speaker_name(ref, speaker) -> None` | Removes an episode-scoped display name. |
| `search(query, limit=20) -> list[Hit]` | Full-text search over turns, best match first. The query may use FTS5 syntax; if it is not valid FTS5 (for example `don't`), it is retried as a literal phrase. `Hit.snippet` marks matches with `[` and `]`. |
| `stats() -> dict[str, int]` | Episode count per status. |
| `change_seq() -> int` | Highest committed change-feed sequence number. |
| `changes(after=0, limit=100) -> list[Change]` | Returns cursor-based transcript and metadata change events in ascending sequence order. |
| `prune_changes(through) -> int` | Deletes events through an acknowledged sequence number and returns the number removed. A later read with an older cursor raises `ChangeLogPrunedError`, so call it only after every consumer has advanced past it. |
| `emit_current() -> int` | Queues the current revision of every completed episode for a new consumer's bootstrap. |

## Episode lifecycle

```
new ──▶ downloaded ──▶ done
  └──────────┴────────▶ error
```

- An episode whose audio is already on disk can go from `new` straight to `done`.
- `process` picks up `new` and `downloaded` episodes, plus `error` ones when
  `retry_errors=True`. `done` episodes are never picked up again; use `remerge`
  to rewrite their transcripts from cached model output. `force` only makes a
  selected pending or retried episode rerun the models instead of using its cache.
- On failure an episode becomes `error` and `Episode.error` holds a traceback.
  Success sets it back to `None`.
- `remerge` sets the episode to `done`, including one that was in `error`.

## Revisions: how a consumer stays in sync

`Episode.revision` starts at 0 and increases by one every time the transcript is
rewritten: after `process` finishes an episode, and after every `remerge`. Status
changes, such as `new` to `downloaded`, and metadata refreshes from `sync` do not
change it. `merged_at` is the time of the last rewrite. `updated_at` moves on any
change, including processing bookkeeping, metadata refreshes, and speaker-name
edits. Speaker-name change events keep the current revision; consumers should
re-read the episode on every metadata event rather than using revision alone.
If an episode is deleted and later re-added with the same guid, its revision
continues above the deleted revision rather than resetting.

Writing a transcript, bumping the revision, storing the corrections and marking the
episode `done` (clearing `error`) happen in one transaction, so a failure part-way
leaves the previous transcript, revision and status in place. `transcript()` reads
under one snapshot (see "Concurrency and consistency"), so what it returns comes
from a single revision. Two separate calls, such as `episodes()` then
`transcript()`, are two snapshots: compare `revision` between them.

For consumers that need a durable synchronization stream, `changes(after, limit)`
is the canonical contract. It provides an ordered cursor and includes metadata,
transcript, and deleted events. `episodes(updated_since=...)` is a local polling
convenience: it is inclusive, can repeat rows, and also sees internal status or
audio bookkeeping that does not create a change event.

Change events remain durable until explicitly pruned with `prune_changes`; the
caller is responsible for confirming that every consumer has advanced past the
pruned sequence. The sequence high-water mark is retained, so later events
continue to have monotonically increasing cursors.

A change-feed consumer should:

1. Call `changes(after=<last sequence>)`.
2. For transcript events, re-read `episode_guid` and compare the returned
   revision with the one it stored. For metadata events, always re-read the
   episode because metadata can change while `revision` stays the same; an ETag
   or `If-None-Match` check can avoid downloading an unchanged body.
3. For transcript revisions, re-read `transcript(guid)` and replace whatever it
   derived from the old turns.
4. Treat `deleted` events as removal notifications; the episode endpoint will
   return 404 afterward.

If a consumer asks for a cursor older than the retained log, the Python API
raises `ChangeLogPrunedError`; HTTP clients receive `410 Gone` with `reset: true`.
Recover by re-bootstrapping: call `emit_current()`, or `POST /v1/changes/bootstrap`
over HTTP, then poll from the `after` it returns.

Bootstrap emits only episodes that are completed now. A `deleted` event inside the
pruned range is gone for good, so a consumer that has reset must treat the
re-emitted set as authoritative: keep the episodes it sees again, and drop any
local episode whose guid is not re-emitted (`GET /v1/episodes` lists every guid
if it needs the full set). Bootstrap appends one event per completed episode to
the shared log, so every other consumer replays them too; that is harmless because
they compare revisions, but avoid calling it repeatedly.

Because `updated_since` is inclusive, the same episode can appear on consecutive
polls. Compare revisions for transcript changes; metadata events are authoritative
even when the revision is unchanged. Timestamps have second resolution.

A new revision changes `idx` values and can change turn text. Do not keep turn
positions across revisions.

## Processing

- Order: episodes with a `published` date first, newest first, then episodes with
  no date (YouTube) by newest id. Dates are stored as ISO 8601 UTC.
- `process` does not raise for a failing episode. It records the error on that
  episode, moves on, and returns it with status `error`. This includes a missing
  ML dependency: install the matching `apple` or `linux` extra for processing.
- `glossary`: a list of terms. `None` reads the glossary file (`glossary.txt` under
  the data directory, or `RIPPERR_GLOSSARY`); `[]` turns the glossary off.
- Only the work up to that transaction can mark an episode `error`. Once the
  transcript is stored, a failure while deleting audio (`keep_audio` off) is logged
  and the episode stays `done`. When the source audio is deleted, `audio_path` is
  cleared (`None`); if deletion fails it is left set, because the file still exists.
- Model output is cached, so re-processing after a merge change, a glossary change
  or a crash skips the expensive steps. `force=True` ignores the cache. Names are
  cleared when a rewrite is built from different diarization output than the
  stored transcript, and are preserved when the output is identical. `remerge`
  from the same cache therefore preserves them.
- Episodes finished before schema v6 have no stored diarization key. Their first
  rewrite records one without clearing existing names because there is nothing to
  compare against.
- The diarization key is an exact hash of the segments. A nondeterministic
  diarizer can produce slightly different output on a rerun with the same
  speakers, which clears names conservatively.
- Cache files are written to a temporary file and renamed into place, so an
  interruption never leaves a partial file (a killed run can leave a stray `*.tmp`
  in `raw/`, which is safe to delete). A cache that can't be read is treated as
  missing and redone, not as an error.
- ASR and diarization caches are keyed by episode and the relevant backend,
  runtime version, model, device, language, and cache format. Changing those
settings selects a new cache automatically; use `force=True` to replace the
current cache.

`prune_cache()` is an explicit cleanup operation for the older cache variants
left behind after model or backend settings change. It keeps the cache selected
by the current configuration for every live episode.
- Processing needs a selected local model backend. Reading does not (see below).

## Glossary corrections

A correction is a `(heard, fixed, count)` triple. `Transcript.corrections` holds
those for the **current** revision only; each rewrite replaces them. Corrections
are a report of what changed, not an instruction: the corrected text is already in
the turns. Matching rules and limits are in the README.

## Data conventions

- Times: `Turn.start` and `Turn.end` are seconds from the start of the audio.
  `Episode.duration` is in seconds and is `None` if `ffprobe` is unavailable.
- Timestamps (`updated_at`, `merged_at`): ISO 8601 UTC with second resolution, for
  example `2026-09-18T21:17:16+00:00`.
- `Episode.published`: ISO 8601 UTC, or `None` if the feed gave no date.
- `Episode.summary`: the source-provided RSS description or YouTube description, or `None` when unavailable. Ripperr does not generate summaries.
- `Episode.source_url`: public RSS entry or YouTube watch URL for attribution; never a local path.
- Speakers: labels like `SPEAKER_01` are per episode and mean nothing across
  episodes. `SpeakerName` mappings provide editable display names without
  rewriting transcript turns. `SPEAKER_?` means no speaker could be assigned.
- `Hit.speaker` from `search()` is the raw per-episode diarization label; search
  does not join `SpeakerName` mappings. The `show` CLI command applies mappings.
- `SpeakerName.method` is currently `manual` and `confidence` is currently
  `None`; other values are reserved for future attribution methods.
- Models are frozen dataclasses (fields cannot be reassigned), and ripperr never
  modifies one after returning it.

## Concurrency and consistency

- One `Ripperr` holds one SQLite connection. Use it from the thread that created it;
  other threads get `sqlite3.ProgrammingError`. Create one instance per thread.
- The database uses WAL mode, so a second process can read while another writes.
  This has not been tested under load.
- `transcript()` reads the episode, turns and corrections inside one SQLite read
  transaction, so they always belong to the same revision even if another process
  commits a rewrite while it runs. Other methods are single queries. Anything that
  combines several calls sees several snapshots.
- Two processes running `process` on the same database can pick up the same
  episode. Run one at a time.

## Change-feed server

`ripperr serve` exposes an HTTP API for consumers that cannot share the SQLite
file. Transcript data is read-only; the feed registry also has an authenticated
add operation. It binds to `127.0.0.1:8765` by default and rejects non-loopback
binds: bearer tokens authenticate requests but do not encrypt HTTP traffic.
For a remote consumer, keep Ripperr loopback-only and put a TLS reverse proxy,
SSH tunnel, or private-network gateway in front of it. `--token TOKEN` remains
available as defense in depth behind that trusted boundary.

`GET /healthz` returns `ok` and the highest committed `change_seq`. `GET
/v1/changes?after=SEQ&limit=N` returns ascending, cursor-based metadata and
transcript, and deleted events. The cursor is advanced by the last returned
`seq`; callers may safely replay a page. A deleted event means the episode
endpoint will no longer be available. `POST /v1/changes/bootstrap` requires
the bearer token, emits the current revision of every completed episode, and
returns `after` and `next_cursor`; fetch the emitted events with
`GET /v1/changes?after=after`.

`GET /v1/feeds` returns the registered feed ids, URLs and titles. `POST
/v1/feeds` accepts `{"url": "https://…", "title": "…"}` and returns the
idempotently stored feed. `PUT /v1/feeds/{id}` replaces an existing feed's URL
and title. `DELETE /v1/feeds/{id}` removes the feed, its stored episodes, and
owned local audio/model-cache files.
Deleting a feed also deletes its episode-scoped speaker names. Re-adding the same
feed and episode restores the monotonic revision, but not those manual mappings.
Authenticated feed management rejects local/private literal hosts as a typo and
misconfiguration guard. It does not resolve DNS names or inspect redirect
targets, so it is not complete SSRF protection. Direct RSS audio downloads are
capped at 1 GiB and incomplete files are removed on failure.
Feed writes require `ripperr serve --token TOKEN`, and the caller must send
`Authorization: Bearer TOKEN`; the server refuses feed writes when no token is
configured.

`GET /v1/episodes` returns the stored episodes with feed metadata, public
episode metadata including the source `summary` when available, processing
status, and revision information. It does not include transcript turns.

`GET /v1/episodes/{guid}` returns feed metadata, public episode metadata,
corrections, ordered turns, and `speaker_names` objects with `speaker`, `name`,
`method`, `confidence`, and `updated_at`. It omits audio and host filesystem
paths. The response has an ETag derived from the complete response body and
returns `304 Not Modified` when `If-None-Match` matches.

`PUT /v1/episodes/{guid}/speakers/{speaker}` requires the bearer token and
accepts `{"name": "Display Name"}`. It returns the stored `speaker_name`.
The speaker must occur in the episode's current transcript. `DELETE` on the
same path removes the mapping and returns `{"deleted": "{speaker}"}`. Both
operations leave raw turn labels and transcript `revision` unchanged, but emit
a metadata change and update `updated_at`.

`ripperr serve --emit-current` queues a transcript event for every completed
episode, which is the one-time bootstrap operation for a new consumer.

## Dependencies

Reading (`episodes`, `transcript`, `search`, `stats`, `remerge` from cache) needs
only the base dependencies. Processing needs `ffmpeg` plus either the `apple`
extra (MLX/Senko) or the `linux` extra (faster-whisper/pyannote). YouTube also
needs the `youtube` extra.
