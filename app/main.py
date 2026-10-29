import asyncio
import json
import math
import os
import re
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .proxies import THREADS, ProxyManager, choose_pipeline, pipeline_args


@asynccontextmanager
async def lifespan(app):
    proxies.reaper = asyncio.create_task(proxies.reap())
    yield
    await proxies.close()
    if render_task and not render_task.done():
        render_task.cancel()
        await asyncio.gather(render_task, return_exceptions=True)


app = FastAPI(title="NeoClip Web", lifespan=lifespan)
APP_BUILD = "1.1.1"
if origins := os.getenv("NEOCLIP_CORS_ORIGINS"):
    # Optional for a separate private preview frontend; disabled in Compose.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins.split(","),
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )


# MEDIA_ROOT / EXPORT_ROOT are only the first-run defaults. Once the user
# picks folders in Settings, the saved choice in DATA_ROOT wins.
DEFAULT_MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", "/media")).resolve()
DEFAULT_EXPORT_ROOT = Path(os.getenv("EXPORT_ROOT", "/exports")).resolve()
DATA_ROOT = Path(os.getenv("DATA_ROOT", "/data")).resolve()
WEB_ROOT = Path(__file__).parent / "web"
QUEUE_FILE = DATA_ROOT / "neoclip_queue.json"
SETTINGS_FILE = DATA_ROOT / "neoclip_settings.json"
PROXY_ROOT = DATA_ROOT / "proxies"
PROXY_HEIGHT = 360

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm"}

DATA_ROOT.mkdir(parents=True, exist_ok=True)
PROXY_ROOT.mkdir(parents=True, exist_ok=True)


def browse_roots() -> list[Path]:
    """Top-level folders the Settings folder picker may show.

    Defaults to the classic /media and /exports mounts plus /host, a spot for
    mounting a whole drive so users can choose any subfolder from the GUI.
    Override with NEOCLIP_BROWSE_ROOTS (comma-separated container paths).
    """
    raw = os.getenv("NEOCLIP_BROWSE_ROOTS", "/host,/media,/exports")
    roots: list[Path] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        root = Path(item).resolve()
        if root == Path("/") or root == DATA_ROOT or root in roots:
            continue
        if root.is_dir():
            roots.append(root)
    return roots


def inside_browse_roots(path: Path) -> bool:
    for root in browse_roots():
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def folder_writable(path: Path) -> bool:
    try:
        with tempfile.NamedTemporaryFile(dir=path, prefix=".neoclip-write-test-"):
            return True
    except OSError:
        return False


def folder_status(path: Optional[Path], need_write: bool) -> dict:
    if path is None:
        return {"path": "", "ok": False, "message": "Not set"}
    if not path.is_dir():
        return {"path": str(path), "ok": False, "message": "Folder not found"}
    if need_write and not folder_writable(path):
        return {"path": str(path), "ok": False, "message": "Folder is read-only"}
    return {"path": str(path), "ok": True, "message": "Ready"}


def load_settings() -> tuple[Optional[Path], Optional[Path]]:
    input_dir: Optional[Path] = DEFAULT_MEDIA_ROOT if DEFAULT_MEDIA_ROOT.is_dir() else None
    output_dir: Optional[Path] = None
    if DEFAULT_EXPORT_ROOT.is_dir():
        output_dir = DEFAULT_EXPORT_ROOT
    else:
        try:
            # Keep 1.1 behaviour: the default export folder is created on demand.
            DEFAULT_EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
            output_dir = DEFAULT_EXPORT_ROOT
        except OSError:
            output_dir = None
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            if data.get("input_dir"):
                input_dir = Path(str(data["input_dir"])).resolve()
            if data.get("output_dir"):
                output_dir = Path(str(data["output_dir"])).resolve()
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return input_dir, output_dir


def save_settings() -> None:
    payload = {
        "version": 1,
        "input_dir": str(MEDIA_ROOT) if MEDIA_ROOT else "",
        "output_dir": str(EXPORT_ROOT) if EXPORT_ROOT else "",
    }
    temporary_file = SETTINGS_FILE.with_suffix(".tmp")
    temporary_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary_file.replace(SETTINGS_FILE)


# None means "not chosen yet"; the GUI still starts and asks for a folder.
MEDIA_ROOT, EXPORT_ROOT = load_settings()


class SourceRequest(BaseModel):
    path: str
    current_time: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)


class ClipRequest(BaseModel):
    name: str = ""
    start_s: float = Field(ge=0, allow_inf_nan=False)
    end_s: float = Field(gt=0, allow_inf_nan=False)


class SettingsRequest(BaseModel):
    input_dir: str = ""
    output_dir: str = ""


class NewFolderRequest(BaseModel):
    parent: str
    name: str


class RenderRequest(BaseModel):
    mode: str = "individual"
    prefix: str = "neoclip_output"


class EditorSessionRequest(BaseModel):
    path: str
    current_time: float = Field(default=0, ge=0, allow_inf_nan=False)
    mark_in: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    mark_out: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    clip_name: str = ""
    render_mode: str = "individual"
    prefix: str = "neoclip_output"


state = {
    "source_path": "",
    "queues": {},
    "editor_sessions": {},
}

render_state = {
    "status": "idle",
    "progress": 0,
    "message": "Idle",
    "current_file": "",
    "error": "",
}

render_task: Optional[asyncio.Task] = None
render_process: Optional[asyncio.subprocess.Process] = None


def encoder_label(encoder: str) -> str:
    return {
        "h264_nvenc": "NVIDIA NVENC",
        "h264_qsv": "Intel QuickSync",
        "h264_amf": "AMD AMF",
        "libx264": "CPU (libx264)",
    }.get(encoder, encoder)


def sanitize_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", value).strip().strip(".")
    return value or "clip"


def require_input_root() -> Path:
    if MEDIA_ROOT is None or not MEDIA_ROOT.is_dir():
        raise HTTPException(
            status_code=409,
            detail="Choose an input folder in Settings first",
        )
    return MEDIA_ROOT


def require_output_root() -> Path:
    if EXPORT_ROOT is None or not EXPORT_ROOT.is_dir():
        raise HTTPException(
            status_code=409,
            detail="Choose an output folder in Settings before rendering",
        )
    if not folder_writable(EXPORT_ROOT):
        raise HTTPException(
            status_code=409,
            detail="The output folder is read-only; choose another in Settings",
        )
    return EXPORT_ROOT


def resolve_media_path(relative_path: str) -> Path:
    require_input_root()
    candidate = (MEDIA_ROOT / relative_path).resolve()

    try:
        candidate.relative_to(MEDIA_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid media path") from exc

    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")

    return candidate


proxies = ProxyManager(PROXY_ROOT, resolve_media_path)
app.include_router(proxies.router())
app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(
        WEB_ROOT / "manifest.webmanifest", media_type="application/manifest+json"
    )


@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        WEB_ROOT / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


def active_clips() -> list[dict]:
    source_path = state["source_path"]
    if not source_path:
        return []

    queues = state["queues"]
    clips = queues.setdefault(source_path, [])
    return clips


def default_editor_session() -> dict:
    return {
        "current_time": 0.0,
        "mark_in": None,
        "mark_out": None,
        "clip_name": "",
        "render_mode": "individual",
        "prefix": "neoclip_output",
    }


def normalize_editor_session(value: object) -> dict:
    session = default_editor_session()
    if not isinstance(value, dict):
        return session

    for key in ("current_time", "mark_in", "mark_out"):
        raw_value = value.get(key)
        if raw_value is None and key != "current_time":
            session[key] = None
            continue

        try:
            parsed = float(raw_value or 0)
        except (TypeError, ValueError):
            parsed = 0.0

        session[key] = max(0.0, parsed) if math.isfinite(parsed) else 0.0

    session["clip_name"] = str(value.get("clip_name", ""))[:250]

    render_mode = str(value.get("render_mode", "individual"))
    session["render_mode"] = (
        render_mode if render_mode in {"individual", "reel", "both"} else "individual"
    )

    session["prefix"] = (
        str(value.get("prefix", "neoclip_output"))[:250] or "neoclip_output"
    )
    return session


def editor_session_for(source_path: str) -> dict:
    if not source_path:
        return default_editor_session()

    sessions = state["editor_sessions"]
    session = normalize_editor_session(sessions.get(source_path))
    sessions[source_path] = session
    return session


def state_response() -> dict:
    clip_counts = {
        source_path: len(clips)
        for source_path, clips in state["queues"].items()
        if isinstance(clips, list) and clips
    }

    return {
        "build": APP_BUILD,
        "source_path": state["source_path"],
        "clips": list(active_clips()),
        "clip_counts": clip_counts,
        "total_queued_clips": sum(clip_counts.values()),
        "current_session": dict(editor_session_for(state["source_path"])),
    }


# Queues use paths relative to the input folder, so each input folder keeps
# its own queue. Switching folders parks the current one here.
folder_states: dict[str, dict] = {}


def input_key() -> str:
    return str(MEDIA_ROOT) if MEDIA_ROOT else ""


def park_current_state() -> None:
    key = input_key()
    if key:
        folder_states[key] = {
            "source_path": state["source_path"],
            "queues": state["queues"],
            "editor_sessions": state["editor_sessions"],
        }


def activate_folder_state() -> None:
    saved = folder_states.get(input_key()) or {}
    state["source_path"] = str(saved.get("source_path", ""))
    queues = saved.get("queues")
    state["queues"] = (
        {str(k): v for k, v in queues.items() if isinstance(v, list)}
        if isinstance(queues, dict)
        else {}
    )
    sessions = saved.get("editor_sessions")
    state["editor_sessions"] = (
        {
            str(k): normalize_editor_session(v)
            for k, v in sessions.items()
            if isinstance(v, dict)
        }
        if isinstance(sessions, dict)
        else {}
    )


def save_queue() -> None:
    park_current_state()
    payload = {
        "version": 4,
        "input_dir": input_key(),
        "source_path": state["source_path"],
        "queues": state["queues"],
        "editor_sessions": state["editor_sessions"],
        "folders": folder_states,
    }

    temporary_file = QUEUE_FILE.with_suffix(".tmp")
    temporary_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary_file.replace(QUEUE_FILE)


def load_queue() -> None:
    if not QUEUE_FILE.exists():
        return

    try:
        data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return

        folders = data.get("folders")
        if isinstance(folders, dict) and folders:
            for key, value in folders.items():
                if isinstance(value, dict):
                    folder_states[str(key)] = value
            activate_folder_state()
            return

        # Version 3 and older files belong to whichever input folder was used
        # before Settings existed (the /media mount).
        source_path = str(data.get("source_path", ""))
        state["source_path"] = source_path

        queues = data.get("queues")
        if isinstance(queues, dict):
            state["queues"] = {
                str(path): clips
                for path, clips in queues.items()
                if isinstance(clips, list)
            }
        else:
            # Migrate the original single-video queue format automatically.
            clips = data.get("clips", [])
            if source_path and isinstance(clips, list):
                state["queues"] = {source_path: clips}

        sessions = data.get("editor_sessions")
        if isinstance(sessions, dict):
            state["editor_sessions"] = {
                str(path): normalize_editor_session(session)
                for path, session in sessions.items()
                if isinstance(session, dict)
            }

    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        # Keep the application usable if the queue file is damaged.
        state["source_path"] = ""
        state["queues"] = {}
        state["editor_sessions"] = {}


load_queue()


def list_videos() -> list[dict]:
    if MEDIA_ROOT is None or not MEDIA_ROOT.is_dir():
        return []

    results = []

    for path in MEDIA_ROOT.rglob("*"):
        if not path.is_file():
            continue

        if path.name.startswith("."):
            continue

        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue

        try:
            if path.stat().st_size < 1:
                continue
        except OSError:
            continue

        relative = path.relative_to(MEDIA_ROOT).as_posix()

        results.append(
            {
                "name": path.name,
                "path": relative,
                "original_url": f"/api/media?path={quote(relative)}",
            }
        )

    results.sort(key=lambda item: item["path"].lower())
    return results


async def run_ffmpeg(
    args: list[str],
    duration_s: float,
    progress_start: float,
    progress_span: float,
) -> None:
    global render_process

    render_process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    assert render_process.stdout is not None

    process = render_process
    recent = []
    try:
        while True:
            line_bytes = await process.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode(errors="replace").strip()
            recent.append(line)
            recent = recent[-35:]
            if line.startswith("out_time_us="):
                try:
                    elapsed = int(line.split("=", 1)[1]) / 1_000_000
                    fraction = min(elapsed / max(duration_s, 0.001), 1.0)
                    render_state["progress"] = min(
                        99, int(progress_start + fraction * progress_span)
                    )
                except ValueError:
                    pass
        if await process.wait() != 0:
            raise RuntimeError("FFmpeg failed: " + " | ".join(recent))
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        render_process = None


async def render_individual(
    source: Path,
    clips: list[dict],
    prefix: str,
    source_has_audio: bool,
    progress_start: float,
    progress_span: float,
    video_encoder: str,
    output_dir: Optional[Path] = None,
) -> list[Path]:
    output_dir = output_dir or require_output_root() / sanitize_filename(source.stem)
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = []

    total_duration = sum(float(c["end_s"]) - float(c["start_s"]) for c in clips)
    completed_duration = 0.0

    for index, clip in enumerate(clips, start=1):
        start_s = float(clip["start_s"])
        end_s = float(clip["end_s"])
        duration = end_s - start_s
        clip_name = sanitize_filename(str(clip.get("name") or f"clip_{index:03d}"))
        output_path = output_dir / f"{prefix}_{index:03d}_{clip_name}.mp4"

        render_state["message"] = f"Rendering clip {index}/{len(clips)}"
        render_state["current_file"] = output_path.name

        clip_start_progress = (
            progress_start
            + (completed_duration / max(total_duration, 0.001)) * progress_span
        )

        clip_progress_span = (duration / max(total_duration, 0.001)) * progress_span

        before, after = pipeline_args(video_encoder, height=None)
        args = [
            "-hide_banner",
            "-nostdin",
            "-y",
            "-filter_threads",
            str(THREADS),
            "-ss",
            str(start_s),
            *before,
            "-i",
            str(source),
            "-t",
            str(duration),
            "-map",
            "0:v:0",
            *after,
            "-r",
            "30",
        ]

        if source_has_audio:
            args += [
                "-map",
                "0:a:0",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
                "-ac",
                "2",
            ]
        else:
            args += ["-an"]

        args += [
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-nostats",
            str(output_path),
        ]

        await run_ffmpeg(
            args,
            duration,
            clip_start_progress,
            clip_progress_span,
        )

        completed_duration += duration
        outputs.append(output_path)
    return outputs


async def render_reel(
    source: Path,
    clips: list[dict],
    prefix: str,
    source_has_audio: bool,
    progress_start: float,
    progress_span: float,
    video_encoder: str,
) -> None:
    export_root = require_output_root()
    output_dir = export_root / sanitize_filename(source.stem)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{prefix}_reel.mp4"

    render_state["message"] = "Rendering highlight reel"
    render_state["current_file"] = output_path.name

    # Render sequentially instead of opening N decoders/filter graphs at once.
    # Joining matched H.264/AAC intermediates requires no second video encode.
    with tempfile.TemporaryDirectory(prefix="reel-", dir=export_root) as temp:
        pieces = await render_individual(
            source,
            clips,
            "part",
            source_has_audio,
            progress_start,
            progress_span * 0.95,
            video_encoder,
            Path(temp),
        )
        listing = Path(temp) / "concat.txt"
        listing.write_text(
            "".join("file '" + p.name.replace("'", "'\\''") + "'\n" for p in pieces)
        )
        args = [
            "-hide_banner",
            "-nostdin",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-map",
            "0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-nostats",
            str(output_path),
        ]
        total_duration = sum(float(c["end_s"]) - float(c["start_s"]) for c in clips)
        await run_ffmpeg(
            args,
            total_duration,
            progress_start + progress_span * 0.95,
            progress_span * 0.05,
        )


async def render_job(
    mode: str, prefix: str, rendered_source: str, clips: list[dict]
) -> None:
    try:
        render_state.update(
            status="rendering",
            progress=0,
            message="Preparing render",
            current_file="",
            error="",
            source_path=rendered_source,
        )

        source = resolve_media_path(rendered_source)
        prefix = sanitize_filename(prefix)
        source_has_audio = (await proxies.probe(source))[1]
        video_encoder, label, failures = await choose_pipeline(
            source, float(clips[0]["start_s"]), height=None
        )
        render_state.update(pipeline=label, fallback_reason=" | ".join(failures))
        render_state["message"] = f"Using {label}"

        async def run_with(encoder: str) -> None:
            if mode == "individual":
                await render_individual(
                    source, clips, prefix, source_has_audio, 0, 100, encoder
                )
            elif mode == "reel":
                await render_reel(
                    source, clips, prefix, source_has_audio, 0, 100, encoder
                )
            elif mode == "both":
                await render_individual(
                    source, clips, prefix, source_has_audio, 0, 50, encoder
                )
                await render_reel(
                    source, clips, prefix, source_has_audio, 50, 50, encoder
                )
            else:
                raise RuntimeError("Unknown render mode")

        try:
            await run_with(video_encoder)
        except Exception as render_exc:
            # If the GPU encoder fails on this source, retry once on the CPU
            # instead of failing the whole render.
            from .proxies import REQUIRE_GPU

            if video_encoder == "cpu" or REQUIRE_GPU:
                raise
            render_state["message"] = (
                f"{encoder_label(video_encoder)} failed; retrying with CPU (libx264)"
            )
            video_encoder = "cpu"
            render_state.update(
                pipeline="CPU fallback (limited threads)",
                fallback_reason=str(render_exc),
            )
            await run_with(video_encoder)

        render_state.update(
            status="done",
            progress=100,
            message="Render complete",
            current_file="",
            error="",
        )

        # Remove only the rendered snapshot, not newly queued clips or a
        # different video's queue selected while this job was running.
        remaining = state["queues"].get(rendered_source, [])
        for clip in clips:
            if clip in remaining:
                remaining.remove(clip)
        save_queue()

    except asyncio.CancelledError:
        render_state.update(
            status="cancelled",
            message="Render cancelled",
            current_file="",
        )
        raise

    except Exception as exc:  # noqa: BLE001 -- job boundary reports failures to the UI
        render_state.update(
            status="error",
            message="Render failed",
            current_file="",
            error=str(exc),
        )


@app.get("/", response_class=HTMLResponse)
async def home() -> HTMLResponse:
    return HTMLResponse(
        (WEB_ROOT / "index.html").read_text(), headers={"Cache-Control": "no-store"}
    )


@app.get("/api/files")
async def get_files() -> list[dict]:
    return await asyncio.to_thread(list_videos)


@app.get("/api/media")
async def get_media(path: str) -> FileResponse:
    media_path = resolve_media_path(path)

    return FileResponse(
        media_path,
        filename=media_path.name,
        content_disposition_type="inline",
    )


@app.post("/api/source")
async def select_source(request: SourceRequest) -> dict:
    resolve_media_path(request.path)

    state["source_path"] = request.path
    state["queues"].setdefault(request.path, [])
    session = editor_session_for(request.path)
    save_queue()
    start = (
        request.current_time
        if request.current_time is not None
        else session["current_time"]
    )
    proxy = await proxies.start(request.path, start)
    return {**state_response(), "proxy": proxy}


@app.post("/api/proxy/start")
async def start_proxy(request: SourceRequest) -> dict:
    return await proxies.start(request.path, request.current_time or 0)


@app.post("/api/media/delete")
async def delete_media(request: SourceRequest) -> dict:
    global render_task, render_process

    relative_path = request.path
    # Validates that the path is inside MEDIA_ROOT and still exists.
    source = resolve_media_path(relative_path)

    # If a render is running against this source, cancel it before deleting.
    if (
        render_state.get("source_path") == relative_path
        and render_task is not None
        and not render_task.done()
    ):
        if render_process is not None and render_process.returncode is None:
            render_process.kill()
        render_task.cancel()
        render_task = None
        render_process = None

    # Stop FFmpeg/proxy access before trying to remove the source file.
    await proxies.delete_path(relative_path)

    try:
        source.unlink()
    except PermissionError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "The video could not be deleted because the input folder is "
                "read-only or the file is locked. Mount it read-write (not "
                "':ro') in Docker, then restart the container."
            ),
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"The video could not be deleted from disk: {exc}",
        ) from exc

    # Never report success unless the file is actually gone.
    if source.exists():
        raise HTTPException(
            status_code=500,
            detail="Delete was attempted, but the video still exists on disk.",
        )

    # Only clear queue/state after physical deletion succeeds.
    state["queues"].pop(relative_path, None)
    state["editor_sessions"].pop(relative_path, None)
    if state.get("source_path") == relative_path:
        state["source_path"] = ""
    save_queue()

    return {"ok": True, "deleted": relative_path, "exists": False}


@app.get("/api/state")
async def get_state() -> dict:
    return state_response()


@app.post("/api/session")
async def save_editor_session(request: EditorSessionRequest) -> dict:
    resolve_media_path(request.path)

    if request.render_mode not in {"individual", "reel", "both"}:
        raise HTTPException(status_code=400, detail="Invalid render mode")

    session = normalize_editor_session(
        {
            "current_time": request.current_time,
            "mark_in": request.mark_in,
            "mark_out": request.mark_out,
            "clip_name": request.clip_name,
            "render_mode": request.render_mode,
            "prefix": request.prefix,
        }
    )
    state["editor_sessions"][request.path] = session
    save_queue()
    return {"saved": True, "session": dict(session)}


@app.post("/api/clips")
async def add_clip(request: ClipRequest) -> dict:
    if not state["source_path"]:
        raise HTTPException(status_code=400, detail="Select a video first")

    if request.end_s <= request.start_s:
        raise HTTPException(status_code=400, detail="OUT must be after IN")

    clips = active_clips()
    name = request.name.strip() or f"clip_{len(clips) + 1:03d}"

    clips.append(
        {
            "name": name,
            "start_s": request.start_s,
            "end_s": request.end_s,
        }
    )

    save_queue()
    return state_response()


@app.delete("/api/clips/{index}")
async def delete_clip(index: int) -> dict:
    clips = active_clips()

    if index < 0 or index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")

    del clips[index]

    if not clips and state["source_path"]:
        state["queues"].pop(state["source_path"], None)

    save_queue()
    return state_response()


@app.delete("/api/clips")
async def clear_clips() -> dict:
    if state["source_path"]:
        state["queues"].pop(state["source_path"], None)

    save_queue()
    return state_response()


@app.post("/api/render")
async def start_render(request: RenderRequest) -> dict:
    global render_task

    if render_task and not render_task.done():
        raise HTTPException(status_code=409, detail="A render is already running")

    require_output_root()

    if not state["source_path"]:
        raise HTTPException(status_code=400, detail="Select a source video first")

    if not active_clips():
        raise HTTPException(status_code=400, detail="Add at least one clip")

    if request.mode not in {"individual", "reel", "both"}:
        raise HTTPException(400, "Invalid render mode")
    render_task = asyncio.create_task(
        render_job(
            request.mode,
            request.prefix,
            state["source_path"],
            [dict(clip) for clip in active_clips()],
        )
    )

    return {"started": True}


@app.post("/api/render/cancel")
async def cancel_render() -> dict:
    if render_process and render_process.returncode is None:
        try:
            render_process.kill()
        except ProcessLookupError:
            pass

    if render_task and not render_task.done():
        render_task.cancel()
        await asyncio.gather(render_task, return_exceptions=True)

    return {"cancelled": True}


@app.get("/api/render/status")
async def get_render_status() -> dict:
    return render_state


@app.get("/api/status")
async def status() -> dict:
    return {
        "status": "online",
        "media_available": bool(MEDIA_ROOT and MEDIA_ROOT.is_dir()),
        "export_available": bool(EXPORT_ROOT and EXPORT_ROOT.is_dir()),
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
        "proxy_height": PROXY_HEIGHT,
        "proxy_directory": str(PROXY_ROOT),
        "build": APP_BUILD,
        "cpu_threads": THREADS,
        "active_proxy_processes": len(proxies.processes),
        "pip_note": "Browser/OS support required; open the app directly, not in an embedded frame",
    }


# ---------------------------------------------------------------- settings --


def settings_response() -> dict:
    input_status = folder_status(MEDIA_ROOT, need_write=False)
    output_status = folder_status(EXPORT_ROOT, need_write=True)
    return {
        "input": input_status,
        "output": output_status,
        "ready": input_status["ok"] and output_status["ok"],
        "roots": [str(root) for root in browse_roots()],
        "render_running": bool(render_task and not render_task.done()),
    }


def validate_folder(raw: str, label: str) -> Path:
    if not raw.strip():
        raise HTTPException(400, f"Choose an {label} folder")
    path = Path(raw.strip()).resolve()
    if not inside_browse_roots(path):
        allowed = ", ".join(str(r) for r in browse_roots()) or "(none mounted)"
        raise HTTPException(
            400, f"The {label} folder must be inside one of: {allowed}"
        )
    if not path.is_dir():
        raise HTTPException(404, f"The {label} folder does not exist")
    return path


@app.get("/api/settings")
async def get_settings() -> dict:
    return await asyncio.to_thread(settings_response)


@app.post("/api/settings")
async def update_settings(request: SettingsRequest) -> dict:
    global MEDIA_ROOT, EXPORT_ROOT

    if render_task and not render_task.done():
        raise HTTPException(409, "Wait for the current render to finish first")

    new_input = validate_folder(request.input_dir, "input")
    new_output = validate_folder(request.output_dir, "output")
    if not folder_writable(new_output):
        raise HTTPException(409, "The output folder is read-only")
    if new_output == new_input:
        raise HTTPException(400, "Use different input and output folders")
    if new_output.is_relative_to(new_input):
        # Rendered clips would otherwise show up as new source videos.
        raise HTTPException(
            400, "The output folder cannot be inside the input folder"
        )

    if new_input != MEDIA_ROOT:
        save_queue()  # park this folder's queue before switching
        for job_id in list(proxies.tasks):
            await proxies.stop(job_id)
        MEDIA_ROOT = new_input
        activate_folder_state()
    EXPORT_ROOT = new_output
    save_settings()
    save_queue()
    return {**settings_response(), "state": state_response()}


@app.get("/api/folders")
async def list_folders(path: str = "") -> dict:
    roots = browse_roots()
    if not path:
        return {
            "path": "",
            "parent": None,
            "folders": [{"name": str(r), "path": str(r)} for r in roots],
        }

    folder = Path(path).resolve()
    if not inside_browse_roots(folder):
        raise HTTPException(400, "That folder is outside the mounted folders")
    if not folder.is_dir():
        raise HTTPException(404, "Folder not found")

    def read() -> list[dict]:
        entries = []
        try:
            for child in folder.iterdir():
                if child.name.startswith(".") or not child.is_dir():
                    continue
                entries.append({"name": child.name, "path": str(child)})
        except OSError as exc:
            raise HTTPException(403, f"Cannot open folder: {exc}") from exc
        entries.sort(key=lambda e: e["name"].lower())
        return entries

    parent = None if folder in roots else str(folder.parent)
    return {
        "path": str(folder),
        "parent": parent,
        "writable": folder_writable(folder),
        "folders": await asyncio.to_thread(read),
    }


@app.post("/api/folders")
async def create_folder(request: NewFolderRequest) -> dict:
    parent = Path(request.parent).resolve()
    if not inside_browse_roots(parent) or not parent.is_dir():
        raise HTTPException(400, "Choose a folder inside the mounted folders")
    name = sanitize_filename(request.name)
    target = parent / name
    try:
        target.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise HTTPException(409, "A folder with that name already exists") from exc
    except OSError as exc:
        raise HTTPException(409, f"Cannot create folder: {exc}") from exc
    return {"path": str(target), "name": name}
