"""Bounded, resume-aware HLS jobs. Every playlist has an immutable job ID."""

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

THREADS = max(1, int(os.getenv("FFMPEG_CPU_THREADS", "2")))
HEIGHT = 360
RATE = max(1.0, float(os.getenv("PROXY_READRATE", "2")))
IDLE_SECONDS = max(30, int(os.getenv("PROXY_IDLE_SECONDS", "120")))
RETAIN_SECONDS = max(300, int(os.getenv("PROXY_CACHE_SECONDS", "86400")))
REQUIRE_GPU = os.getenv("REQUIRE_FULL_GPU", "0") == "1"
PIPELINES = [
    ("cuda", "NVIDIA NVDEC + CUDA resize + NVENC"),
    ("upload", "CPU decode + CUDA resize + NVENC"),
    ("nvenc", "CPU decode/resize + NVENC"),
    ("cpu", "CPU decode/resize/encode (limited threads)"),
]


def pipeline_args(
    mode: str, height: int | None = HEIGHT
) -> tuple[list[str], list[str]]:
    """Input-side hardware options and output-side filter/encoder options."""
    before = ["-threads", str(THREADS)]
    if mode == "cuda":
        before += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        vf = (
            f"scale_cuda=-2:{height}:format=nv12"
            if height
            else "scale_cuda=format=nv12"
        )
    elif mode == "upload":
        vf = "format=nv12,hwupload_cuda"
        if height:
            vf += f",scale_cuda=-2:{height}:format=nv12"
    else:
        vf = f"scale=-2:{height},format=yuv420p" if height else "format=yuv420p"
    after = ["-vf", vf]
    if mode == "cpu":
        after += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28" if height else "18",
            "-threads",
            str(THREADS),
        ]
    else:
        # CUDA-resident frames MUST NOT be forced to software -pix_fmt yuv420p.
        after += [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p1" if height else "p4",
            "-rc",
            "vbr",
            "-cq",
            "28" if height else "19",
            "-b:v",
            "0",
        ]
    if height:
        after += [
            "-r",
            "30",
            "-maxrate",
            "1000k",
            "-bufsize",
            "2000k",
            "-g",
            "60",
            "-keyint_min",
            "60",
            "-sc_threshold",
            "0",
            "-force_key_frames",
            "expr:gte(t,n_forced*2)",
        ]
        after += ["-tune", "zerolatency" if mode == "cpu" else "ll", "-bf", "0"]
    return before, after


async def capture(args: list[str], timeout: float = 25) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
        return (
            process.returncode,
            out.decode(errors="replace"),
            err.decode(errors="replace"),
        )
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


async def choose_pipeline(source: Path, start: float, height: int | None = HEIGHT):
    failures = []
    for mode, label in PIPELINES:
        if REQUIRE_GPU and mode != "cuda":
            break
        before, after = pipeline_args(mode, height)
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-filter_threads",
            str(THREADS),
            "-ss",
            str(start),
            *before,
            "-i",
            str(source),
            "-map",
            "0:v:0",
            *after,
            "-frames:v",
            "3",
            "-an",
            "-f",
            "null",
            "-",
        ]
        try:
            code, _, error = await capture(command)
        except asyncio.TimeoutError:
            code, error = -1, "Pipeline test timed out"
        if code == 0:
            return mode, label, failures
        failures.append(f"{label}: {error.strip()[-1400:]}")
    raise RuntimeError("No usable pipeline. " + " | ".join(failures))


class ProxyManager:
    def __init__(self, root: Path, resolve):
        self.root = root / "v2"
        self.root.mkdir(parents=True, exist_ok=True)
        self.resolve = resolve
        self.jobs = {}
        self.tasks = {}
        self.processes = {}
        self.lock = asyncio.Lock()
        self.probe_cache = {}
        self.reaper = None
        # Only completed, versioned jobs survive restart. Never treat a killed
        # partial encode as complete, and never import old zero-based caches.
        for meta in self.root.glob("*/job.json"):
            try:
                job = json.loads(meta.read_text())
                if (
                    re.fullmatch(r"[a-f0-9]{32}", job["id"])
                    and job["id"] == meta.parent.name
                    and job["status"] == "ready"
                    and time.time() - job["last_activity"] < RETAIN_SECONDS
                ):
                    self.jobs[job["id"]] = job
                else:
                    shutil.rmtree(meta.parent, ignore_errors=True)
            except (ValueError, KeyError, OSError, TypeError):
                continue

    def directory(self, job):
        return self.root / job["id"]

    @staticmethod
    def signature(source):
        stat = source.stat()
        return hashlib.sha256(
            f"{source}:{stat.st_size}:{stat.st_mtime_ns}".encode()
        ).hexdigest()

    async def probe(self, source):
        signature = self.signature(source)
        if signature not in self.probe_cache:
            code, out, error = await capture(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_type",
                    "-of",
                    "json",
                    str(source),
                ]
            )
            if code:
                raise HTTPException(422, "Cannot inspect video: " + error[-500:])
            data = json.loads(out)
            duration = float(data.get("format", {}).get("duration", 0))
            if not math.isfinite(duration) or duration <= 0:
                raise HTTPException(422, "Video has no usable duration")
            if not any(s["codec_type"] == "video" for s in data.get("streams", [])):
                raise HTTPException(422, "Source contains no video stream")
            self.probe_cache[signature] = (
                duration,
                any(s["codec_type"] == "audio" for s in data.get("streams", [])),
            )
        return self.probe_cache[signature]

    def save(self, job):
        directory = self.directory(job)
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / "job.tmp"
        tmp.write_text(json.dumps(job, indent=2))
        tmp.replace(directory / "job.json")

    def get(self, job_id):
        job = self.jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Proxy expired; select the video again")
        job["last_activity"] = time.time()
        return job

    def view(self, job):
        playlist = self.directory(job) / "index.m3u8"
        try:
            text = playlist.read_text()
        except OSError:
            text = ""
        lengths = re.findall(r"^#EXTINF:([\d.]+)", text, re.M)
        available = sum(float(x) for x in lengths)
        return {
            **job,
            "playable": bool(lengths),
            "available_seconds": available,
            "available_end": job["start_seconds"] + available,
            "segment_count": len(lengths),
            "progress": min(
                100,
                round(
                    100 * available / max(0.001, job["duration"] - job["start_seconds"])
                ),
            ),
            "url": f"/api/proxy/playlist?id={job['id']}",
            "switch_lead_seconds": 2,
        }

    async def stop(self, job_id):
        task = self.tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        job = self.jobs.get(job_id)
        if job and job["status"] == "creating":
            job.update(
                status="stopped", message="Inactive proxy stopped; reopen to resume"
            )
            self.save(job)
        self.tasks.pop(job_id, None)

    async def start(self, path, requested):
        source = self.resolve(path)
        duration, audio = await self.probe(source)
        # Reopening a video at EOF should still yield a playable final second.
        start = round(min(max(0.0, requested), max(0.0, duration - 1)), 3)
        signature = self.signature(source)
        async with self.lock:
            candidate = None
            for job in self.jobs.values():
                if job["signature"] != signature or job["status"] not in {
                    "ready",
                    "creating",
                }:
                    continue
                info = self.view(job)
                if job["status"] == "ready" and not info["playable"]:
                    continue
                if job["start_seconds"] <= start and (
                    info["available_end"] > start + 0.05
                    or abs(job["start_seconds"] - start) < 0.001
                ):
                    candidate = job
                    break
            # This is a single-editor app: selecting/seeking elsewhere stops
            # old active work before launching another process.
            for job_id in list(self.tasks):
                if not candidate or job_id != candidate["id"]:
                    await self.stop(job_id)
            if candidate:
                candidate["last_activity"] = time.time()
                return self.view(candidate)
            job = {
                "id": uuid.uuid4().hex,
                "path": path,
                "signature": signature,
                "start_seconds": start,
                "duration": duration,
                "audio": audio,
                "status": "creating",
                "pipeline": "",
                "fallback_reason": "",
                "error": "",
                "message": "Testing GPU pipeline on this video",
                "last_activity": time.time(),
            }
            self.jobs[job["id"]] = job
            self.save(job)
            self.tasks[job["id"]] = asyncio.create_task(self.encode(job, source))
            return self.view(job)

    def command(self, job, source, mode):
        before, after = pipeline_args(mode)
        directory = self.directory(job)
        return [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-loglevel",
            "warning",
            "-filter_threads",
            str(THREADS),
            # Input-side seek: skip the earlier hour BEFORE decoding.
            "-ss",
            str(job["start_seconds"]),
            "-readrate",
            str(RATE),
            *before,
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            *after,
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-ac",
            "2",
            "-f",
            "hls",
            "-hls_time",
            "2",
            "-hls_list_size",
            "0",
            "-hls_playlist_type",
            "event",
            "-hls_flags",
            "independent_segments+temp_file",
            "-hls_segment_options",
            "mpegts_copyts=1",
            "-hls_segment_filename",
            str(directory / "segment_%05d.ts"),
            "-progress",
            "pipe:1",
            "-nostats",
            str(directory / "index.m3u8"),
        ]

    async def encode(self, job, source):
        process = None
        try:
            mode, label, failures = await choose_pipeline(source, job["start_seconds"])
            job.update(
                pipeline=label,
                fallback_reason=" | ".join(failures),
                message="Encoding forward from the requested position",
            )
            self.save(job)
            print(f"[proxy] start={job['start_seconds']} pipeline={label}", flush=True)
            if failures:
                print("[proxy-fallback] " + job["fallback_reason"], flush=True)
            process = await asyncio.create_subprocess_exec(
                *self.command(job, source, mode),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self.processes[job["id"]] = process
            recent = deque(maxlen=35)
            async for line in process.stdout:
                recent.append(line.decode(errors="replace").strip())
            code = await process.wait()
            if code:
                # Do not rewrite a playlist already being consumed by a player.
                raise RuntimeError("FFmpeg failed: " + " | ".join(recent))
            job.update(status="ready", message="Proxy ready")
        except asyncio.CancelledError:
            job.update(
                status="stopped", message="Inactive proxy stopped; reopen to resume"
            )
            raise
        except Exception as exc:  # noqa: BLE001 -- job boundary reports failures to the UI
            job.update(status="error", message="Proxy failed", error=str(exc))
        finally:
            if process and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            self.processes.pop(job["id"], None)
            self.save(job)

    async def delete_path(self, path):
        async with self.lock:
            for job_id, job in list(self.jobs.items()):
                if job["path"] == path:
                    await self.stop(job_id)
                    shutil.rmtree(self.directory(job), ignore_errors=True)
                    self.jobs.pop(job_id, None)

    async def reap(self):
        while True:
            await asyncio.sleep(10)
            async with self.lock:
                now = time.time()
                for job_id, job in list(self.jobs.items()):
                    idle = now - job["last_activity"]
                    if idle > IDLE_SECONDS:
                        await self.stop(job_id)
                    if idle > RETAIN_SECONDS:
                        shutil.rmtree(self.directory(job), ignore_errors=True)
                        self.jobs.pop(job_id, None)

    async def close(self):
        if self.reaper:
            self.reaper.cancel()
            await asyncio.gather(self.reaper, return_exceptions=True)
        for job_id in list(self.tasks):
            await self.stop(job_id)

    def router(self):
        router = APIRouter()

        @router.get("/api/proxy/status")
        async def status(id: str):
            return self.view(self.get(id))

        @router.get("/api/proxy/heartbeat")
        async def heartbeat(id: str):
            self.get(id)
            return {"ok": True}

        @router.get("/api/proxy/playlist")
        async def playlist(id: str):
            job = self.get(id)
            path = self.directory(job) / "index.m3u8"
            if not path.exists():
                raise HTTPException(
                    503, "Proxy is starting", headers={"Retry-After": "1"}
                )
            text = path.read_text()
            text = re.sub(
                r"^(segment_\d+\.ts)$",
                lambda m: f"segment/{m[1]}?id={quote(id)}",
                text,
                flags=re.M,
            )
            # EVENT playlists are not a live broadcast: start at the beginning
            # of this offset window, not at its growing live edge.
            text = text.replace(
                "#EXTM3U", "#EXTM3U\n#EXT-X-START:TIME-OFFSET=0,PRECISE=YES", 1
            )
            return Response(
                text,
                media_type="application/vnd.apple.mpegurl",
                headers={"Cache-Control": "no-store"},
            )

        @router.get("/api/proxy/segment/{name}")
        async def segment(name: str, id: str):
            job = self.get(id)
            if not re.fullmatch(r"segment_\d+\.ts", name):
                raise HTTPException(400, "Invalid segment name")
            path = self.directory(job) / name
            if not path.is_file():
                raise HTTPException(404, "Segment not found")
            return FileResponse(
                path,
                media_type="video/mp2t",
                headers={"Cache-Control": "private, max-age=86400"},
            )

        return router
