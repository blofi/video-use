"""Extract ProRes 422 clips with handles + OTIO timeline from a video-use EDL.

Handles broadcast MXF correctly:
  - Explicit stream mapping (0:v:0, 0:a:0) for multi-stream audio sources
  - yadif deinterlace for interlaced sources (--deinterlace)
  - Source timecode embedded in each clip for NLE conform
  - OTIO with correct absolute-TC coordinate system (Resolve-verified, all three bugs fixed)

Usage:
    python helpers/extract_clips.py edit/edl.json
    python helpers/extract_clips.py edit/edl.json --handles 1.0 --deinterlace
    python helpers/extract_clips.py edit/edl.json -o 3N_POLIMON01_edit.otio
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

import opentimelineio as otio


# -------- ffprobe helpers -----------------------------------------------------


def detect_fps(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True, check=True,
    )
    num, den = map(int, out.stdout.strip().split("/"))
    return num / den


def detect_duration(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def resolve_path(maybe_path: str, base: Path) -> Path:
    p = Path(maybe_path)
    if p.is_absolute():
        return p
    return (base / p).resolve()


# -------- Timecode helpers ----------------------------------------------------


def secs_to_tc(s: float, fps: float) -> str:
    """Convert seconds to non-drop-frame timecode HH:MM:SS:FF."""
    fps_int = round(fps)
    total = int(math.floor(s * fps))
    fr = total % fps_int
    sc = (total // fps_int) % 60
    mn = (total // fps_int // 60) % 60
    hr = total // fps_int // 3600
    return f"{hr:02d}:{mn:02d}:{sc:02d}:{fr:02d}"


def fr(s: float, fps: float) -> int:
    return int(math.floor(s * fps))


# -------- Clip extraction -----------------------------------------------------


def extract_clip(
    source: Path,
    edit_in: float,
    edit_out: float,
    handle_s: float,
    fps: float,
    src_duration: float,
    clip_name: str,
    out_path: Path,
    deinterlace: bool,
    audio_streams: int = 1,
) -> tuple[float, float]:
    """Extract one ProRes 422 clip with handles and embedded source TC.

    Returns (actual_h_in, actual_h_out) in source seconds.
    The .mov's embedded TC is set to h_in so OTIO available_range can match it.

    audio_streams: number of source audio streams to mix together.
      1 (default) → map 0:a:0 directly.
      N > 1 → amix all N streams into a single mono output.
      Broadcast Avid MXF news packages split VO and NAT/SOT onto separate
      tracks — pass audio_streams=2 (or however many are active) to get the
      complete programme audio.
    """
    h_in  = max(0.0,         edit_in  - handle_s)
    h_out = min(src_duration, edit_out + handle_s)
    tc    = secs_to_tc(3600.0 + h_in, fps)   # 01:00:00:00 base + offset (broadcast MXF source TC)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    mix_audio = audio_streams > 1

    if deinterlace or mix_audio:
        # Build filter_complex so video and audio chains can coexist.
        fc_parts: list[str] = []
        v_out = "0:v:0"
        if deinterlace:
            fc_parts.append("[0:v:0]yadif=mode=0:parity=0:deint=0[vout]")
            v_out = "[vout]"
        if mix_audio:
            inputs = "".join(f"[0:a:{i}]" for i in range(audio_streams))
            fc_parts.append(
                f"{inputs}amix=inputs={audio_streams}:duration=first:dropout_transition=0[aout]"
            )
            a_out = "[aout]"
        else:
            a_out = "0:a:0"
        fc_args = ["-filter_complex", ";".join(fc_parts)] if fc_parts else []
        map_args = ["-map", v_out, "-map", a_out]
    else:
        fc_args = []
        map_args = ["-map", "0:v:0", "-map", "0:a:0"]

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{h_in:.6f}",
        "-i", str(source),
        "-t", f"{h_out - h_in:.6f}",
        *fc_args,
        *map_args,
        "-c:v", "prores_ks", "-profile:v", "2",   # ProRes 422
        "-pix_fmt", "yuv422p10le",
        "-c:a", "pcm_s24le", "-ar", "48000",
        "-timecode", tc,                           # embed source TC for NLE conform
        "-metadata", f"reel_name={clip_name}",
        "-metadata", f"tape_name={clip_name}",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return h_in, h_out


# -------- OTIO generation -----------------------------------------------------
#
# Three bugs found in Resolve testing; all three are fixed here:
#
#   Bug 1 — wrong rate: every RationalTime must use rate=FPS. rate=24 on a 25fps
#            source causes frame-position drift proportional to distance from zero.
#
#   Bug 2 — no media_reference: without an explicit target_url, Resolve matches clips
#            by searching for a file whose embedded TC window contains source_range.
#            Adjacent handle clips have overlapping TC windows, so the lookup is
#            ambiguous — audio silently plays from the wrong source file.
#
#   Bug 3 — available_range.start_time=0: tells Resolve the file's TC starts at
#            00:00:00:00. But the extracted .mov has an embedded TC matching h_in.
#            Resolve compares the OTIO available_range against the file's actual TC,
#            finds no overlap, and does not place the clip's picture on the timeline.
#
# Correct coordinate system (all values in absolute source TC space):
#   available_range.start_time = tc_offset + floor(h_in * fps)         ← matches .mov embedded TC
#   available_range.duration   = floor((h_out-h_in) * fps)              ← total .mov length
#   source_range.start_time    = tc_offset + floor(edit_in * fps)       ← absolute in source
#   source_range.duration      = floor((edit_out-edit_in) * fps)
#
# tc_offset = round(fps) * 3600  (= 01:00:00:00 in frames; broadcast MXF default source TC)


def make_otio_clip(
    name: str,
    file: Path,
    edit_in: float,
    edit_out: float,
    h_in: float,
    h_out: float,
    fps: float,
    tc_offset_frames: int = 0,
) -> otio.schema.Clip:
    rate = fps
    media_ref = otio.schema.ExternalReference(
        target_url=file.resolve().as_uri(),
        available_range=otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(tc_offset_frames + fr(h_in, fps), rate),
            duration=otio.opentime.RationalTime(fr(h_out - h_in, fps), rate),
        ),
    )
    return otio.schema.Clip(
        name=name,
        media_reference=media_ref,
        source_range=otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(tc_offset_frames + fr(edit_in, fps), rate),
            duration=otio.opentime.RationalTime(fr(edit_out - edit_in, fps), rate),
        ),
    )


def write_otio(clips_data: list[dict], fps: float, timeline_name: str, out_path: Path) -> None:
    tc_offset_frames = round(fps) * 3600   # 01:00:00:00 — broadcast MXF source TC default

    video_track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    audio_track = otio.schema.Track(name="A1", kind=otio.schema.TrackKind.Audio)

    for d in clips_data:
        clip_v = make_otio_clip(**d, fps=fps, tc_offset_frames=tc_offset_frames)
        clip_a = make_otio_clip(**d, fps=fps, tc_offset_frames=tc_offset_frames)
        video_track.append(clip_v)
        audio_track.append(clip_a)

    timeline = otio.schema.Timeline(name=timeline_name)
    timeline.tracks.append(video_track)
    timeline.tracks.append(audio_track)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    otio.adapters.write_to_file(timeline, str(out_path))


# -------- Clip name derivation ------------------------------------------------


def clip_name(index: int, r: dict) -> str:
    label = r.get("beat") or r.get("source") or f"clip_{index + 1}"
    label = label.upper().replace(" ", "_").replace("/", "_")
    return f"{index + 1:02d}_{label}"


# -------- Main orchestrator ---------------------------------------------------


def run(
    edl_path: Path,
    handle_s: float = 1.0,
    fps_override: float | None = None,
    deinterlace: bool = False,
    audio_streams: int = 1,
    clips_dir_name: str = "clips",
    otio_out: Path | None = None,
    timeline_name: str | None = None,
) -> None:
    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent

    sources = edl["sources"]
    ranges  = edl["ranges"]

    # Detect per-source metadata
    src_meta: dict[str, dict] = {}
    for src_key, src_rel in sources.items():
        src_path = resolve_path(src_rel, edit_dir)
        fps   = fps_override or detect_fps(src_path)
        dur   = detect_duration(src_path)
        src_meta[src_key] = {"path": src_path, "fps": fps, "duration": dur}

    clips_dir = edit_dir / clips_dir_name
    clips_dir.mkdir(parents=True, exist_ok=True)

    if otio_out is None:
        otio_out = edit_dir / "timeline.otio"
    if timeline_name is None:
        timeline_name = edl_path.stem

    print(f"extracting {len(ranges)} clip(s) → {clips_dir}/")
    if deinterlace:
        print("  deinterlace: yadif=mode=0:parity=0:deint=0")
    if audio_streams > 1:
        print(f"  audio: amix of {audio_streams} streams (0:a:0 … 0:a:{audio_streams - 1})")

    clips_data: list[dict] = []

    for i, r in enumerate(ranges):
        src_key   = r["source"]
        meta      = src_meta[src_key]
        src_path  = meta["path"]
        fps       = meta["fps"]
        edit_in   = float(r["start"])
        edit_out  = float(r["end"])
        name      = clip_name(i, r)
        out_path  = clips_dir / f"{name}.mov"
        beat      = r.get("beat") or r.get("note") or ""

        print(f"  [{i + 1:02d}] {name}  {edit_in:.2f}–{edit_out:.2f}  ({edit_out - edit_in:.2f}s)  {beat}")

        h_in, h_out = extract_clip(
            source=src_path,
            edit_in=edit_in,
            edit_out=edit_out,
            handle_s=handle_s,
            fps=fps,
            src_duration=meta["duration"],
            clip_name=name,
            out_path=out_path,
            deinterlace=deinterlace,
            audio_streams=audio_streams,
        )
        clips_data.append({
            "name":    name,
            "file":    out_path,
            "edit_in":  edit_in,
            "edit_out": edit_out,
            "h_in":    h_in,
            "h_out":   h_out,
        })

    # Use fps from the first source for the timeline
    first_fps = src_meta[next(iter(sources))]["fps"]

    print(f"writing {otio_out.name} ({len(clips_data)} clips)")
    write_otio(clips_data, first_fps, timeline_name, otio_out)

    total_s = sum(d["edit_out"] - d["edit_in"] for d in clips_data)
    print(f"\ndone: {len(clips_data)} clips, {total_s:.1f}s timeline")
    print(f"      clips → {clips_dir}")
    print(f"      otio  → {otio_out}")


# -------- CLI -----------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Extract ProRes clips with handles + OTIO timeline from a video-use EDL"
    )
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument(
        "--handles", type=float, default=1.0, metavar="SECONDS",
        help="Handle duration in seconds on each side (default: 1.0 = 25 frames at 25fps)",
    )
    ap.add_argument(
        "--fps", type=float, default=None, metavar="FPS",
        help="Override frame rate detection (default: auto-detect from source)",
    )
    ap.add_argument(
        "--deinterlace", action="store_true",
        help="Apply yadif deinterlacing (use for interlaced MXF sources)",
    )
    ap.add_argument(
        "--audio-streams", type=int, default=1, metavar="N",
        help="Number of audio streams to amix together (default: 1). "
             "Use 2 for broadcast Avid MXF packages where VO is on ch1 and NAT/SOT is on ch2.",
    )
    ap.add_argument(
        "--clips-dir", default="clips", metavar="DIR",
        help="Subdirectory under edit dir for extracted clips (default: clips)",
    )
    ap.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output .otio path (default: <edit_dir>/timeline.otio)",
    )
    ap.add_argument(
        "--name", default=None, metavar="TITLE",
        help="Timeline name in the OTIO file (default: edl filename stem)",
    )
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    otio_out = args.output.resolve() if args.output else None

    run(
        edl_path=edl_path,
        handle_s=args.handles,
        fps_override=args.fps,
        deinterlace=args.deinterlace,
        audio_streams=args.audio_streams,
        clips_dir_name=args.clips_dir,
        otio_out=otio_out,
        timeline_name=args.name,
    )


if __name__ == "__main__":
    main()
