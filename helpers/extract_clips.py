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
) -> tuple[float, float]:
    """Extract one ProRes 422 clip with handles and embedded source TC.

    Returns (actual_h_in, actual_h_out) in source seconds.
    The .mov's embedded TC is set to h_in so OTIO available_range can match it.
    """
    h_in  = max(0.0,         edit_in  - handle_s)
    h_out = min(src_duration, edit_out + handle_s)
    tc    = secs_to_tc(3600.0 + h_in, fps)   # 01:00:00:00 base + offset (broadcast MXF source TC)

    vf_filters = ["yadif=mode=0:parity=0:deint=0"] if deinterlace else []
    vf_args = (["-vf", ",".join(vf_filters)] if vf_filters else [])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{h_in:.6f}",
        "-i", str(source),
        "-t", f"{h_out - h_in:.6f}",
        *vf_args,
        "-map", "0:v:0", "-map", "0:a:0",   # explicit: ignores secondary MXF streams
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
        clips_dir_name=args.clips_dir,
        otio_out=otio_out,
        timeline_name=args.name,
    )


if __name__ == "__main__":
    main()
