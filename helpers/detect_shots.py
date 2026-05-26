"""Detect shot boundaries within a video using ffmpeg's scdet filter.

Results are cached in edit/shots/<source_stem>.json so subsequent calls are instant.

Usage:
    python helpers/detect_shots.py <source> [--threshold 10] [--edit-dir edit/]
    python helpers/detect_shots.py <source> --force   # ignore cache
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_THRESHOLD = 5.0    # scdet score 0–100; ~5 catches broadcast hard-cuts reliably

# ffmpeg scdet logs cut frames like:
# [scdet @ ptr] frame:73 pts:73 pts_time:2.43333 lavfi.scd.score: 14.47 lavfi.scd.time: 2.43333
# Note the space after the colon — \s* handles both formats.
_CUT_RE = re.compile(r"pts_time:\s*([\d.]+).*?lavfi\.scd\.score:\s*([\d.]+)")


def detect_shots(
    source: Path,
    edit_dir: Path,
    threshold: float = DEFAULT_THRESHOLD,
    force: bool = False,
) -> list[dict]:
    """Return list of {"time": float, "score": float} cut points for source.

    Uses ffmpeg scdet filter. Results cached in edit_dir/shots/<stem>.json.
    A cut is emitted at t=0 for the first shot (always).
    """
    cache_dir = edit_dir / "shots"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{source.stem}.json"

    if cache_path.exists() and not force:
        data = json.loads(cache_path.read_text())
        # Invalidate if threshold changed
        if abs(data.get("threshold", -1) - threshold) < 0.01:
            return data["cuts"]

    print(f"detecting shot boundaries in {source.name} (threshold={threshold})…", flush=True)

    result = subprocess.run(
        ["ffmpeg", "-i", str(source), "-vf", f"scdet=t={threshold}", "-an", "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    # scdet logs cut frames on stderr regardless of return code (null muxer = exit 0)

    cuts: list[dict] = [{"time": 0.0, "score": 100.0}]  # always include first shot
    for line in result.stderr.splitlines():
        m = _CUT_RE.search(line)
        if m:
            t = round(float(m.group(1)), 4)
            score = round(float(m.group(2)), 2)
            if t > 0.05:  # skip near-zero detections (codec artefacts at open)
                cuts.append({"time": t, "score": score})

    cuts.sort(key=lambda c: c["time"])

    cache_path.write_text(json.dumps({
        "source": str(source),
        "threshold": threshold,
        "cuts": cuts,
    }, indent=2))

    print(f"  found {len(cuts)} shot(s)", flush=True)
    return cuts


def main() -> None:
    ap = argparse.ArgumentParser(description="Detect shot boundaries in a video")
    ap.add_argument("source", type=Path)
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help=f"scdet score threshold 0–100 (default {DEFAULT_THRESHOLD})")
    ap.add_argument("--edit-dir", type=Path, required=True)
    ap.add_argument("--force", action="store_true", help="Re-run even if cached")
    args = ap.parse_args()

    source = args.source.resolve()
    if not source.exists():
        sys.exit(f"source not found: {source}")

    cuts = detect_shots(source, args.edit_dir.resolve(), args.threshold, args.force)
    print(json.dumps(cuts, indent=2))


if __name__ == "__main__":
    main()
