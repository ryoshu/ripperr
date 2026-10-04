# ripperr

Subscribe to podcast feeds (RSS or YouTube playlists), get them transcribed and
diarized, store everything in SQLite, and serve it to other programs. Model
inference runs in a separate worker ([ripperr-worker](https://github.com/ryoshu/ripperr-worker)),
so ripperr itself needs no ML runtime and runs on any machine with Python 3.11+
and ffmpeg. Meant to be used from Python (`ripperr.api.Ripperr`) as well as from
the CLI.

```
RSS / YouTube ──▶ download ──▶ ffmpeg 16k mono ──▶ [worker: ASR + diarization] ──▶ words, speaker segments
                                                                                         │
                         glossary respelling ──▶ merge by temporal overlap ◀────────────┘
                                                          │
                                                    SQLite + FTS5 ──▶ change feed, HTTP API
```

## Architecture

Ripperr owns feeds, downloads, audio preparation, the store, the merge,
glossary corrections, speaker identity and the HTTP API. A worker claims a
prepared episode over HTTP, runs speech recognition and diarization on its
16 kHz WAV, and uploads the raw output; ripperr merges it. Workers only poll,
so one can run on a laptop that sleeps: an unfinished episode's lease expires
and it goes back in the queue. The protocol is
[docs/worker-contract.md](docs/worker-contract.md).

## Setup

```bash
brew install ffmpeg                 # macOS; elsewhere use the system package manager
uv venv --python 3.13 && source .venv/bin/activate
uv pip install -e ".[youtube]"      # drop [youtube] if you only use RSS feeds
npm ci --prefix frontend           # dashboard dependencies
```

Then set up at least one [ripperr-worker](https://github.com/ryoshu/ripperr-worker)
pointing at this server.

## Usage

```bash
ripperr add https://example.com/feed.xml
ripperr add https://example.com/feed.xml --backfill 10   # also take the 10 newest
ripperr sync                    # poll feeds, record new episodes
ripperr prepare                 # download + normalize for workers
ripperr show 12 --out ep12.md   # markdown transcript
ripperr search "interest rates"
ripperr status
```

Maintenance operations such as `prune_changes()` and `prune_cache()` are
available through the Python API only; the CLI does not expose destructive
cleanup commands.

Settings come from the environment:

| Variable | Effect |
| --- | --- |
| `RIPPERR_ROOT` | where the database, audio and cache live (default `~/ripperr`) |
| `RIPPERR_KEEP_AUDIO=0` | delete audio once a worker's result is merged |
| `RIPPERR_GLOSSARY` | glossary file (default `<root>/glossary.txt`) |
| `RIPPERR_DEEPINFRA_TOKEN` | optional DeepInfra token for LLM guest extraction (`DEEPINFRA_TOKEN` also works) |
| `RIPPERR_DEEPINFRA_MODEL` | DeepInfra model (default `deepseek-ai/DeepSeek-V4-Flash-0731`) |
| `RIPPERR_DEEPINFRA_BASE_URL` | DeepInfra OpenAI-compatible base URL |
| `RIPPERR_LEASE_SECONDS` | how long a remote worker holds a claimed episode (default 7200) |
| `RIPPERR_DASHBOARD_DIR` | built dashboard to serve alongside the API (unset: API only) |
| `RIPPERR_EMBEDDING_SPACE` | speaker-vector space accepted from workers (default `senko-campplus`) |

Run the tests with `uv pip install pytest && python -m pytest tests`.

### YouTube

Playlist URLs work as feeds (`uv pip install -e ".[youtube]"` for yt-dlp and its
deno JS runtime). Audio is fetched with yt-dlp and transcribed like any other
episode. YouTube's own auto-captions are deliberately not used: they have no
speakers and garble proper nouns more than Whisper does. Playlist entries that
are private or deleted are skipped.

### Glossary

Whisper spells names by ear ("Drake May", "Basial Tootin"). Give ripperr a list of
terms it should know and it respells near-sound-alikes (metaphone match, cutoff
`Config.glossary_match`) before the merge. Put one term per line in
`glossary.txt` in the data directory (`#` comments allowed), or pass a list from
Python. Only two-word terms (first + last name) are matched, so lone surnames pass
through, and it can misfire on people who aren't in the list; each swap is stored
with the episode (`Transcript.corrections`) so you can check. Raw ASR output is
never modified, so `ripperr remerge <id>` re-applies a changed glossary in seconds.

What goes in the glossary is up to the caller. ripperr has no idea what a player
or a company is.

### Unattended operation

Run `ripperr serve` with a bearer token, and a launchd agent or cron job
running `scripts/cron.sh` (`ripperr sync && ripperr prepare`). Workers pick up
whatever is prepared.

## Python API

Other code should use `ripperr.api.Ripperr`, not the database:

```python
from ripperr.api import Ripperr

with Ripperr() as rip:
    rip.sync()
    rip.prepare()                              # a worker transcribes these
    for ep in rip.episodes(status="done", updated_since=last_seen):
        transcript = rip.transcript(ep.guid)   # episode, turns, corrections
    rip.remerge(guid, glossary=new_terms)      # after the glossary changes
    rip.search("interest rates")
```

Everything returned is a plain dataclass from `models.py`. Episodes are keyed by
`guid`, which is stable across storage backends; `Episode.revision` increases
whenever a transcript is rewritten, so a consumer knows when to re-read it.
`episodes(updated_since=...)` is inclusive, so a poller should expect repeats and
compare revisions. The CLI is a client of this same class. The full contract,
including errors, ordering and consistency, is in [docs/api.md](docs/api.md).


## Layout

| Module | Role |
| --- | --- |
| `api.py` | the public `Ripperr` class; CLI and other callers use only this |
| `models.py` | plain dataclasses returned by the API |
| `config.py` | paths and thresholds, overridable from the environment |
| `feeds.py` | RSS parsing, enclosure download |
| `youtube.py` | YouTube playlists, audio via yt-dlp |
| `glossary.py` | phonetic respelling of names from a supplied term list |
| `audio.py` | ffmpeg normalization to 16 kHz mono WAV |
| `merge.py` | word flattening, word→speaker assignment, turn grouping |
| `pipeline.py` | feed sync, audio preparation, worker results, raw-output cache, merge |
| `server.py` | HTTP API, change feed, worker endpoints |
| `store.py` | SQLite schema, FTS5 search |


## Dashboard

The repo includes a small local React + HeroUI dashboard for feed administration,
episode status, transcript review, and episode-scoped speaker names. It has no
login screen; set `RIPPERR_API_TOKEN` in `.env` to protect the local API. The
startup script passes it to the dashboard as well. The server also accepts
`--token` when started manually.

```bash
./scripts/start-local.sh
# ...work...
./scripts/stop-local.sh
```

This starts the API on `http://127.0.0.1:8876` and the Vite dashboard on
`http://127.0.0.1:5174`; logs and PID files live under `.local/`.
For manual startup, the API and frontend commands above remain valid. Open the
Vite URL shown in the terminal.
The dev server proxies `/healthz` and `/v1` to the Ripperr server; set
`RIPPERR_API_SERVER` to point it elsewhere, including another host.

To serve the dashboard from `ripperr serve` itself, build it
(`npm run build --prefix frontend`) and set `RIPPERR_DASHBOARD_DIR` to
`frontend/dist`. The server then answers any non-API path with the built files,
which hold no secrets and need no token; the API still does. A built dashboard
asks for the API token once and keeps it in that browser's local storage. Build
without `VITE_RIPPERR_TOKEN` set, so no token is compiled into the bundle.

## The merge stage

Each word is assigned to whichever speaker segment it overlaps most in time.
Words overlapping nothing (breaths, VAD-clipped edges) inherit from a near
neighbour. Runs of same-speaker words then collapse into turns, splitting on
pauses longer than `max_turn_gap`.

## Caching and re-running

A worker's ASR and diarization output is written under `raw/`, keyed by the
episode and by the model run that produced it, and each transcript records
which run it came from. `ripperr remerge <id>` redoes only the glossary and
merge steps from that cache, so you can tune `max_turn_gap`, the orphan-word
logic or the glossary across a whole archive in seconds, without any models.
Re-running the models is a worker's job; Whisper is not deterministic, so a
re-run changes the transcript slightly.

## Known rough edges

- **Speaker labels are per-episode.** `SPEAKER_00` in one episode has no relation
  to `SPEAKER_00` in the next. A Senko worker uploads one CAM++
  centroid per episode speaker, stored as a `SpeakerEmbedding` sample.
  `ripperr profiles --feed ID` builds show-level centroids from recurring manual
  labels (at least two samples), and `speaker_matches()` can suggest names from
  those profiles without auto-applying a match. `guest_hints()` separately
  surfaces explicit guest-name phrases from episode context. Normal detail
  reads stay local; use `ripperr identify --feed ID --llm` to ask DeepInfra for
  additional candidates. It is not voice identification.
- **Overlapping speech** degrades attribution, as it does in every system.
- **Hallucination on silence.** `condition_on_previous_text=False` limits runaway
  repetition loops but doesn't eliminate them on long musical interludes.

Feeds are the reliable ingestion route; platform-specific episode links expire
and often sit behind authentication.
