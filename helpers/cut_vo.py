"""Cut a voiceover recording to match an approved script.

Aligns the script against the word-level ElevenLabs transcript using
difflib.SequenceMatcher, extracts the matching audio segments with 30ms
crossfade joins, and writes vo_clean.wav (the spliced VO audio only —
no silence padding).  Silence at grab/PTC positions is applied at render
time by helpers/render.py via an ffmpeg volume envelope, so the WAV
length simply equals the sum of kept segment durations.

Usage:
    python helpers/cut_vo.py <source_wav> --script "approved text" --edit-dir edit/
    python helpers/cut_vo.py <source_wav> --script-file approved.txt --edit-dir edit/
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

CROSSFADE_S = 0.030  # 30ms crossfade at each splice (Rule 3 equivalent)
PAD_S = 0.030        # 30ms padding each side of a kept segment
MERGE_GAP_S = 0.060  # gaps smaller than this between kept words → same segment
MIN_SEG_S = 0.080    # minimum segment duration (must be > 2 * CROSSFADE_S)
MATCH_WARN = 0.70    # warn if coverage drops below 70%
SAMPLE_RATE = 48000


# ── Transcript loading ─────────────────────────────────────────────────────────

def load_words(transcript_path: Path) -> list[dict]:
    """Return type='word' entries from the Scribe transcript JSON."""
    data = json.loads(transcript_path.read_text())
    return [w for w in data.get("words", []) if w.get("type") == "word"]


# ── Script alignment ───────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    return re.sub(r"[^\w]", "", text.lower())


def align_script(transcript_words: list[dict], script: str) -> tuple[list[dict], float]:
    """Align script words against transcript words via SequenceMatcher.

    Returns (kept_words, coverage) where coverage is fraction of script words matched.
    autojunk=False is critical — short words like "a", "the", "I" must match.
    """
    script_tokens = script.split()
    norm_script = [_norm(t) for t in script_tokens]
    norm_tx = [_norm(w["text"]) for w in transcript_words]

    matcher = difflib.SequenceMatcher(None, norm_script, norm_tx, autojunk=False)

    kept_indices: set[int] = set()
    matched_script = 0
    for block in matcher.get_matching_blocks():
        a_start, b_start, size = block
        if size == 0:
            continue
        matched_script += size
        for offset in range(size):
            kept_indices.add(b_start + offset)

    kept_words = [transcript_words[i] for i in sorted(kept_indices)]
    coverage = matched_script / max(1, len(norm_script))
    return kept_words, coverage


# ── Segment grouping ───────────────────────────────────────────────────────────

def build_segments(kept_words: list[dict]) -> list[dict]:
    """Collapse kept_words into contiguous audio segments with padding.

    Words closer than MERGE_GAP_S are merged into one segment.
    Each segment gets PAD_S added to both edges (clamped to 0).
    Minimum segment duration is MIN_SEG_S (avoids acrossfade shorter than d).
    Returns list of {"start": float, "end": float} in source time.
    """
    if not kept_words:
        return []

    segs: list[dict] = []
    seg_start = kept_words[0]["start"]
    seg_end = kept_words[0]["end"]

    for w in kept_words[1:]:
        if w["start"] - seg_end < MERGE_GAP_S:
            seg_end = w["end"]
        else:
            segs.append({"start": seg_start, "end": seg_end})
            seg_start = w["start"]
            seg_end = w["end"]
    segs.append({"start": seg_start, "end": seg_end})

    # Apply padding
    padded = []
    for seg in segs:
        s = max(0.0, seg["start"] - PAD_S)
        e = seg["end"] + PAD_S
        dur = e - s
        if dur < MIN_SEG_S:
            e = s + MIN_SEG_S
        padded.append({"start": s, "end": e})

    return padded


# ── FFmpeg audio splicing ──────────────────────────────────────────────────────

def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed:\n{result.stderr.decode(errors='replace')[-2000:]}"
        )


def splice_segments(source: Path, segments: list[dict], out_wav: Path) -> None:
    """Extract segments from source and join them with 30ms acrossfades.

    For N=1: simple extract.
    For N>1: extract each to a temp WAV, then chain with acrossfade filter.
    Output: 48kHz mono PCM WAV.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        seg_paths: list[Path] = []

        for i, seg in enumerate(segments):
            out = tmp_dir / f"seg_{i:03d}.wav"
            dur = seg["end"] - seg["start"]
            _run([
                "ffmpeg", "-y",
                "-ss", f"{seg['start']:.3f}",
                "-i", str(source),
                "-t", f"{dur:.3f}",
                "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
                str(out),
            ])
            seg_paths.append(out)

        if len(seg_paths) == 1:
            _run([
                "ffmpeg", "-y", "-i", str(seg_paths[0]),
                "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
                str(out_wav),
            ])
            return

        # Chain pairwise acrossfades
        inputs: list[str] = []
        for p in seg_paths:
            inputs += ["-i", str(p)]

        n = len(seg_paths)
        filter_parts: list[str] = []
        prev = "[0]"
        for i in range(1, n):
            nxt = f"[cf{i}]" if i < n - 1 else "[out]"
            filter_parts.append(
                f"{prev}[{i}]acrossfade=d={CROSSFADE_S:.3f}:c1=tri:c2=tri{nxt}"
            )
            prev = nxt

        _run([
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex", ";".join(filter_parts),
            "-map", "[out]",
            "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
            str(out_wav),
        ])


# ── Output timecode adjustment ─────────────────────────────────────────────────

def compute_output_words(kept_words: list[dict], segments: list[dict]) -> list[dict]:
    """Return kept_words with start/end rewritten to spliced output-timeline positions.

    Accounts for the crossfade overlap: each join shortens the cumulative offset by
    CROSSFADE_S. Words are assigned to whichever segment contains their source start.
    """
    output_words: list[dict] = []
    cursor = 0.0

    # Build a mapping: segment_index → output_start
    seg_output_starts: list[float] = []
    for i, seg in enumerate(segments):
        seg_output_starts.append(cursor)
        dur = seg["end"] - seg["start"]
        if i < len(segments) - 1:
            cursor += dur - CROSSFADE_S
        else:
            cursor += dur

    for w in kept_words:
        # Find which segment this word belongs to
        assigned = -1
        for i, seg in enumerate(segments):
            if seg["start"] <= w["start"] <= seg["end"]:
                assigned = i
                break
        if assigned == -1:
            continue
        seg = segments[assigned]
        offset = seg_output_starts[assigned] - seg["start"]
        out_w = dict(w)
        out_w["start"] = round(w["start"] + offset, 4)
        out_w["end"] = round(w["end"] + offset, 4)
        output_words.append(out_w)

    return output_words


# ── Silence insertion for grab/PTC ranges ──────────────────────────────────────

def _is_sync_range(r: dict) -> bool:
    """Return True if this range should use camera audio (not VO)."""
    if r.get("audio_mode") == "sync":
        return True
    beat = (r.get("beat") or "").upper()
    return any(kw in beat for kw in ("GRAB", "PTC", "SYNC", "INTERVIEW", "SOT"))


def insert_sync_silence(spliced_wav: Path, edl: dict, out_wav: Path) -> None:
    """Build final vo_clean.wav by placing spliced VO at B-roll positions
    and inserting silence at grab/PTC positions.

    The output duration matches edl["total_duration_s"] exactly.
    Uses ffmpeg aevalsrc=0 for silence, then concat demuxer to assemble.
    """
    ranges = edl.get("ranges", [])
    if not ranges:
        # No EDL structure — just copy the spliced file as-is
        _run(["ffmpeg", "-y", "-i", str(spliced_wav),
              "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
              str(out_wav)])
        return

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        parts: list[Path] = []
        vo_consumed = 0.0  # how many seconds we've consumed from spliced_wav

        for i, r in enumerate(ranges):
            dur = float(r["end"]) - float(r["start"])
            part_path = tmp_dir / f"part_{i:03d}.wav"

            if _is_sync_range(r):
                # Silence for grab/PTC
                _run([
                    "ffmpeg", "-y",
                    "-f", "lavfi",
                    "-i", f"aevalsrc=0:channel_layout=mono:sample_rate={SAMPLE_RATE}:duration={dur:.3f}",
                    "-c:a", "pcm_s16le",
                    str(part_path),
                ])
            else:
                # Chunk of VO audio
                _run([
                    "ffmpeg", "-y",
                    "-ss", f"{vo_consumed:.3f}",
                    "-i", str(spliced_wav),
                    "-t", f"{dur:.3f}",
                    "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
                    str(part_path),
                ])
                vo_consumed += dur

            parts.append(part_path)

        if len(parts) == 1:
            _run(["ffmpeg", "-y", "-i", str(parts[0]),
                  "-c:a", "pcm_s16le", str(out_wav)])
            return

        # Concat all parts
        concat_list = tmp_dir / "concat.txt"
        concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
        _run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
            str(out_wav),
        ])


# ── Top-level entry point ──────────────────────────────────────────────────────

def cut_vo(
    source_path: Path,
    script: str,
    edit_dir: Path,
    edl: dict | None = None,  # kept for CLI compat; silence is now applied in render.py
    out_wav: Path | None = None,
    out_words: Path | None = None,
) -> dict:
    """Splice source_path to match script. Returns a result dict."""
    import shutil
    edit_dir.mkdir(parents=True, exist_ok=True)
    out_wav = out_wav or edit_dir / "vo_clean.wav"
    out_words = out_words or edit_dir / "vo_words.json"

    stem = source_path.stem
    transcript_path = edit_dir / "transcripts" / f"{stem}.json"
    if not transcript_path.exists():
        return {
            "status": "error",
            "message": f"Transcript not found: {transcript_path}. Run transcription first.",
        }

    transcript_words = load_words(transcript_path)
    if not transcript_words:
        return {"status": "error", "message": "Transcript has no word entries."}

    kept_words, coverage = align_script(transcript_words, script)
    print(f"alignment coverage: {coverage:.1%} ({len(kept_words)} words kept)")

    if coverage < MATCH_WARN:
        print(
            f"WARNING: low coverage ({coverage:.1%}) — only {coverage:.0%} of script words "
            "found in transcript. Check for paraphrasing or missing audio.",
            file=sys.stderr,
        )

    if not kept_words:
        return {
            "status": "error",
            "message": "No script words matched the transcript. Cannot produce output.",
            "coverage": round(coverage, 4),
        }

    segments = build_segments(kept_words)
    print(f"merged into {len(segments)} audio segment(s)")

    with tempfile.TemporaryDirectory() as tmp:
        spliced_wav = Path(tmp) / "spliced.wav"
        print("splicing audio segments…")
        splice_segments(source_path, segments, spliced_wav)
        # Silence at grab/PTC positions is handled in render.py via volume envelope.
        shutil.copy2(spliced_wav, out_wav)

    output_words = compute_output_words(kept_words, segments)
    out_words.write_text(json.dumps(output_words, indent=2))

    size_mb = out_wav.stat().st_size / (1024 * 1024)
    print(f"wrote {out_wav.name} ({size_mb:.1f} MB, {len(segments)} segment(s))")
    print(f"wrote {out_words.name} ({len(output_words)} words)")

    status = "warn" if coverage < MATCH_WARN else "ok"
    return {
        "status": status,
        "message": (
            f"VO spliced: {len(kept_words)} words kept across {len(segments)} segment(s). "
            f"Coverage: {coverage:.1%}."
            + (f" WARNING: low coverage — check for mismatches." if status == "warn" else "")
        ),
        "coverage": round(coverage, 4),
        "segments_kept": len(segments),
        "output_wav": str(out_wav),
        "output_words": str(out_words),
    }


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Cut VO to match an approved script")
    ap.add_argument("source_path", type=Path, help="Source WAV/audio file")
    ap.add_argument("--script", type=str, default=None, help="Approved script text (inline)")
    ap.add_argument("--script-file", type=Path, default=None, help="Path to approved script text file")
    ap.add_argument("--edit-dir", type=Path, required=True, help="Edit directory (contains transcripts/)")
    ap.add_argument("--edl", type=Path, default=None, help="EDL JSON for grab/PTC silence insertion")
    ap.add_argument("--out-wav", type=Path, default=None)
    ap.add_argument("--out-words", type=Path, default=None)
    args = ap.parse_args()

    if args.script:
        script = args.script
    elif args.script_file:
        script = args.script_file.read_text()
    else:
        ap.error("Provide --script or --script-file")

    edl: dict | None = None
    if args.edl and args.edl.exists():
        edl = json.loads(args.edl.read_text())

    source = args.source_path.resolve()
    if not source.exists():
        sys.exit(f"source not found: {source}")

    result = cut_vo(
        source_path=source,
        script=script,
        edit_dir=args.edit_dir.resolve(),
        edl=edl,
        out_wav=args.out_wav,
        out_words=args.out_words,
    )

    print(f"RESULT:{json.dumps(result)}")
    if result["status"] == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
