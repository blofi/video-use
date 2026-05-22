# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repository Is

`video-use` is an LLM video-editing skill: a set of Python helpers plus a `SKILL.md` that an agent reads to edit video using ElevenLabs Scribe transcripts + ffmpeg. It is not a traditional app with a test suite — correctness is verified by running helpers on real video files.

## Setup

```bash
uv sync            # or: pip install -e .
cp .env.example .env   # add ELEVENLABS_API_KEY
```

System dependencies (not in pyproject): `ffmpeg`/`ffprobe` (required), `yt-dlp` (optional for URL sources).

## Helper Commands

All helpers are invoked directly as Python scripts — there are no console entry points.

```bash
python helpers/transcribe.py <video>                  # transcribe + cache one file
python helpers/transcribe_batch.py <videos_dir>       # parallel transcribe (4 workers)
python helpers/pack_transcripts.py --edit-dir <dir>   # JSON → phrase-level markdown
python helpers/timeline_view.py <video> <start> <end> # filmstrip + waveform PNG
python helpers/render.py <edl.json> -o <out.mp4>      # render from EDL
python helpers/render.py <edl.json> -o preview.mp4 --preview  # fast 720p preview
python helpers/grade.py <in.mp4> -o <out.mp4>         # apply color grade
```

## Architecture

The editing pipeline has two conceptual layers:

1. **Text layer** — ElevenLabs Scribe output (word-level timestamps, speaker diarization, audio events). Packed into `takes_packed.md` (~12 KB per session).
2. **Visual layer** — On-demand PNG composites (filmstrip + waveform + word labels) via `timeline_view.py`, used only at decision points.

**Pipeline flow:**
```
transcribe → pack → LLM reasons → EDL (edl.json) → render → self-eval → iterate → project.md
```

**Session artifacts** (all written under `<videos_dir>/edit/`, never inside this repo):
- `project.md` — persistent session memory; append each session
- `takes_packed.md` — primary reading view (phrase-level transcript)
- `transcripts/<name>.json` — cached raw Scribe output (immutable once written)
- `edl.json` — cut decisions, grade, overlay specs, subtitle path
- `clips_graded/` — per-segment extracts with color grade + 30ms audio fades
- `animations/slot_<id>/` — per-animation workspace
- `verify/` — debug timeline PNGs from self-eval
- `preview.mp4` / `final.mp4` — render outputs

## Key Production Rules

These are non-negotiable correctness invariants (full details in `SKILL.md`):

- Subtitles applied **last** in the ffmpeg filter chain (after all overlays)
- Per-segment extract → lossless `-c copy` concat — never single-pass to avoid double-encoding
- 30ms audio `afade` in/out at every segment boundary
- Overlays use `setpts=PTS-STARTPTS+T/TB` to shift frame 0 to the correct output-timeline position
- Master SRT uses output-timeline offsets, not source offsets
- Never cut inside a word; snap to transcript word boundaries
- Pad every cut edge 30–200ms (Scribe drifts 50–100ms)
- Transcripts are cached per source file; never re-transcribe an unchanged file
- All session outputs go in `<videos_dir>/edit/` — never write to this repo's directory

## Workflow Conventions

- **Audio-first**: cuts derive from speech boundaries + silence gaps, not visuals
- **Silence gaps**: ≥400ms are safest cut targets; 150–400ms need a visual check; <150ms unsafe
- **Strategy confirmation before execution**: never touch cuts until the user approves the plan
- **Self-eval loop**: after render, run `timeline_view.py` on the output; fix and re-render up to 3 passes
- **Animations**: spawn parallel sub-agents per slot (never sequential); choose engine per slot — HyperFrames (HTML/CSS/GSAP), Remotion (React), Manim (math diagrams), PIL (simple overlays)
- **Color grading**: per-segment ASC CDL (slope/offset/power per channel + saturation); presets: `warm_cinematic`, `neutral_punch`, `none`

## Vendored Skill

`skills/manim-video/` contains a separate Manim animation skill with its own `SKILL.md`. Read it when building a Manim animation slot.

## Skill Registration

When deploying, the **entire repo** is symlinked into the agent's skills directory (not just `SKILL.md`) so helpers are resolvable at runtime. See `install.md` for per-agent symlink paths (Claude Code, Codex, Hermes, Openclaw).
