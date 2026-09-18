# ripperr

Subscribe to podcast feeds, transcribe and diarize them locally on Apple Silicon,
store everything in SQLite. No UI, no cloud, no CUDA.

```
RSS feed ──▶ download ──▶ ffmpeg 16k mono ──┬──▶ mlx-whisper (Metal) ──▶ words
                                            └──▶ Senko (CoreML)     ──▶ speaker segments
                                                                          │
                                       merge by temporal overlap ◀────────┘
                                                    │
                                              SQLite + FTS5
```

## Why these pieces

**mlx-whisper** runs Whisper on the Metal GPU. **Senko** is a tuned fork of the
3D-Speaker pipeline (pyannote segmentation-3.0 for VAD, CAM++ for embeddings,
spectral or UMAP+HDBSCAN clustering) that runs both models through CoreML on
macOS instead of PyTorch. Roughly an hour of audio diarized in single-digit
seconds on an M3, versus minutes for pyannote on MPS.

Both run on the same normalized 16 kHz mono WAV, so the conversion happens once.

## Setup

```bash
brew install ffmpeg
uv venv --python 3.13 && source .venv/bin/activate
uv pip install -e ".[apple]"    # includes mlx-whisper
uv pip install "git+https://github.com/narcotic-sh/senko.git"
```

Senko needs the Xcode Command Line Tools and macOS 14+. Unlike pyannote's own
pipeline, no Hugging Face token or gated-model acceptance is required.

## Usage

```bash
ripperr add https://example.com/feed.xml
ripperr sync                    # poll feeds, record new episodes
ripperr run --limit 3           # download + transcribe + diarize
ripperr show 12 --out ep12.md   # markdown transcript
ripperr search "interest rates"
ripperr status
```

Set `RIPPERR_ROOT` to move the database and audio elsewhere (defaults to
`~/ripperr`). `RIPPERR_ASR_MODEL` swaps the Whisper model;
`RIPPERR_KEEP_AUDIO=0` deletes audio after processing.

### YouTube

Playlist URLs work as feeds (`uv pip install -e ".[youtube]"` for yt-dlp and its
deno JS runtime). Audio is fetched with yt-dlp and transcribed like any other
episode. `RIPPERR_YT_CAPTIONS=1` uses YouTube's auto-captions instead of Whisper
to skip the transcription pass; they have word timing but no speakers, so
diarization still runs, and they garble proper nouns more than Whisper does.

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
compare revisions. The CLI is a client of this same class.

## Layout

| Module | Role |
| --- | --- |
| `api.py` | the public `Ripperr` class; CLI and other callers use only this |
| `models.py` | plain dataclasses returned by the API |
| `feeds.py` | RSS parsing, enclosure download |
| `youtube.py` | YouTube playlists, audio via yt-dlp, optional captions |
| `glossary.py` | phonetic respelling of names from a supplied term list |
| `audio.py` | ffmpeg normalization to 16 kHz mono WAV |
| `asr.py` | mlx-whisper, word-level timestamps |
| `diarize.py` | Senko wrapper, segment normalization |
| `merge.py` | word→speaker assignment, turn grouping |
| `pipeline.py` | orchestration, per-stage caching |
| `store.py` | SQLite schema, FTS5 search |

## The merge stage

This is where transcript quality is actually won or lost, and it's the part most
write-ups skip. Whisper and the diarizer segment the audio independently —
Whisper follows linguistic units, the diarizer follows acoustic ones — and they
disagree most at turn boundaries, which is exactly where errors are most visible.

Each word is assigned to whichever speaker segment it overlaps most in time.
Words overlapping nothing (breaths, VAD-clipped edges) inherit from a near
neighbour. Runs of same-speaker words then collapse into turns, splitting on
pauses longer than `max_turn_gap`.

Word-level timestamps are load-bearing here. Segment-level assignment fails
routinely, because a single Whisper segment often spans a speaker change.

## Caching and re-running

ASR and diarization output are written to `raw/<hash of guid>.{asr,diar}.json`
before merging. `ripperr remerge <id>` redoes only the merge from that cache, so
you can tune `max_turn_gap` or the orphan-word logic across a whole archive in
seconds. `ripperr run --force` ignores the cache and re-runs the models.

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
