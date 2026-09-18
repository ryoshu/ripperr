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
| `Episode.guid` | The stable external key. Use it to refer to an episode from another system. RSS episodes use the feed's guid (or a hash of the audio URL if the feed has none); YouTube episodes use `yt:<video id>`. |
| `Episode.id` | A local integer. It is meaningful only within one database. Methods that take a `ref` accept a guid (`str`) or a local id (`int`). |
| Turn key | `(guid, idx)`. `idx` is 0-based and contiguous within one revision. |

`guid` is unique across **all** feeds. If two feeds use the same guid, the second
episode is silently ignored.

## Methods

### Ingest

| Method | Behaviour |
| --- | --- |
| `add_feed(url) -> Feed` | Idempotent: adding a known URL returns the existing feed. The URL may be an RSS feed or a YouTube playlist. |
| `feeds() -> list[Feed]` | In id order. |
| `sync() -> int` | Polls every feed and records episodes not yet seen. Returns the number of new episodes. A feed that fails to load is logged and skipped; `sync` does not raise for it. |
| `process(limit=None, *, glossary=None, retry_errors=False, force=False) -> list[Episode]` | Downloads, transcribes, diarizes and merges pending episodes, then returns them as they now stand. See "Processing". |
| `remerge(ref, *, glossary=None) -> Episode` | Redoes the glossary and merge steps from cached model output and returns the episode. Raises `LookupError` for an unknown episode and `FileNotFoundError` if it has no cached model output. |

### Read

| Method | Behaviour |
| --- | --- |
| `episode(ref) -> Episode \| None` | `None` if unknown. |
| `episodes(*, status=None, updated_since=None) -> list[Episode]` | In id order. `updated_since` is an ISO 8601 UTC timestamp and is **inclusive**. |
| `transcript(ref) -> Transcript \| None` | The episode, its turns in `idx` order, and the glossary corrections applied to this revision. `None` if unknown. An episode that is not done normally has no turns. |
| `search(query, limit=20) -> list[Hit]` | Full-text search over turns, best match first. The query may use FTS5 syntax; if it is not valid FTS5 (for example `don't`), it is retried as a literal phrase. `Hit.snippet` marks matches with `[` and `]`. |
| `stats() -> dict[str, int]` | Episode count per status. |

## Episode lifecycle

```
new ──▶ downloaded ──▶ done
  └──────────┴────────▶ error
```

- An episode whose audio is already on disk can go from `new` straight to `done`.
- `process` picks up `new` and `downloaded` episodes, plus `error` ones when
  `retry_errors=True`. `done` episodes are never picked up again; use `force` or
  `remerge` for those.
- On failure an episode becomes `error` and `Episode.error` holds a traceback.
  Success sets it back to `None`.
- `remerge` sets the episode to `done`, including one that was in `error`.

## Revisions: how a consumer stays in sync

`Episode.revision` starts at 0 and increases by one every time the transcript is
rewritten: after `process` finishes an episode, and after every `remerge`. Status
changes, such as `new` to `downloaded`, do not change it. `merged_at` is the time
of the last rewrite.

A poller should:

1. Call `episodes(status="done", updated_since=<last poll time>)`.
2. For each episode, compare `revision` with the one it stored.
3. If it differs, re-read `transcript(guid)` and replace whatever it derived from
   the old turns.

Because `updated_since` is inclusive, the same episode can appear on consecutive
polls; comparing revisions makes that harmless. Timestamps have second resolution,
so rely on `revision` rather than on the time to detect changes.

A new revision changes `idx` values and can change turn text. Do not keep turn
positions across revisions.

## Processing

- Order: episodes with a `published` date first, newest first, then episodes with
  no date (YouTube) by newest id. Dates are stored as ISO 8601 UTC.
- `process` does not raise for a failing episode. It records the error on that
  episode, moves on, and returns it with status `error`. This includes a missing
  ML dependency: without the `apple` extra every episode ends up `error`.
- `glossary`: a list of terms. `None` reads the glossary file (`glossary.txt` under
  the data directory, or `RIPPERR_GLOSSARY`); `[]` turns the glossary off.
- Model output is cached, so re-processing after a merge change, a glossary change
  or a crash skips the expensive steps. `force=True` ignores the cache.
- Processing needs Apple Silicon. Reading does not (see below).

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
- Speakers: labels like `SPEAKER_01` are per episode and mean nothing across
  episodes. `SPEAKER_?` means no speaker could be assigned.
- Models are frozen dataclasses (fields cannot be reassigned), and ripperr never
  modifies one after returning it.

## Concurrency and consistency

- One `Ripperr` holds one SQLite connection. Use it from the thread that created it;
  other threads get `sqlite3.ProgrammingError`. Create one instance per thread.
- The database uses WAL mode, so a second process can read while another writes.
  This has not been tested under load.
- `transcript()` reads the episode, turns and corrections in separate queries. If
  another process rewrites the transcript in between, you can get a mix. After
  reading, call `episode(guid)` again; if `revision` changed, read again.
- Two processes running `process` on the same database can pick up the same
  episode. Run one at a time.

## Dependencies

Reading (`episodes`, `transcript`, `search`, `stats`, `remerge` from cache) needs
only the base dependencies. Processing needs the `apple` extra, Senko, `ffmpeg`,
and, for YouTube, the `youtube` extra.
