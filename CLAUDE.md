# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**video-use** is a conversation-driven video editing skill for Claude Code and other AI agents. Users edit raw video footage through natural language; the system reads phrase-level transcripts rather than processing raw frames, keeping token usage minimal while enabling word-boundary-precise cuts.

## Setup

```bash
# Install Python dependencies
uv sync
# or
pip install -e .

# External hard requirements
# macOS:
brew install ffmpeg
# Debian/Ubuntu:
apt-get install ffmpeg

# ElevenLabs API key (for transcription)
cp .env.example .env
# then add ELEVENLABS_API_KEY=... to .env
```

Optional: `yt-dlp` (URL sources), Node.js 22+ (HyperFrames/Remotion animations). Animation engines (Manim, HyperFrames, Remotion) are installed per-project on first use, not globally.

## No Formal Test Suite or Linter

There is no pytest, ruff, mypy, or CI configuration. Verification is end-to-end: run helpers against real footage and inspect output.

## Helpers (the core executables)

All helpers live in `helpers/` and are invoked as `python helpers/<name>.py`:

| Helper | Purpose |
|--------|---------|
| `transcribe.py` | Single-file ElevenLabs Scribe transcription; results cached in `edit/transcripts/` |
| `transcribe_batch.py` | 4-worker parallel transcription for multi-take projects |
| `pack_transcripts.py` | Consolidates `transcripts/*.json` → `takes_packed.md` (phrase-level, ~12KB) |
| `timeline_view.py` | Generates filmstrip + waveform + word-label PNG for a time range |
| `render.py` | Final renderer: extract → concat → overlay → subtitles. Flags: `--preview`, `--build-subtitles`, `--no-subtitles`, `--vertical` |
| `grade.py` | Applies ffmpeg filter chains; presets: `warm_cinematic`, `neutral_punch`, `none`, or `--filter '<raw>'` |
| `export_resolve.py` | ProRes 422 HQ + FCPXML 1.9 + CMX 3600 EDL for DaVinci Resolve; flags: `--handles N`, `--zip` |

## Editing Pipeline

```
Transcribe → Pack → LLM reasons over takes_packed.md → edl.json → Render → Self-eval
                                                                          ↓
                                                                  Issue? Fix + re-render (max 3)
```

1. `ffprobe` sources, transcribe, pack to `takes_packed.md`
2. Pre-scan transcript for verbal slips
3. Converse with user; propose strategy in plain English; wait for confirmation
4. Sub-agent produces `edl.json`; use `timeline_view` at ambiguous cut points
5. Render at 720p (`--preview`) for fast iteration
6. Self-eval: `timeline_view` at every cut boundary; check for jumps, audio pops, subtitle obscuration
7. Persist session notes to `edit/project.md`

## Output Layout

All outputs go under `<videos_dir>/edit/`:

```
edit/
├── project.md          # session memory
├── takes_packed.md     # LLM reading view of transcripts
├── edl.json            # cut decisions + grade + overlays + subtitles
├── transcripts/        # cached raw Scribe JSON (one file per source)
├── animations/slot_<id>/
├── clips_graded/
├── master.srt          # subtitles in output-timeline space
├── downloads/          # yt-dlp outputs
├── verify/             # debug frames / timeline PNGs
├── preview.mp4
└── final.mp4
```

## 12 Hard Rules (non-negotiable for production correctness)

1. Subtitles applied **last** (after all overlays)
2. Per-segment extract → lossless `-c copy` concat; not a single-pass filtergraph
3. 30ms audio fades at every segment boundary
4. Overlays use `setpts=PTS-STARTPTS+T/TB`
5. Master SRT uses output-timeline offsets
6. **Never cut inside a word**
7. Pad cut edges (30–200ms working window)
8. Word-level verbatim ASR only (not phrase-mode SRT)
9. Cache transcripts per source
10. Parallel sub-agents for multiple animations
11. Strategy confirmation before execution
12. All session outputs in `<videos_dir>/edit/`

## Key Reference Files

- **`SKILL.md`** — complete editing workflow, hard rules, helpers API, animation techniques, subtitle styles, grade examples. Primary reference for all editing operations.
- **`install.md`** — first-time setup guide for end users.
- **`skills/manim-video/`** — vendored Manim animation sub-skill with its own SKILL.md and references for equations, animations, and visual design.

## Design Principles

- **Text-first**: transcript is the primary reasoning surface; `timeline_view` visuals are on-demand, not constant.
- **Audio-primary**: cuts come from speech boundaries and silence gaps.
- **Confirm before execute**: propose strategy in plain English, wait for user OK.
- **Self-evaluate**: run `timeline_view` on rendered output before reporting done.
- **Lazy installation**: install Node/Remotion/Manim only when the user's project needs them.
