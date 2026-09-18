# podpipe

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
podpipe add https://example.com/feed.xml
podpipe sync                    # poll feeds, record new episodes
podpipe run --limit 3           # download + transcribe + diarize
podpipe show 12 --out ep12.md   # markdown transcript
podpipe search "interest rates"
podpipe status
```

Set `PODPIPE_ROOT` to move the database and audio elsewhere (defaults to
`~/podpipe`). `PODPIPE_ASR_MODEL` swaps the Whisper model;
`PODPIPE_KEEP_AUDIO=0` deletes audio after processing.

### YouTube

Playlist URLs work as feeds (`uv pip install -e ".[youtube]"` for yt-dlp and its
deno JS runtime). Audio is fetched with yt-dlp and transcribed like any other
episode. `PODPIPE_YT_CAPTIONS=1` uses YouTube's auto-captions instead of Whisper
to skip the transcription pass; they have word timing but no speakers, so
diarization still runs, and they garble proper nouns more than Whisper does.

### Player names

Whisper spells names by ear ("Drake May", "Basial Tootin"). `podpipe players`
builds a roster of active QB/RB/WR/TE/K from Sleeper's public API (or
`--from players.json` to reuse a copy) and saves it as `players.txt` in the data
directory. When that file exists, capitalised word pairs that sound like a roster
name (metaphone, cutoff `Config.name_match`) are respelled before the merge, and
each swap is printed. It only fixes full first+last pairs, so lone surnames pass
through, and it can misfire on non-roster people such as coaches; check the log.
Raw ASR output is never modified, so `podpipe remerge <id>` re-applies it after
you change the roster or cutoff.

For unattended operation, a launchd agent or cron job running
`podpipe sync && podpipe run` is all you need.

## Layout

| Module | Role |
| --- | --- |
| `feeds.py` | RSS parsing, enclosure download |
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

ASR and diarization output are written to `raw/<episode_id>.{asr,diar}.json`
before merging. `podpipe remerge <id>` redoes only the merge from that cache, so
you can tune `max_turn_gap` or the orphan-word logic across a whole archive in
seconds. `podpipe run --force` ignores the cache and re-runs the models.

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
