"""Export a NLE package from a video-use EDL using OpenTimelineIO.

Produces a folder (or zip) containing:
  resolve_shots/
    shot_01_<source>.mov  ← ProRes 422 HQ extract with handles
    shot_02_<source>.mov
    ...
    timeline.otio          ← import into any OTIO-compatible NLE
    README.txt

Usage:
    python helpers/export_resolve.py edit/edl.json
    python helpers/export_resolve.py edit/edl.json --handles 2.0
    python helpers/export_resolve.py edit/edl.json -o edit/package.zip
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import zipfile
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


# -------- Shot extraction -----------------------------------------------------


def extract_shot_with_handles(
    source: Path,
    seg_start: float,
    seg_end: float,
    handle_frames: int,
    fps: float,
    src_duration: float,
    out_path: Path,
) -> tuple[float, float]:
    """Extract a ProRes 422 HQ shot with frame-accurate handles.

    Returns (actual_pre_s, actual_post_s) — may be shorter near source boundaries.
    """
    handle_s = handle_frames / fps
    pre = min(handle_s, seg_start)
    post = min(handle_s, max(0.0, src_duration - seg_end))
    start = seg_start - pre
    duration = (seg_end - seg_start) + pre + post
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.6f}",
        "-i", str(source),
        "-t", f"{duration:.6f}",
        "-c:v", "prores_ks", "-profile:v", "3",  # ProRes 422 HQ
        "-pix_fmt", "yuv422p10le",
        "-c:a", "pcm_s24le",
        "-ar", "48000",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return pre, post


# -------- OTIO generation -----------------------------------------------------


def write_otio(shots: list[dict], fps: float, out_path: Path) -> None:
    """Write an OpenTimelineIO .otio file referencing the handle-clipped shot files.

    Each shot dict must have: file (Path), pre_handle (float), post_handle (float),
    cut_duration (float), timeline_offset (float).

    source_range trims into the handle clip so the editor has room to extend cuts.
    """
    rate = fps
    video_track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)

    for i, shot in enumerate(shots):
        total_duration_s = shot["pre_handle"] + shot["cut_duration"] + shot["post_handle"]
        media_ref = otio.schema.ExternalReference(
            target_url=shot["file"].resolve().as_uri(),
            available_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(0, rate),
                duration=otio.opentime.RationalTime(round(total_duration_s * rate), rate),
            ),
        )
        clip = otio.schema.Clip(
            name=shot["file"].stem,
            media_reference=media_ref,
            source_range=otio.opentime.TimeRange(
                start_time=otio.opentime.RationalTime(round(shot["pre_handle"] * rate), rate),
                duration=otio.opentime.RationalTime(round(shot["cut_duration"] * rate), rate),
            ),
        )
        video_track.append(clip)

    timeline = otio.schema.Timeline(name="video-use export")
    timeline.tracks.append(video_track)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    otio.adapters.write_to_file(timeline, str(out_path))


# -------- README --------------------------------------------------------------


def write_readme(shots: list[dict], handle_frames: int, fps: float, out_path: Path) -> None:
    n = len(shots)
    handle_s = handle_frames / fps
    lines = [
        "NLE Import Instructions",
        "=======================",
        "",
        "1. Keep all files in this folder.",
        "2. Open your NLE (DaVinci Resolve 18+, Final Cut Pro, Premiere Pro, etc.).",
        "3. Import timeline.otio:",
        "   DaVinci Resolve: File > Import Timeline > Import AAF, EDL, XML...",
        "   Final Cut Pro:   install the OTIO plugin from OpenTimelineIO releases.",
        "   Premiere Pro:    use the otio2premiere adapter or Adobe's OTIO panel.",
        "4. When asked to locate media, point to this folder.",
        "",
        f"This package contains {n} shot clip{'s' if n != 1 else ''} encoded as ProRes 422 HQ.",
        f"Each clip has up to {handle_frames} frames ({handle_s:.2f}s at {fps:.2f} fps) of handle",
        "on each side. You can extend any cut in the NLE up to that amount without",
        "re-exporting.",
        "",
        "Generated by video-use (https://github.com/browser-use/video-use)",
    ]
    out_path.write_text("\n".join(lines))


# -------- Main orchestrator ---------------------------------------------------


def build_package(
    edl_path: Path,
    handle_frames: int = 25,
    fps_override: float | None = None,
    make_zip: bool = False,
    out_zip: Path | None = None,
) -> Path:
    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent
    shots_dir = edit_dir / "resolve_shots"
    shots_dir.mkdir(parents=True, exist_ok=True)

    sources = edl["sources"]
    ranges = edl["ranges"]

    src_meta: dict[str, dict] = {}
    for src_rel in sources.values():
        src_path = resolve_path(src_rel, edit_dir)
        if str(src_path) not in src_meta:
            fps = fps_override or detect_fps(src_path)
            dur = detect_duration(src_path)
            src_meta[str(src_path)] = {"fps": fps, "duration": dur}

    first_src = resolve_path(next(iter(sources.values())), edit_dir)
    fps = src_meta[str(first_src)]["fps"]

    shots: list[dict] = []
    timeline_offset = 0.0

    print(f"extracting {len(ranges)} shot(s) — ProRes 422 HQ, {handle_frames}-frame handles → resolve_shots/")
    for i, r in enumerate(ranges):
        src_name = r["source"]
        src_path = resolve_path(sources[src_name], edit_dir)
        meta = src_meta[str(src_path)]
        seg_start = float(r["start"])
        seg_end = float(r["end"])
        cut_dur = seg_end - seg_start
        beat = r.get("beat") or r.get("note") or ""

        out_name = f"shot_{i + 1:02d}_{src_name}.mov"
        out_path = shots_dir / out_name
        print(f"  [{i + 1:02d}] {src_name}  {seg_start:.2f}-{seg_end:.2f}  ({cut_dur:.2f}s)  {beat}")

        pre, post = extract_shot_with_handles(
            src_path, seg_start, seg_end, handle_frames, meta["fps"], meta["duration"], out_path
        )
        shots.append({
            "file": out_path,
            "pre_handle": pre,
            "post_handle": post,
            "cut_duration": cut_dur,
            "timeline_offset": timeline_offset,
        })
        timeline_offset += cut_dur

    otio_path = shots_dir / "timeline.otio"
    readme_path = shots_dir / "README.txt"

    print(f"writing timeline.otio ({len(shots)} clips, {timeline_offset:.1f}s total)")
    write_otio(shots, fps, otio_path)
    write_readme(shots, handle_frames, fps, readme_path)

    print(f"\ndone: {shots_dir}")
    print(f"      {len(shots)} ProRes shots, {timeline_offset:.1f}s timeline, {handle_frames}-frame handles")

    if make_zip:
        if out_zip is None:
            out_zip = edit_dir / "resolve_package.zip"
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for shot in shots:
                zf.write(shot["file"], f"resolve_shots/{shot['file'].name}")
            zf.write(otio_path, "resolve_shots/timeline.otio")
            zf.write(readme_path, "resolve_shots/README.txt")
        size_mb = out_zip.stat().st_size / (1024 * 1024)
        print(f"      zipped → {out_zip} ({size_mb:.1f} MB)")
        return out_zip

    return shots_dir


# -------- CLI -----------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Export a NLE package from a video-use EDL")
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument(
        "--handles", type=int, default=25, metavar="FRAMES",
        help="Handle length in frames on each side of every cut (default: 25)",
    )
    ap.add_argument(
        "--fps", type=float, default=None, metavar="FPS",
        help="Override frame rate detection (default: auto-detect from source)",
    )
    ap.add_argument(
        "--zip", action="store_true",
        help="Also produce a resolve_package.zip alongside the folder",
    )
    ap.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output zip path when --zip is set (default: <edit_dir>/resolve_package.zip)",
    )
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    out_zip = args.output.resolve() if args.output else None
    build_package(
        edl_path,
        handle_frames=args.handles,
        fps_override=args.fps,
        make_zip=args.zip,
        out_zip=out_zip,
    )


if __name__ == "__main__":
    main()
