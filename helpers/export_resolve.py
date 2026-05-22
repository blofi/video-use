"""Export a DaVinci Resolve package from a video-use EDL.

Produces a zip containing:
  resolve_shots/
    shot_01_<source>.mp4  ← source extract with handles
    shot_02_<source>.mp4
    ...
    timeline.fcpxml       ← import into DaVinci Resolve 18+
    timeline.edl          ← CMX 3600 fallback
    README.txt

Usage:
    python helpers/export_resolve.py edit/edl.json
    python helpers/export_resolve.py edit/edl.json --handles 2.0
    python helpers/export_resolve.py edit/edl.json -o edit/resolve_package.zip
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


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


def detect_dimensions(video: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True,
    )
    w, h = map(int, out.stdout.strip().split(","))
    return w, h


def fps_to_frame_duration(fps: float) -> str:
    """Return FCPXML-style rational frameDuration string."""
    if abs(fps - round(fps)) < 0.02:
        return f"1/{round(fps)}s"
    if abs(fps - 30000 / 1001) < 0.02:
        return "1001/30000s"
    if abs(fps - 60000 / 1001) < 0.02:
        return "1001/60000s"
    return f"1/{fps:.4f}s"


def seconds_to_tc(seconds: float, fps: float) -> str:
    """Convert seconds to non-drop-frame timecode HH:MM:SS:FF."""
    fps_int = round(fps)
    total_frames = round(seconds * fps)
    frames = total_frames % fps_int
    total_secs = total_frames // fps_int
    secs = total_secs % 60
    mins = (total_secs // 60) % 60
    hours = total_secs // 3600
    return f"{hours:02d}:{mins:02d}:{secs:02d}:{frames:02d}"


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
    handle_s: float,
    src_duration: float,
    out_path: Path,
) -> tuple[float, float]:
    """Extract a shot with handles. Returns (actual_pre_s, actual_post_s)."""
    pre = min(handle_s, seg_start)
    post = min(handle_s, max(0.0, src_duration - seg_end))
    start = seg_start - pre
    duration = (seg_end - seg_start) + pre + post
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-i", str(source),
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return pre, post


# -------- FCPXML generation ---------------------------------------------------


def write_fcpxml(shots: list[dict], fps: float, width: int, height: int, out_path: Path) -> None:
    """Write a FCPXML 1.9 timeline referencing the handle-clipped shot files.

    Each shot dict must have: file (Path), pre_handle (float), post_handle (float),
    cut_duration (float), timeline_offset (float).

    The asset-clip start/duration trim points sit inside the handle clip so the
    editor has trim room in either direction up to the handle length.
    """
    root = ET.Element("fcpxml", version="1.9")
    resources = ET.SubElement(root, "resources")
    ET.SubElement(resources, "format",
        id="r1",
        frameDuration=fps_to_frame_duration(fps),
        width=str(width),
        height=str(height),
    )

    for i, shot in enumerate(shots):
        shot_dur = shot["pre_handle"] + shot["cut_duration"] + shot["post_handle"]
        ET.SubElement(resources, "asset",
            id=f"a{i + 1}",
            name=shot["file"].stem,
            src=shot["file"].resolve().as_uri(),
            start="0s",
            duration=f"{shot_dur:.4f}s",
            hasVideo="1",
            hasAudio="1",
            format="r1",
        )

    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", name="video-use export")
    project = ET.SubElement(event, "project", name="edit")
    total_dur = sum(s["cut_duration"] for s in shots)
    sequence = ET.SubElement(project, "sequence", format="r1", duration=f"{total_dur:.4f}s")
    spine = ET.SubElement(sequence, "spine")

    for i, shot in enumerate(shots):
        ET.SubElement(spine, "asset-clip",
            ref=f"a{i + 1}",
            name=shot["file"].stem,
            offset=f"{shot['timeline_offset']:.4f}s",
            start=f"{shot['pre_handle']:.4f}s",
            duration=f"{shot['cut_duration']:.4f}s",
        )

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(str(out_path), xml_declaration=True, encoding="UTF-8")


# -------- CMX 3600 EDL --------------------------------------------------------


def write_cmx_edl(shots: list[dict], fps: float, out_path: Path) -> None:
    """Write a CMX 3600 EDL (NON-DROP FRAME). Reel names are shot stems ≤7 chars."""
    lines = ["TITLE: video-use export", "FCM: NON-DROP FRAME", ""]
    for i, shot in enumerate(shots, start=1):
        reel = shot["file"].stem[:7]
        src_in = seconds_to_tc(shot["pre_handle"], fps)
        src_out = seconds_to_tc(shot["pre_handle"] + shot["cut_duration"], fps)
        rec_in = seconds_to_tc(shot["timeline_offset"], fps)
        rec_out = seconds_to_tc(shot["timeline_offset"] + shot["cut_duration"], fps)
        lines.append(f"{i:03d}  {reel:<8} V     C    {src_in} {src_out} {rec_in} {rec_out}")
        lines.append(f"* FROM CLIP NAME: {shot['file'].name}")
        lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))


# -------- README --------------------------------------------------------------


def write_readme(shots: list[dict], handle_s: float, out_path: Path) -> None:
    n = len(shots)
    lines = [
        "DaVinci Resolve Import Instructions",
        "====================================",
        "",
        "1. Unzip this package and keep all files in the same folder.",
        "2. Open DaVinci Resolve 18 or later.",
        "3. File > Import Timeline > Import AAF, EDL, XML...",
        "   Select:  timeline.fcpxml  (preferred — preserves clip names and trim room)",
        "   Or:      timeline.edl     (CMX 3600 fallback if FCPXML fails)",
        "4. When asked to locate media, point Resolve at this folder.",
        "",
        f"This package contains {n} shot clip{'s' if n != 1 else ''} with up to {handle_s:.1f}s",
        "of handle on each side. You can extend any cut in Resolve up to that",
        "amount in either direction without re-exporting.",
        "",
        "Generated by video-use (https://github.com/browser-use/video-use)",
    ]
    out_path.write_text("\n".join(lines))


# -------- Main orchestrator ---------------------------------------------------


def build_resolve_package(
    edl_path: Path,
    handle_s: float = 2.0,
    fps_override: float | None = None,
    out_zip: Path | None = None,
) -> Path:
    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent
    shots_dir = edit_dir / "resolve_shots"
    shots_dir.mkdir(parents=True, exist_ok=True)

    sources = edl["sources"]
    ranges = edl["ranges"]

    # Detect per-source fps and duration (cache per unique source path)
    src_meta: dict[str, dict] = {}
    for src_name, src_rel in sources.items():
        src_path = resolve_path(src_rel, edit_dir)
        if str(src_path) not in src_meta:
            fps = fps_override or detect_fps(src_path)
            dur = detect_duration(src_path)
            w, h = detect_dimensions(src_path)
            src_meta[str(src_path)] = {"fps": fps, "duration": dur, "width": w, "height": h}

    # Use fps + dimensions from the first source for the timeline
    first_src = resolve_path(next(iter(sources.values())), edit_dir)
    meta0 = src_meta[str(first_src)]
    fps = meta0["fps"]
    width, height = meta0["width"], meta0["height"]

    shots: list[dict] = []
    timeline_offset = 0.0

    print(f"extracting {len(ranges)} shot(s) with {handle_s:.1f}s handles → resolve_shots/")
    for i, r in enumerate(ranges):
        src_name = r["source"]
        src_path = resolve_path(sources[src_name], edit_dir)
        meta = src_meta[str(src_path)]
        seg_start = float(r["start"])
        seg_end = float(r["end"])
        cut_dur = seg_end - seg_start
        beat = r.get("beat") or r.get("note") or ""

        out_name = f"shot_{i + 1:02d}_{src_name}.mp4"
        out_path = shots_dir / out_name
        print(f"  [{i + 1:02d}] {src_name}  {seg_start:.2f}-{seg_end:.2f}  ({cut_dur:.2f}s)  {beat}")

        pre, post = extract_shot_with_handles(
            src_path, seg_start, seg_end, handle_s, meta["duration"], out_path
        )
        shots.append({
            "file": out_path,
            "pre_handle": pre,
            "post_handle": post,
            "cut_duration": cut_dur,
            "timeline_offset": timeline_offset,
        })
        timeline_offset += cut_dur

    # Write FCPXML and EDL
    fcpxml_path = shots_dir / "timeline.fcpxml"
    edl_out_path = shots_dir / "timeline.edl"
    readme_path = shots_dir / "README.txt"

    print(f"writing timeline.fcpxml ({len(shots)} clips, {timeline_offset:.1f}s total)")
    write_fcpxml(shots, fps, width, height, fcpxml_path)
    print("writing timeline.edl (CMX 3600 fallback)")
    write_cmx_edl(shots, fps, edl_out_path)
    write_readme(shots, handle_s, readme_path)

    # Zip everything
    if out_zip is None:
        out_zip = edit_dir / "resolve_package.zip"
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for shot in shots:
            zf.write(shot["file"], f"resolve_shots/{shot['file'].name}")
        zf.write(fcpxml_path, "resolve_shots/timeline.fcpxml")
        zf.write(edl_out_path, "resolve_shots/timeline.edl")
        zf.write(readme_path, "resolve_shots/README.txt")

    size_mb = out_zip.stat().st_size / (1024 * 1024)
    print(f"\ndone: {out_zip} ({size_mb:.1f} MB)")
    print(f"      {len(shots)} shots, {timeline_offset:.1f}s timeline, {handle_s:.1f}s handles")
    return out_zip


# -------- CLI -----------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Export a DaVinci Resolve package from a video-use EDL")
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument(
        "--handles", type=float, default=2.0, metavar="S",
        help="Handle duration in seconds on each side of every cut (default: 2.0)",
    )
    ap.add_argument(
        "--fps", type=float, default=None, metavar="FPS",
        help="Override frame rate detection (default: auto-detect from source)",
    )
    ap.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output zip path (default: <edit_dir>/resolve_package.zip)",
    )
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    out_zip = args.output.resolve() if args.output else None
    build_resolve_package(edl_path, handle_s=args.handles, fps_override=args.fps, out_zip=out_zip)


if __name__ == "__main__":
    main()
