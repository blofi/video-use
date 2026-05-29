"""Export a NLE package from a video-use EDL using OpenTimelineIO.

Produces a folder (or zip) containing:
  resolve_shots/
    shot_01_<source>.mov  <- ProRes 422 HQ extract with handles + embedded TC
    shot_02_<source>.mov
    ...
    timeline.otio          <- import into any OTIO-compatible NLE
    README.txt

Usage:
    python helpers/export_resolve.py edit/edl.json
    python helpers/export_resolve.py edit/edl.json --handles 25
    python helpers/export_resolve.py edit/edl.json -o edit/package.zip
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import zipfile
from datetime import datetime
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


def count_audio_streams(source: Path) -> int:
    """Return the number of audio streams in source (0 if none/error)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(source)],
            capture_output=True, text=True, check=True,
        )
        return len([ln for ln in out.stdout.splitlines() if ln.strip()])
    except Exception:
        return 0


def resolve_path(maybe_path: str, base: Path) -> Path:
    p = Path(maybe_path)
    if p.is_absolute():
        return p
    return (base / p).resolve()


def secs_to_tc(s: float, fps: float) -> str:
    fps_int = round(fps)
    total = int(math.floor(s * fps))
    fr = total % fps_int
    sc = (total // fps_int) % 60
    mn = (total // fps_int // 60) % 60
    hr = total // fps_int // 3600
    return f"{hr:02d}:{mn:02d}:{sc:02d}:{fr:02d}"


def fr(s: float, fps: float) -> int:
    return int(math.floor(s * fps))


# -------- Shot extraction -----------------------------------------------------


def get_video_dimensions(source: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,coded_width,coded_height",
         "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
        capture_output=True, text=True, check=True,
    )
    vals = [int(v) for v in out.stdout.split() if v.strip().lstrip("-").isdigit() and int(v) > 0]
    if len(vals) >= 2:
        return vals[0], vals[1]
    return 1920, 1080


def vertical_crop_params(src_w: int, src_h: int, x_crop: float) -> tuple[int, int, int, int]:
    """Return (crop_w, crop_h, x, y) for a 9:16 vertical window from a 16:9 source."""
    crop_w = (src_h * 9 // 16) & ~1   # must be even
    crop_h = src_h & ~1
    x = int((src_w - crop_w) * max(0.0, min(1.0, x_crop))) & ~1
    return crop_w, crop_h, x, 0


def extract_shot_with_handles(
    source: Path,
    seg_start: float,
    seg_end: float,
    handle_frames: int,
    fps: float,
    src_duration: float,
    shot_name: str,
    out_path: Path,
    crop: tuple[int, int, int, int] | None = None,
    audio_streams: int = 1,
) -> tuple[float, float]:
    """Extract a ProRes 422 HQ shot with handles and embedded source TC.

    Returns (actual_h_in, actual_h_out) in source seconds.
    If crop=(w,h,x,y) is provided, a crop filter is applied (for vertical exports).
    audio_streams>1 folds all source streams into ONE centered mono mix per clip
    (e.g. broadcast MXF: VO on 0:a:0, NAT/SOT on 0:a:1). Broadcast packages keep
    VO and SOT on time-disjoint buses, so a single mix is always audible regardless
    of NLE monitoring - unlike discrete channels, which leave each clip one-sided.
    """
    handle_s = handle_frames / fps
    h_in  = max(0.0,         seg_start - handle_s)
    h_out = min(src_duration, seg_end   + handle_s)
    tc    = "01:00:00:00"   # always fixed - we own these clips

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Build crop (video) and the audio mix in a single -filter_complex so the two
    # never collide with -vf. For multi-stream sources (broadcast MXF: VO + NAT/SOT)
    # amix folds the buses into one centered program track. normalize=0 keeps each
    # bus at full level (they're disjoint, so no summing overlap to clip); duration=
    # longest spans the whole clip even if a stream is short.
    fc_parts: list[str] = []
    vmap = "0:v:0"
    if crop:
        fc_parts.append(f"[0:v:0]crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]}[vout]")
        vmap = "[vout]"
    if audio_streams > 1:
        inputs = "".join(f"[0:a:{i}]" for i in range(audio_streams))
        fc_parts.append(
            f"{inputs}amix=inputs={audio_streams}:duration=longest:"
            f"dropout_transition=0:normalize=0[aout]"
        )
        amap = "[aout]"
    else:
        amap = "0:a:0?"

    filter_args = ["-filter_complex", ";".join(fc_parts)] if fc_parts else []
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{h_in:.6f}",
        "-i", str(source),
        "-t", f"{h_out - h_in:.6f}",
        *filter_args,
        "-map", vmap, "-map", amap,
        "-c:v", "prores_ks", "-profile:v", "3",  # ProRes 422 HQ
        "-pix_fmt", "yuv422p10le",
        "-c:a", "pcm_s24le", "-ar", "48000",
        "-timecode", tc,
        "-metadata", f"reel_name={shot_name}",
        "-metadata", f"tape_name={shot_name}",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return h_in, h_out


# -------- OTIO generation -----------------------------------------------------
#
# Every exported clip is embedded with TC 01:00:00:00 regardless of where it
# came from in the source. We own these files, so we fix the base unconditionally.
#
#   available_range.start_time = tc_offset                               <- always 01:00:00:00
#   available_range.duration   = floor((h_out - h_in) * fps)
#   source_range.start_time    = tc_offset + floor((seg_start - h_in) * fps)  <- offset into clip
#   source_range.duration      = floor(cut_duration * fps)
#
# tc_offset = round(fps) * 3600  (frames for 01:00:00:00)


def write_otio(shots: list[dict], fps: float, out_path: Path) -> None:
    """Write an OTIO file with video and audio tracks.

    Each shot dict must have: file (Path), seg_start (float), cut_duration (float),
    h_in (float), h_out (float).
    """
    rate = fps
    tc_offset = round(fps) * 3600   # frames for 01:00:00:00 - matches fixed embedded TC

    video_track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    audio_track = otio.schema.Track(name="A1", kind=otio.schema.TrackKind.Audio)

    for shot in shots:
        edit_in_offset = fr(shot["seg_start"] - shot["h_in"], fps)   # frames from clip start to edit-in
        for track in (video_track, audio_track):
            media_ref = otio.schema.ExternalReference(
                target_url=shot["file"].resolve().as_uri(),
                available_range=otio.opentime.TimeRange(
                    start_time=otio.opentime.RationalTime(tc_offset, rate),
                    duration=otio.opentime.RationalTime(fr(shot["h_out"] - shot["h_in"], fps), rate),
                ),
            )
            clip = otio.schema.Clip(
                name=shot["file"].stem,
                media_reference=media_ref,
                source_range=otio.opentime.TimeRange(
                    start_time=otio.opentime.RationalTime(tc_offset + edit_in_offset, rate),
                    duration=otio.opentime.RationalTime(fr(shot["cut_duration"], fps), rate),
                ),
            )
            track.append(clip)

    timeline = otio.schema.Timeline(name="video-use export")
    timeline.tracks.append(video_track)
    timeline.tracks.append(audio_track)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    otio.adapters.write_to_file(timeline, str(out_path))


# -------- README --------------------------------------------------------------


def write_readme(shots: list[dict], handle_frames: int, fps: float, out_path: Path,
                 audio_streams: int = 1) -> None:
    n = len(shots)
    handle_s = handle_frames / fps
    audio_note = (
        [
            "",
            f"Audio: the source's {audio_streams} audio buses (reporter VO + NAT/SOT) are folded",
            "into one centered mono mix per clip, so every clip is audible regardless of",
            "monitoring. The buses are time-disjoint in the source, so nothing is lost.",
        ]
        if audio_streams > 1 else []
    )
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
        *audio_note,
        "",
        "Generated by Video-Chainsaw",
    ]
    out_path.write_text("\n".join(lines))


# -------- Main orchestrator ---------------------------------------------------


def load_shot_cuts(edit_dir: Path, src_stem: str, range_start: float, range_end: float) -> list[float]:
    """Return shot boundary times within [range_start, range_end] from cached shot file.

    Returns a list of split points *between* the range start and end (exclusive of both
    endpoints), sorted ascending. Empty list means no intra-range cuts detected.
    """
    cache_path = edit_dir / "shots" / f"{src_stem}.json"
    if not cache_path.exists():
        return []
    try:
        data = json.loads(cache_path.read_text())
        cuts = data.get("cuts", [])
        # Keep cuts strictly inside the range (not at the endpoints themselves)
        return sorted(
            c["time"] for c in cuts
            if range_start + 0.1 < c["time"] < range_end - 0.1
        )
    except Exception:
        return []


def build_package(
    edl_path: Path,
    handle_frames: int = 25,
    fps_override: float | None = None,
    make_zip: bool = False,
    out_zip: Path | None = None,
    vertical: bool = False,
    split_shots: bool = False,
    audio_streams: int | None = None,
) -> Path:
    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent

    # Broadcast MXF packages split audio across streams (VO + NAT/SOT). Honour the
    # EDL's audio_streams so all active buses are folded into one centered mix per
    # clip (see extract_shot_with_handles).
    if audio_streams is None:
        audio_streams = int(edl.get("audio_streams", 1))

    if vertical:
        folder_name = "resolve_shots_vertical"
    elif split_shots:
        # Timestamp the split folder so every export lands at a unique path. The
        # OTIO references media by that path, so Resolve treats each re-export as
        # new media instead of reusing a stale (possibly broken) audio conform.
        folder_name = f"resolve_shots_split_{datetime.now():%Y%m%d_%H%M%S}"
    else:
        folder_name = "resolve_shots"

    shots_dir = edit_dir / folder_name
    shots_dir.mkdir(parents=True, exist_ok=True)

    sources = edl["sources"]
    ranges = edl["ranges"]

    src_meta: dict[str, dict] = {}
    for src_key, src_rel in sources.items():
        src_path = resolve_path(src_rel, edit_dir)
        fps = fps_override or detect_fps(src_path)
        dur = detect_duration(src_path)
        w, h = get_video_dimensions(src_path) if vertical else (0, 0)
        na = count_audio_streams(src_path)
        src_meta[src_key] = {"path": src_path, "fps": fps, "duration": dur, "w": w, "h": h, "na": na}

    fps = src_meta[next(iter(sources))]["fps"]

    shots: list[dict] = []
    timeline_offset = 0.0
    clip_idx = 0

    if vertical:
        label = "vertical 9:16"
    elif split_shots:
        label = "16:9 split at shot boundaries"
    else:
        label = "16:9"
    audio_label = f", {audio_streams} audio buses folded to mix" if audio_streams > 1 else ""
    print(f"extracting {len(ranges)} range(s) - ProRes 422 HQ {label}, {handle_frames}-frame handles{audio_label} -> {folder_name}/")
    for i, r in enumerate(ranges):
        src_name = r["source"]
        meta = src_meta[src_name]
        src_path = meta["path"]
        seg_start = float(r["start"])
        seg_end = float(r["end"])
        beat = r.get("beat") or r.get("note") or ""

        # Build sub-segments list: (start, end, x_crop)
        sub_segs: list[tuple[float, float, float]] = []
        if vertical:
            # Split at sub_crops boundaries, each with its own x_crop
            sub_crops = r.get("sub_crops") or []
            if len(sub_crops) > 1:
                sub_crops_sorted = sorted(sub_crops, key=lambda c: c["offset"])
                for j, sc in enumerate(sub_crops_sorted):
                    ss = seg_start + sc["offset"]
                    se = seg_start + sub_crops_sorted[j + 1]["offset"] if j + 1 < len(sub_crops_sorted) else seg_end
                    sub_segs.append((ss, se, float(sc["x_crop"])))
            else:
                x_crop = float(sub_crops[0]["x_crop"] if sub_crops else r.get("x_crop", 0.5))
                sub_segs.append((seg_start, seg_end, x_crop))
        elif split_shots:
            # Split at detected shot boundaries from cached scdet results
            src_path_for_shots = meta["path"]
            cut_times = load_shot_cuts(edit_dir, src_path_for_shots.stem, seg_start, seg_end)
            boundaries = [seg_start] + cut_times + [seg_end]
            for j in range(len(boundaries) - 1):
                sub_segs.append((boundaries[j], boundaries[j + 1], 0.5))
        else:
            sub_segs.append((seg_start, seg_end, 0.5))

        for j, (ss, se, x_crop) in enumerate(sub_segs):
            clip_idx += 1
            cut_dur = se - ss
            suffix = f"_s{j + 1:02d}" if len(sub_segs) > 1 else ""
            shot_name = f"shot_{clip_idx:02d}_{src_name}{suffix}"
            crop = vertical_crop_params(meta["w"], meta["h"], x_crop) if vertical else None
            out_path = shots_dir / f"{shot_name}.mov"
            sub_label = f" sub-shot {j + 1}/{len(sub_segs)}" if len(sub_segs) > 1 else ""
            print(f"  [{clip_idx:02d}] {src_name}{sub_label}  {ss:.2f}-{se:.2f}  ({cut_dur:.2f}s)  {beat}")

            # Clamp to the streams this source actually has so amerge can't fail.
            eff_audio = max(1, min(audio_streams, meta["na"])) if meta["na"] else 1
            h_in, h_out = extract_shot_with_handles(
                src_path, ss, se, handle_frames, meta["fps"], meta["duration"],
                shot_name, out_path, crop=crop, audio_streams=eff_audio,
            )
            shots.append({
                "file": out_path,
                "seg_start": ss,
                "cut_duration": cut_dur,
                "h_in": h_in,
                "h_out": h_out,
                "timeline_offset": timeline_offset,
            })
            timeline_offset += cut_dur

    otio_path = shots_dir / "timeline.otio"
    readme_path = shots_dir / "README.txt"

    print(f"writing timeline.otio ({len(shots)} clips, {timeline_offset:.1f}s total)")
    write_otio(shots, fps, otio_path)
    write_readme(shots, handle_frames, fps, readme_path, audio_streams=audio_streams)

    print(f"\ndone: {shots_dir}")
    print(f"      {len(shots)} ProRes shots, {timeline_offset:.1f}s timeline, {handle_frames}-frame handles")

    if make_zip:
        if out_zip is None:
            if vertical:
                zip_name = "resolve_package_vertical.zip"
            elif split_shots:
                zip_name = "resolve_package_shots.zip"
            else:
                zip_name = "resolve_package.zip"
            out_zip = edit_dir / zip_name
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for shot in shots:
                zf.write(shot["file"], f"{folder_name}/{shot['file'].name}")
            zf.write(otio_path, f"{folder_name}/timeline.otio")
            zf.write(readme_path, f"{folder_name}/README.txt")
        size_mb = out_zip.stat().st_size / (1024 * 1024)
        print(f"      zipped -> {out_zip} ({size_mb:.1f} MB)")
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
        help="Output zip path when --zip is set (default: <edit_dir>/resolve_package[_vertical].zip)",
    )
    ap.add_argument(
        "--vertical", action="store_true",
        help="Bake 9:16 crop (x_crop per range) into each clip for vertical delivery",
    )
    ap.add_argument(
        "--split-shots", action="store_true",
        help="Split each range at detected shot boundaries (reads edit/shots/ cache)",
    )
    ap.add_argument(
        "--audio-streams", type=int, default=None, metavar="N",
        help="Keep N source audio streams as separate tracks per clip "
             "(default: read 'audio_streams' from the EDL, else 1). "
             "Use 2 for broadcast MXF with VO on 0:a:0 and NAT/SOT on 0:a:1.",
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
        vertical=args.vertical,
        split_shots=args.split_shots,
        audio_streams=args.audio_streams,
    )


if __name__ == "__main__":
    main()
