# Worker contract (v1)

Ripperr is split in two. **Ripperr** (this repo) does everything except model
inference: feeds, downloads, ffmpeg normalization, the store, merge, glossary,
speaker identity, the change feed, the API and the dashboard. It needs no ML
runtime and runs on any machine with Python 3.11+ and ffmpeg. A **worker**
(separate repo) runs ASR and diarization and nothing else. It holds no database
and no state beyond a scratch directory.

The two meet only at the HTTP endpoints and JSON shapes below. Neither imports
the other's code.

## Flow

```
ripperr (always on)                              worker (where the GPU is)
  sync → download → status=downloaded
                        ◀── POST /v1/work/claim ──
  lease episode         ── guid, lease_id ──────▶
                        ◀── GET  /v1/work/{guid}/audio
                        ── 16 kHz mono WAV ─────▶  ASR + diarization
                        ◀── PUT  /v1/work/{guid}/result
  merge, glossary, identity, change event
```

Workers poll; ripperr never connects to a worker. A worker may sleep or vanish
at any point, and its lease simply expires.

## Endpoints

All require `Authorization: Bearer <token>` (`401` without it). A ripperr with
no token configured refuses them with `503`.

### `POST /v1/work/claim`

Body: `{"worker": "<name>", "schema": 1}`.

- `200` `{"guid", "lease_id", "lease_expires", "duration_s"}` leases the
  newest-published claimable episode, matching today's `pending()` order
  (`published DESC, id DESC`).
- `204` when nothing is claimable.
- `400` when `schema` is not supported; the body lists the supported versions.

An episode is claimable when its status is `downloaded` and it has no
unexpired lease. `ripperr prepare` (run on a schedule next to `ripperr sync`)
downloads and normalizes new episodes into that state, so a claim never waits
on a download. Claiming is atomic: two workers never
receive the same lease. The lease lasts `RIPPERR_LEASE_SECONDS` (default 7200).
There is no heartbeat: a worker that needs longer than the lease is too slow
for this deployment, so raise the setting.

### `GET /v1/work/{guid}/audio?lease_id=…`

Returns the episode's normalized WAV (16 kHz, mono, 16-bit PCM) with
`Content-Length`. `409` if the lease is not current.

### `PUT /v1/work/{guid}/result`

Body (`Content-Type: application/json`, at most 64 MiB):

```json
{
  "schema": 1,
  "lease_id": "…",
  "asr": {
    "config": {"backend": "mlx", "runtime": "0.4.2", "model": "…", "device": "auto", "language": null},
    "segments": [
      {"start": 0.0, "end": 4.1, "text": "…", "words": [{"start": 0.0, "end": 0.4, "word": "Hello"}]}
    ]
  },
  "diarization": {
    "config": {"backend": "senko", "runtime": "…", "model": null, "device": "auto"},
    "segments": [{"start": 0.0, "end": 4.3, "speaker": "SPEAKER_00"}],
    "embeddings": {"space": "senko-campplus", "speakers": {"SPEAKER_00": [0.01, "…"]}}
  }
}
```

- `asr.segments` keep Whisper's segment and word nesting. `words` may be
  empty; ripperr then uses the segment text, as today.
- `diarization.segments` are already normalized: sorted by `start`, labels
  `SPEAKER_NN`. Normalizing a backend's own output shape is the worker's job.
- `config` objects identify the model run. Ripperr keys its raw cache by these
  values, so a result from a different model never overwrites or masquerades
  as another model's output.
- `embeddings` is optional. `space` names the vector space. Ripperr stores and
  matches speaker embeddings only from its configured space
  (`RIPPERR_EMBEDDING_SPACE`, default `senko-campplus`) and drops others with a
  log line, keeping the transcript. Vectors from different models are not
  comparable, and one stray worker must not corrupt every feed's speaker
  profiles.

Ripperr records which model run each transcript was merged from, so `remerge`
and `prune_cache` find a worker's output even though ripperr has no models
installed.

Responses: `200` `{"guid", "revision"}` once the transcript is merged and
committed; `409` if the lease is not current (expired and re-claimed, or
already applied); `400` for a malformed body, which leaves the lease intact. A
result is applied at most once per lease: a worker that retries a `PUT` after
a lost response may get `409`, and should treat it as "done or taken over" and
move on.

### `POST /v1/work/{guid}/fail`

Body: `{"lease_id": "…", "error": "<one line>"}`. Marks the episode `error`
with the message and releases the lease. Failed episodes are not claimed
again until someone retries them (`ripperr prepare --retry`). A worker that crashes without calling
this just lets the lease expire.

## Versioning

`schema` is an integer. A breaking change to any body above bumps it, and
ripperr accepts the old and new versions for at least one release. Additive
fields do not bump it; both sides ignore fields they do not know.
