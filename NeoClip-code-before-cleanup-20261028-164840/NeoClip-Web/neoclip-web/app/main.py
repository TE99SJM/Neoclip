import asyncio
import errno
import logging
import math
import os
import re
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .continuous_proxy import ContinuousProxyManager
from .deletion import VideoChangedError, fingerprint, remove_video
from .library import VideoLibrary
from .library_scan import scan_directory
from .metadata import VideoMetadata
from .player_diagnostics import player_diagnostics_router
from .proxies import THREADS, choose_pipeline, pipeline_args
from .queue_store import QueueStore
from .renaming import MediaRenamer, clipped_name


@asynccontextmanager
async def lifespan(app):
    try:
        await asyncio.to_thread(media_renamer().recover)
    except Exception:
        logging.getLogger(__name__).exception("Could not complete a pending file rename")
    library_index.start()
    proxies.reaper = asyncio.create_task(proxies.reap())
    proxies.start_background()
    yield
    await proxies.close()
    await library_index.close()
    await video_metadata.close()
    if render_task and not render_task.done():
        render_task.cancel()
        await asyncio.gather(render_task, return_exceptions=True)


app = FastAPI(title="NeoClip 240", lifespan=lifespan)
APP_BUILD = "2.2.0-playback.2"
app.include_router(player_diagnostics_router(APP_BUILD))
if origins := os.getenv("NEOCLIP_CORS_ORIGINS"):
    # Optional for a separate private preview frontend; disabled in Compose.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins.split(","),
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )


MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", "/media")).resolve()
EXPORT_ROOT = Path(os.getenv("EXPORT_ROOT", "/exports")).resolve()
DATA_ROOT = Path(os.getenv("DATA_ROOT", "/data")).resolve()
WEB_ROOT = Path(__file__).parent / "web"
QUEUE_FILE = Path(os.getenv("NEOCLIP_QUEUE_FILE", str(DATA_ROOT / "neoclip_queue.json")))
PROXY_ROOT = DATA_ROOT / "proxies"
PROXY_HEIGHT = 240

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm"}

DATA_ROOT.mkdir(parents=True, exist_ok=True)
EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
PROXY_ROOT.mkdir(parents=True, exist_ok=True)


class SourceRequest(BaseModel):
    path: str
    native_vod: bool = False
    current_time: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)


class RenameRequest(BaseModel):
    path: str
    name: str


class ClipRequest(BaseModel):
    path: Optional[str] = None
    name: str = ""
    start_s: float = Field(ge=0, allow_inf_nan=False)
    end_s: float = Field(gt=0, allow_inf_nan=False)


class RenderRequest(BaseModel):
    path: Optional[str] = None
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

media_change_lock = asyncio.Lock()
deleting_paths = set()
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


def resolve_media_path(relative_path: str) -> Path:
    candidate = (MEDIA_ROOT / relative_path).resolve()

    try:
        candidate.relative_to(MEDIA_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid media path") from exc

    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")

    return candidate


video_metadata = VideoMetadata(DATA_ROOT / "video_metadata.json", resolve_media_path)
proxies = ContinuousProxyManager(PROXY_ROOT, resolve_media_path)
native_proxies = proxies  # Same durable 240p jobs for Safari and other browsers.
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


_queue_store = None


def queue_store():
    global _queue_store
    if (_queue_store is None or _queue_store.path != QUEUE_FILE
            or _queue_store.state is not state):
        _queue_store = QueueStore(QUEUE_FILE, state)
    return _queue_store


def save_queue() -> None:
    queue_store().save()


def load_queue() -> None:
    queue_store().refresh(startup=True)


def refresh_queue(path=None):
    store = queue_store()
    store.refresh()
    if path is not None:
        state["source_path"] = store.resolve(path)


def render_lease():
    try:
        return queue_store().acquire_render()
    except BlockingIOError as exc:
        raise HTTPException(409, "A render or file change is running in the other NeoClip app. Wait for it to finish.") from exc


async def shared_render(lease, *args):
    try:
        await render_job(*args)
    finally:
        lease.close()


load_queue()


library_index = VideoLibrary(MEDIA_ROOT, DATA_ROOT / "video_library.json")


async def remove_missing_proxy(path):
    if path not in deleting_paths:
        await proxies.delete_path(path)


library_index.on_remove = remove_missing_proxy


def media_renamer():
    return MediaRenamer(
        root=MEDIA_ROOT, data_root=DATA_ROOT, state=state, save_state=save_queue,
        move_state=queue_store().rename,
        managers=[proxies, proxies.legacy], library=library_index, metadata=video_metadata,
        lock=media_change_lock, busy=deleting_paths,
        rendering=lambda path: render_state.get("source_path") == path
        and render_task is not None and not render_task.done(),
    )


@app.post("/api/media/rename")
async def rename_media(request: RenameRequest):
    refresh_queue()
    with render_lease():
        return await media_renamer().rename(queue_store().resolve(request.path), request.name)


def list_videos() -> list[dict]:
    """Synchronous diagnostic helper; HTTP requests use the background index."""
    results = []
    folders = ["."]
    while folders:
        try:
            for record in scan_directory(MEDIA_ROOT, folders.pop()):
                if "file" in record:
                    results.append(record["file"])
                elif "directory" in record:
                    folders.append(record["directory"])
        except OSError:
            continue
    return sorted(results, key=lambda item: item["path"].lower())


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
    output_dir = output_dir or EXPORT_ROOT / sanitize_filename(source.stem)
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
    output_dir = EXPORT_ROOT / sanitize_filename(source.stem)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{prefix}_reel.mp4"

    render_state["message"] = "Rendering highlight reel"
    render_state["current_file"] = output_path.name

    # Render sequentially instead of opening N decoders/filter graphs at once.
    # Joining matched H.264/AAC intermediates requires no second video encode.
    with tempfile.TemporaryDirectory(prefix="reel-", dir=EXPORT_ROOT) as temp:
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
            renamed_from="", renamed_to="", rename_warning="",
        )

        source = resolve_media_path(rendered_source)
        rendered_fingerprint = await asyncio.to_thread(fingerprint, source)
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

        render_state.update(status="finalizing", progress=99,
                            message="Finishing render and updating filename", current_file="")

        # Remove only the rendered snapshot, not newly queued clips or a
        # different video's queue selected while this job was running.
        refresh_queue()
        remaining = state["queues"].get(rendered_source, [])
        for clip in clips:
            if clip in remaining:
                remaining.remove(clip)
        save_queue()
        warning = ""
        if not state["queues"].get(rendered_source):
            try:
                renamed = await media_renamer().rename(
                    rendered_source, clipped_name(rendered_source),
                    after_render=True, expected=rendered_fingerprint,
                )
                warning = renamed.get("warning", "")
                if renamed["path"] != rendered_source:
                    render_state.update(renamed_from=rendered_source, renamed_to=renamed["path"],
                                        source_path=renamed["path"])
            except Exception as exc:
                # The exports succeeded. A name conflict/NAS permission error
                # must not misreport that success or cause duplicate rendering.
                warning = "Clips saved, but the source filename was not updated: " + str(getattr(exc, "detail", exc))
        render_state.update(status="done", progress=100, current_file="", error="",
                            rename_warning=warning,
                            message="Render complete" + (" — " + warning if warning else ""))

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
async def get_files(details: bool = False) -> JSONResponse:
    library_index.start()
    listing = library_index.snapshot()
    listing["files"] = video_metadata.enrich(listing["files"], MEDIA_ROOT)
    return JSONResponse(
        listing if details else listing["files"], headers={"Cache-Control": "no-store"}
    )


@app.get("/api/media")
async def get_media(path: str) -> FileResponse:
    if path in deleting_paths:
        raise HTTPException(409, "This video is being deleted")
    media_path = resolve_media_path(path)

    return FileResponse(
        media_path,
        filename=media_path.name,
        content_disposition_type="inline",
    )


@app.post("/api/source")
async def select_source(request: SourceRequest) -> dict:
    refresh_queue()
    request.path = queue_store().resolve(request.path)
    if request.path in deleting_paths:
        raise HTTPException(409, "This video is being deleted")
    async with media_change_lock:
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
        if start >= proxy["duration"] - 0.25:
            start = 0
            state["editor_sessions"][request.path]["current_time"] = 0
            save_queue()
        return {**state_response(), "proxy": proxy}


@app.post("/api/proxy/queue")
async def queue_proxy(request: SourceRequest) -> dict:
    if request.path in deleting_paths:
        raise HTTPException(409, "This video is being deleted")
    job = await proxies.enqueue(request.path, request.current_time or 0)
    return proxies.view(job)


@app.post("/api/proxy/start")
async def start_proxy(request: SourceRequest) -> dict:
    if request.path in deleting_paths:
        raise HTTPException(409, "This video is being deleted")
    async with media_change_lock:
        return await proxies.start(request.path, request.current_time or 0)


@app.post("/api/media/delete")
async def delete_media(request: SourceRequest) -> dict:
    global render_task

    relative_path = request.path
    if relative_path in deleting_paths:
        raise HTTPException(409, "This video is already being deleted")
    deleting_paths.add(relative_path)
    try:
        async with media_change_lock:
            source = await asyncio.to_thread(resolve_media_path, relative_path)
            expected = await asyncio.to_thread(fingerprint, source)
            async with video_metadata.pause(MEDIA_ROOT / relative_path), proxies.pause_path(relative_path):
                if (render_state.get("source_path") == relative_path
                        and render_task is not None and not render_task.done()):
                    task = render_task
                    task.cancel()
                    # FFmpeg's finally block closes the source before unlink.
                    await asyncio.gather(task, return_exceptions=True)
                    render_task = None

                with render_lease():
                    await remove_video(source, expected)
                await proxies.delete_path(relative_path)

                # Only discard edits and cached rows after physical deletion.
                queue_store().forget(relative_path)
                await library_index.forget(relative_path)
                await video_metadata.forget(MEDIA_ROOT / relative_path)
                return {"ok": True, "deleted": relative_path, "exists": False}
    except VideoChangedError as exc:
        raise HTTPException(409, "The video changed during deletion. Review it before trying again.") from exc
    except PermissionError as exc:
        raise HTTPException(409, "The NAS denied deletion. Check the share's delete permission and that /media is writable.") from exc
    except OSError as exc:
        if exc.errno in {errno.EAGAIN, errno.EBUSY}:
            raise HTTPException(409, "The NAS is still busy or the video is in use. Stop other playback or transfers, wait a moment, then try Delete again. Your clips and marks have been kept.") from exc
        raise HTTPException(500, f"The video could not be deleted from disk: {exc}") from exc
    finally:
        deleting_paths.discard(relative_path)


@app.get("/api/state")
async def get_state(path: Optional[str] = None) -> dict:
    refresh_queue(path)
    return state_response()


@app.post("/api/session")
async def save_editor_session(request: EditorSessionRequest) -> dict:
    refresh_queue()
    request.path = queue_store().resolve(request.path)
    if request.path in deleting_paths:
        raise HTTPException(409, "This video is being changed")
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
    refresh_queue(request.path)
    if state["source_path"]:
        resolve_media_path(state["source_path"])
    if state["source_path"] in deleting_paths:
        raise HTTPException(409, "This video is being changed")
    if not state["source_path"]:
        raise HTTPException(status_code=400, detail="Select a video first")

    if request.end_s <= request.start_s:
        raise HTTPException(status_code=400, detail="OUT must be after IN")

    clips = active_clips()
    name = request.name.strip() or f"clip_{len(clips) + 1:03d}"

    clips.append(
        {
            "id": uuid.uuid4().hex,
            "name": name,
            "start_s": request.start_s,
            "end_s": request.end_s,
        }
    )

    save_queue()
    return state_response()


@app.delete("/api/clips/{index}")
async def delete_clip(index: int, path: Optional[str] = None, clip_id: Optional[str] = None) -> dict:
    refresh_queue(path)
    if state["source_path"] in deleting_paths:
        raise HTTPException(409, "This video is being changed")
    clips = active_clips()

    if clip_id is not None:
        index = next((i for i, clip in enumerate(clips) if clip.get("id") == clip_id), -1)

    if index < 0 or index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")

    del clips[index]

    if not clips and state["source_path"]:
        state["queues"].pop(state["source_path"], None)

    save_queue()
    return state_response()


@app.delete("/api/clips")
async def clear_clips(path: Optional[str] = None) -> dict:
    refresh_queue(path)
    if state["source_path"] in deleting_paths:
        raise HTTPException(409, "This video is being changed")
    if state["source_path"]:
        state["queues"].pop(state["source_path"], None)

    save_queue()
    return state_response()


@app.post("/api/render")
async def start_render(request: RenderRequest) -> dict:
    global render_task
    refresh_queue(request.path)

    if state["source_path"] in deleting_paths:
        raise HTTPException(409, "This video is being deleted")

    if render_task and not render_task.done():
        raise HTTPException(status_code=409, detail="A render is already running")

    if not state["source_path"]:
        raise HTTPException(status_code=400, detail="Select a source video first")

    if not active_clips():
        raise HTTPException(status_code=400, detail="Add at least one clip")

    if request.mode not in {"individual", "reel", "both"}:
        raise HTTPException(400, "Invalid render mode")
    lease = render_lease()
    refresh_queue(request.path)
    if not active_clips():
        lease.close()
        raise HTTPException(400, "These clips were already processed or removed")
    render_task = asyncio.create_task(
        shared_render(lease,
            request.mode,
            request.prefix,
            state["source_path"],
            [dict(clip) for clip in active_clips()],
        )
    )

    render_task.add_done_callback(lambda task: lease.close())
    return {"started": True}


@app.post("/api/render/cancel")
async def cancel_render() -> dict:
    if render_state.get("status") == "finalizing":
        return {"cancelled": False, "message": "Clips are saved; finishing filename update"}
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
        "media_available": MEDIA_ROOT.exists(),
        "export_available": EXPORT_ROOT.exists(),
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
        "proxy_height": PROXY_HEIGHT,
        "proxy_directory": str(PROXY_ROOT),
        "queue_file": str(QUEUE_FILE),
        "build": APP_BUILD,
        "cpu_threads": THREADS,
        "active_proxy_processes": len(proxies.processes),
        "pip_note": "Browser/OS support required; open the app directly, not in an embedded frame",
    }

