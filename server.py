#!/usr/bin/env python3
"""video-use web server — chat-driven video editor in the browser.

Usage:
    python server.py --videos-dir /path/to/footage [--port 8765]
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Load .env from the project directory before anything else
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

import aiofiles
import anthropic
import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"

SKILL_MD: str = ""
VIDEOS_DIR: Path = Path(".")
EDIT_DIR: Path = Path("edit")

app = FastAPI()
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Startup ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def _load_skill_md():
    global SKILL_MD
    p = HERE / "SKILL.md"
    if p.exists():
        async with aiofiles.open(p, encoding="utf-8") as f:
            SKILL_MD = await f.read()


# ── SPA ────────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    async with aiofiles.open(STATIC_DIR / "app.html", encoding="utf-8") as f:
        return HTMLResponse(await f.read())


# ── Video file serving with HTTP Range support ─────────────────────────────────

@app.get("/video")
async def serve_video(path: str, request: Request):
    """Stream any video under VIDEOS_DIR with 206 Partial Content for seek."""
    p = Path(path) if Path(path).is_absolute() else VIDEOS_DIR / path
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        p.relative_to(VIDEOS_DIR)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    file_size = p.stat().st_size
    range_header = request.headers.get("range")

    if range_header:
        raw = range_header.replace("bytes=", "")
        parts = raw.split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1
        end = min(end, file_size - 1)
        length = end - start + 1

        async def _range():
            async with aiofiles.open(p, "rb") as f:
                await f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = await f.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Type": "video/mp4",
        }
        return StreamingResponse(_range(), status_code=206, headers=headers)

    async def _full():
        async with aiofiles.open(p, "rb") as f:
            while True:
                chunk = await f.read(65536)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        _full(),
        headers={"Content-Length": str(file_size), "Accept-Ranges": "bytes"},
        media_type="video/mp4",
    )


# ── Edit-dir file serving ──────────────────────────────────────────────────────

@app.get("/edit/{file_path:path}")
async def serve_edit_file(file_path: str, request: Request):
    p = EDIT_DIR / file_path
    if not p.exists():
        raise HTTPException(status_code=404)
    suffix = p.suffix.lower()
    ct = {
        ".png": "image/png",
        ".srt": "text/plain",
        ".json": "application/json",
        ".md": "text/plain",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".zip": "application/zip",
    }.get(suffix, "application/octet-stream")

    # MP4 inside edit/ also gets range support
    if suffix == ".mp4":
        return await serve_video(str(p), request)

    async def _stream():
        async with aiofiles.open(p, "rb") as f:
            while True:
                chunk = await f.read(65536)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(_stream(), media_type=ct)


# ── API: set videos directory ─────────────────────────────────────────────────

@app.post("/api/set-videos-dir")
async def set_videos_dir(request: Request):
    global VIDEOS_DIR, EDIT_DIR
    body = await request.json()
    path_str = body.get("path", "").strip()
    if not path_str:
        raise HTTPException(status_code=400, detail="path is required")
    p = Path(path_str).resolve()
    if not p.exists() or not p.is_dir():
        raise HTTPException(status_code=400, detail=f"Directory not found: {p}")
    VIDEOS_DIR = p
    EDIT_DIR = p / "edit"
    EDIT_DIR.mkdir(parents=True, exist_ok=True)
    return JSONResponse({"videos_dir": str(VIDEOS_DIR), "edit_dir": str(EDIT_DIR)})


# ── API: project state ─────────────────────────────────────────────────────────

@app.get("/api/project")
async def get_project():
    result: dict = {
        "videos_dir": str(VIDEOS_DIR),
        "edit_dir": str(EDIT_DIR),
        "edl": None,
        "project_md": None,
        "takes_packed": None,
        "preview_available": (EDIT_DIR / "preview.mp4").exists(),
        "final_available": (EDIT_DIR / "final.mp4").exists(),
        "sources": [],
    }
    for attr, fname in (("edl", "edl.json"), ("project_md", "project.md"), ("takes_packed", "takes_packed.md")):
        fp = EDIT_DIR / fname
        if fp.exists():
            async with aiofiles.open(fp, encoding="utf-8") as f:
                raw = await f.read()
            result[attr] = json.loads(raw) if fname.endswith(".json") else raw

    video_exts = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".MP4", ".MOV", ".MKV"}
    for f in sorted(VIDEOS_DIR.iterdir()):
        if f.suffix in video_exts and f.is_file():
            result["sources"].append({"name": f.name, "path": str(f)})

    return JSONResponse(result)


# ── API: render (SSE) ──────────────────────────────────────────────────────────

@app.post("/api/render")
async def api_render(request: Request):
    body = await request.json()
    mode = body.get("mode", "preview")
    edl_path = EDIT_DIR / "edl.json"
    if not edl_path.exists():
        raise HTTPException(status_code=400, detail="edl.json not found — ask the editor to create a cut first")
    out_name = "preview.mp4" if mode == "preview" else "final.mp4"
    out_path = EDIT_DIR / out_name
    cmd = [sys.executable, str(HERE / "helpers" / "render.py"), str(edl_path), "-o", str(out_path)]
    if mode == "preview":
        cmd.append("--preview")

    async def _sse():
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        async for line in proc.stdout:
            text = line.decode(errors="replace").rstrip()
            yield f"data: {json.dumps({'line': text})}\n\n"
        await proc.wait()
        status = "done" if proc.returncode == 0 else "error"
        yield f"data: {json.dumps({'status': status, 'path': f'edit/{out_name}', 'mode': mode})}\n\n"

    return StreamingResponse(_sse(), media_type="text/event-stream")


# ── API: export Resolve package ────────────────────────────────────────────────

@app.post("/api/export")
async def api_export():
    edl_path = EDIT_DIR / "edl.json"
    if not edl_path.exists():
        raise HTTPException(status_code=400, detail="edl.json not found")
    zip_path = EDIT_DIR / "resolve_package.zip"
    cmd = [
        sys.executable, str(HERE / "helpers" / "export_resolve.py"),
        str(edl_path), "--zip", "-o", str(zip_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=stdout.decode(errors="replace"))
    if not zip_path.exists():
        raise HTTPException(status_code=500, detail="Export produced no zip file")
    return JSONResponse({"path": "edit/resolve_package.zip", "size": zip_path.stat().st_size})


# ── LLM tool definitions ───────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "write_edl",
        "description": (
            "Write the EDL (Edit Decision List) to disk at edit/edl.json. "
            "Call this once you have decided on all cuts and subtitles. "
            "Follows the edl.json schema from SKILL.md."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "edl": {"type": "object", "description": "Complete EDL object per the edl.json schema"}
            },
            "required": ["edl"],
        },
    },
    {
        "name": "render_preview",
        "description": "Render a fast preview MP4 from the current edl.json. Use after write_edl for quick iteration.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "render_final",
        "description": "Render the final high-quality MP4 from the current edl.json. Use when the user approves the preview.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_transcribe",
        "description": "Transcribe all video files in the project directory then pack into takes_packed.md. Run this first when no transcript exists.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "timeline_view",
        "description": "Generate a filmstrip+waveform PNG for a time range. Use at decision points (ambiguous cuts, take comparisons, self-eval).",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "Absolute path to source video file"},
                "start": {"type": "number", "description": "Start time in seconds"},
                "end": {"type": "number", "description": "End time in seconds"},
            },
            "required": ["source_path", "start", "end"],
        },
    },
    {
        "name": "read_transcript",
        "description": "Return the full contents of takes_packed.md. Use this to read the transcript before making cut decisions.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


async def _run_tool(name: str, tool_input: dict, ws: WebSocket) -> str:
    if name == "read_transcript":
        packed = EDIT_DIR / "takes_packed.md"
        if packed.exists():
            return packed.read_text()
        return "No transcript found. Call run_transcribe first."
    if name == "write_edl":
        edl = tool_input["edl"]
        # Always recompute — don't trust the LLM's arithmetic
        edl["total_duration_s"] = round(
            sum(float(r["end"]) - float(r["start"]) for r in edl.get("ranges", [])), 3
        )
        EDIT_DIR.mkdir(parents=True, exist_ok=True)
        edl_path = EDIT_DIR / "edl.json"
        async with aiofiles.open(edl_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(edl, indent=2))
        n = len(edl.get("ranges", []))
        return (
            f"EDL written ({n} segment{'s' if n != 1 else ''}, "
            f"total duration {edl['total_duration_s']}s). "
            "Call render_preview to see the cut."
        )

    if name in ("render_preview", "render_final"):
        edl_path = EDIT_DIR / "edl.json"
        if not edl_path.exists():
            return "Error: edl.json not found. Call write_edl first."
        mode = "preview" if name == "render_preview" else "final"
        out_name = "preview.mp4" if mode == "preview" else "final.mp4"
        out_path = EDIT_DIR / out_name
        cmd = [sys.executable, str(HERE / "helpers" / "render.py"), str(edl_path), "-o", str(out_path)]
        if mode == "preview":
            cmd.append("--preview")

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        lines: list[str] = []
        async for line in proc.stdout:
            text = line.decode(errors="replace").rstrip()
            lines.append(text)
            await ws.send_json({"type": "tool_progress", "tool": name, "line": text})
        await proc.wait()

        if proc.returncode == 0:
            await ws.send_json({"type": "render_done", "path": f"edit/{out_name}", "mode": mode})
            return f"{mode.title()} render complete: {out_path}"
        return f"Render failed (exit {proc.returncode}):\n" + "\n".join(lines[-20:])

    if name == "run_transcribe":
        cmd_t = [
            sys.executable, str(HERE / "helpers" / "transcribe_batch.py"),
            str(VIDEOS_DIR), "--edit-dir", str(EDIT_DIR),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd_t, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        lines = []
        async for line in proc.stdout:
            text = line.decode(errors="replace").rstrip()
            lines.append(text)
            await ws.send_json({"type": "tool_progress", "tool": name, "line": text})
        await proc.wait()
        if proc.returncode != 0:
            return "Transcription failed:\n" + "\n".join(lines[-10:])

        cmd_p = [
            sys.executable, str(HERE / "helpers" / "pack_transcripts.py"),
            "--edit-dir", str(EDIT_DIR),
        ]
        proc2 = await asyncio.create_subprocess_exec(
            *cmd_p, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        await proc2.communicate()

        packed = EDIT_DIR / "takes_packed.md"
        if packed.exists():
            content = packed.read_text()
            await ws.send_json({"type": "transcript_ready"})
            return f"Transcription complete. takes_packed.md:\n\n{content}"
        return "Transcription ran but takes_packed.md was not created."

    if name == "timeline_view":
        source = tool_input["source_path"]
        start = float(tool_input["start"])
        end = float(tool_input["end"])
        stem = Path(source).stem
        out_name = f"timeline_{stem}_{start:.2f}-{end:.2f}.png"
        verify_dir = EDIT_DIR / "verify"
        verify_dir.mkdir(parents=True, exist_ok=True)
        out_path = verify_dir / out_name
        transcript = EDIT_DIR / "transcripts" / f"{stem}.json"
        cmd = [
            sys.executable, str(HERE / "helpers" / "timeline_view.py"),
            source, str(start), str(end), "-o", str(out_path),
        ]
        if transcript.exists():
            cmd += ["--transcript", str(transcript)]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        await proc.communicate()
        if out_path.exists():
            url = f"/edit/verify/{out_name}"
            await ws.send_json({
                "type": "timeline_png",
                "url": url,
                "source": Path(source).name,
                "start": start,
                "end": end,
            })
            return f"Timeline view ready: {url}"
        return "timeline_view failed to produce output."

    return f"Unknown tool: {name}"


# ── System prompt builder ──────────────────────────────────────────────────────

def _build_system() -> list[dict]:
    """Return system as a list of blocks with prompt-cache markers."""
    base = (
        "You are a professional video editor operating the video-use editing skill inside a web UI.\n\n"
        "## Important: web context differences from the CLI skill\n"
        "- You do NOT have Bash, Read, Write, or any filesystem tools. Do not ask for them.\n"
        "- The transcript (takes_packed.md) is injected directly into this system prompt below — "
        "you already have it. You do NOT need to read any files to access it.\n"
        "- If for any reason you need to re-read the transcript, use the `read_transcript` tool.\n"
        "- Your only tools are: write_edl, render_preview, render_final, run_transcribe, "
        "timeline_view, read_transcript.\n"
        "- Always confirm the editing strategy in plain English before calling write_edl.\n\n"
        f"Videos directory: {VIDEOS_DIR}\nEdit directory: {EDIT_DIR}\n\n"
    )
    blocks = [
        {"type": "text", "text": base + SKILL_MD, "cache_control": {"type": "ephemeral"}},
    ]

    packed = EDIT_DIR / "takes_packed.md"
    if packed.exists():
        blocks.append({
            "type": "text",
            "text": "# Current Transcript (takes_packed.md)\n\n" + packed.read_text(),
            "cache_control": {"type": "ephemeral"},
        })
    else:
        blocks.append({
            "type": "text",
            "text": (
                "No transcript yet. Use run_transcribe to transcribe and pack the source videos. "
                f"Source videos in {VIDEOS_DIR}: "
                + ", ".join(f.name for f in sorted(VIDEOS_DIR.iterdir()) if f.suffix.lower() in {".mp4", ".mov", ".mkv", ".avi", ".m4v"})
            ),
        })

    dynamic_parts: list[str] = []
    edl_path = EDIT_DIR / "edl.json"
    if edl_path.exists():
        dynamic_parts.append("# Current EDL\n\n```json\n" + edl_path.read_text() + "\n```")
    project_path = EDIT_DIR / "project.md"
    if project_path.exists():
        dynamic_parts.append("# Session Notes\n\n" + project_path.read_text())
    if dynamic_parts:
        blocks.append({"type": "text", "text": "\n\n".join(dynamic_parts)})

    return blocks


# ── WebSocket chat handler ─────────────────────────────────────────────────────

@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    client = anthropic.AsyncAnthropic()
    messages: list[dict] = []

    try:
        while True:
            data = await ws.receive_json()
            user_text = data.get("message", "").strip()
            if not user_text:
                continue

            messages.append({"role": "user", "content": user_text})
            system = _build_system()

            # Agentic tool-use loop
            while True:
                tool_uses: list = []

                async with client.messages.stream(
                    model="claude-opus-4-7",
                    max_tokens=8192,
                    system=system,
                    messages=messages,
                    tools=TOOLS,
                ) as stream:
                    async for event in stream:
                        if event.type == "content_block_delta":
                            delta = event.delta
                            if hasattr(delta, "text"):
                                await ws.send_json({"type": "token", "text": delta.text})
                    final = await stream.get_final_message()

                for block in final.content:
                    if block.type == "tool_use":
                        tool_uses.append(block)

                messages.append({"role": "assistant", "content": final.content})

                if final.stop_reason != "tool_use" or not tool_uses:
                    await ws.send_json({"type": "done"})
                    break

                tool_results: list[dict] = []
                for block in tool_uses:
                    await ws.send_json({"type": "tool_start", "tool": block.name, "input": block.input})
                    result = await _run_tool(block.name, block.input, ws)
                    await ws.send_json({"type": "tool_done", "tool": block.name})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

                messages.append({"role": "user", "content": tool_results})
                system = _build_system()  # refresh with any updated files

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    global VIDEOS_DIR, EDIT_DIR

    parser = argparse.ArgumentParser(description="video-use web server")
    parser.add_argument("--videos-dir", "-d", default=".", help="Directory containing source video files")
    parser.add_argument("--port", "-p", type=int, default=8765, help="Port (default: 8765)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    args = parser.parse_args()

    VIDEOS_DIR = Path(args.videos_dir).resolve()
    EDIT_DIR = VIDEOS_DIR / "edit"
    EDIT_DIR.mkdir(parents=True, exist_ok=True)

    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("WARNING: ANTHROPIC_API_KEY not set — chat will fail", file=sys.stderr)

    print(f"video-use server")
    print(f"  videos : {VIDEOS_DIR}")
    print(f"  edit   : {EDIT_DIR}")
    print(f"  open   : http://localhost:{args.port}")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
