# ripperr

Subscribe to podcast feeds (RSS or YouTube playlists), transcribe and diarize them
locally, store everything in SQLite, and keep model backends swappable. No UI or
cloud service is required. Apple Silicon and Linux are supported.
Meant to be used from Python (`ripperr.api.Ripperr`) as well as from the CLI.

```
RSS / YouTube ──▶ download ──▶ ffmpeg 16k mono ──┬──▶ ASR backend ──▶ words
                                                 └──▶ diarization backend ──▶ speaker segments
                                                                               │
                        glossary respelling ──▶ merge by temporal overlap ◀────┘
                                                         │
                                                   SQLite + FTS5
```

## Architecture

On Apple Silicon, **mlx-whisper** runs Whisper on the Metal GPU and **Senko** is a tuned fork of the
3D-Speaker pipeline (pyannote segmentation-3.0 for VAD, CAM++ for embeddings,
spectral or UMAP+HDBSCAN clustering) that runs both models through CoreML on
macOS instead of PyTorch. Roughly an hour of audio diarized in single-digit
seconds on an M3, versus minutes for pyannote on MPS.

On Linux, `faster-whisper` provides ASR and `pyannote.audio` provides speaker
diarization. The Linux diarization model is downloaded from Hugging Face and may
require accepting the model terms and setting `RIPPERR_HF_TOKEN`.

Both run on the same normalized 16 kHz mono WAV, so the conversion happens once.

## Setup

```bash
brew install ffmpeg                 # macOS
uv venv --python 3.13 && source .venv/bin/activate
uv pip install -e ".[apple]"        # Apple Silicon
# Linux: install ffmpeg with the system package manager, then use .[linux]
# uv pip install -e ".[linux]"
```

The `apple` extra installs Senko from a pinned git commit, the one this code was
tested against. Senko does have a PyPI release now (0.1.0), but its output shape has
changed between versions, so an exact pin is for reproducibility, not because
packaging requires it. To upgrade, bump the SHA in `pyproject.toml` and re-run a real
episode. Senko needs Python below 3.14, the Xcode Command Line Tools and macOS 14+.
Unlike pyannote's own pipeline, no Hugging Face token or gated-model acceptance is required.

## Usage

```bash
ripperr add https://example.com/feed.xml
ripperr sync                    # poll feeds, record new episodes
ripperr run --limit 3           # download + transcribe + diarize
ripperr show 12 --out ep12.md   # markdown transcript
ripperr search "interest rates"
ripperr status
```

Settings come from the environment:

| Variable | Effect |
| --- | --- |
| `RIPPERR_ROOT` | where the database, audio and cache live (default `~/ripperr`) |
| `RIPPERR_ASR_BACKEND` | `auto`, `mlx`, or `faster-whisper` |
| `RIPPERR_DIARIZATION_BACKEND` | `auto`, `senko`, or `pyannote` |
| `RIPPERR_DEVICE` | `auto`, `cpu`, or `cuda` |
| `RIPPERR_ASR_MODEL` | Whisper model (MLX default on Apple, `large-v3` on Linux) |
| `RIPPERR_DIARIZATION_MODEL` | pyannote model (default `pyannote/speaker-diarization-community-1`) |
| `RIPPERR_HF_TOKEN` | Hugging Face token for the Linux pyannote model |
| `RIPPERR_LANGUAGE` | force a language instead of auto-detecting |
| `RIPPERR_KEEP_AUDIO=0` | delete audio after processing |
| `RIPPERR_GLOSSARY` | glossary file (default `<root>/glossary.txt`) |

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

For unattended operation, a launchd agent or cron job running
`ripperr sync && ripperr run` is all you need.

## Python API

Other code should use `ripperr.api.Ripperr`, not the database:

```python
from ripperr.api import Ripperr

with Ripperr() as rip:
    rip.process(limit=3, glossary=["Bhayshul Tuten", "Drake Maye"])
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

Reading (`episodes`, `transcript`, `search`) needs only the base dependencies;
processing needs the matching `apple` or `linux` extra. Linux can run on CPU or
CUDA; set `RIPPERR_DEVICE=cuda` when the CUDA runtime is installed.

## Layout

| Module | Role |
| --- | --- |
| `api.py` | the public `Ripperr` class; CLI and other callers use only this |
| `models.py` | plain dataclasses returned by the API |
| `config.py` | paths, model choices and thresholds, overridable from the environment |
| `feeds.py` | RSS parsing, enclosure download |
| `youtube.py` | YouTube playlists, audio via yt-dlp |
| `glossary.py` | phonetic respelling of names from a supplied term list |
| `audio.py` | ffmpeg normalization to 16 kHz mono WAV |
| `asr.py` | ASR backends, word-level timestamp normalization |
| `diarize.py` | diarization backends, segment normalization |
| `merge.py` | word→speaker assignment, turn grouping |
| `pipeline.py` | orchestration, per-stage caching |
| `store.py` | SQLite schema, FTS5 search |

See the [editable architecture diagram](docs/architecture.drawio).

## The merge stage

Each word is assigned to whichever speaker segment it overlaps most in time.
Words overlapping nothing (breaths, VAD-clipped edges) inherit from a near
neighbour. Runs of same-speaker words then collapse into turns, splitting on
pauses longer than `max_turn_gap`.

## Caching and re-running

ASR and diarization output are written to `raw/<hash of guid>.{asr,diar}.json`
before merging. `ripperr remerge <id>` redoes only the glossary and merge steps
from that cache, so you can tune `max_turn_gap`, the orphan-word logic or the
glossary across a whole archive in seconds. `ripperr run --force` ignores the
cache and re-runs the models.

## Known rough edges

- **Senko's output field names.** `diarize.normalize_segments` probes several key
  aliases because the shape has moved between versions. If it comes back empty,
  print one raw segment and add the key you see.
- **Speaker labels are per-episode.** `SPEAKER_00` in one episode has no relation
  to `SPEAKER_00` in the next. Mapping labels to real names needs a speaker
  embedding store keyed to known voices — a natural next step, since CAM++
  embeddings are already computed and discarded.
- **Overlapping speech** degrades attribution, as it does in every system.
- **Hallucination on silence.** `condition_on_previous_text=False` limits runaway
  repetition loops but doesn't eliminate them on long musical interludes.

Feeds are the reliable ingestion route; platform-specific episode links expire
and often sit behind authentication.
