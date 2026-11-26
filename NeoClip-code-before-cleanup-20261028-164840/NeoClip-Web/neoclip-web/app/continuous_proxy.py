"""Continuous 240p HLS: one encoder and timestamp sequence per generation."""

import asyncio
import re
import shutil
import time
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from .persistent_proxy import PersistentProxyManager
from .proxies import PIPELINES, THREADS, pipeline_args
from .vod import SEGMENT_SECONDS


class ContinuousProxyManager(PersistentProxyManager):
    cache_name = "240-continuous-v1"
    segment_wait_seconds = 20
    progress_timeout = 90

    def __init__(self, root, resolve, clock=time.time):
        super().__init__(root, resolve, clock)
        # Old test proxies retain their original expiry. They are never mixed
        # into the new manifests, and are still removed with their source.
        self.legacy = PersistentProxyManager(root, resolve, clock)
        for job in self.jobs.values():
            self.built[job["id"]] = set()
            try:
                self._published(job)
            except (OSError, RuntimeError, ValueError):
                self.built[job["id"]] = set()
            directory = self.generation_dir(job)
            intact = directory is not None and all(
                (directory / f"segment_{index:05d}.ts").is_file()
                for index in range(self.count(job))
            )
            if intact and job.get("continuous_complete") and len(self.built[job["id"]]) == self.count(job):
                job["build_status"] = "complete"
            else:
                job["build_status"] = "queued"
                job["continuous_complete"] = False
                job["available_end"] = 0
                self.built[job["id"]] = set()

    def generation_dir(self, job):
        generation = job.get("generation", "")
        if not re.fullmatch(r"[a-f0-9]{32}", generation):
            return None
        return self.directory(job) / generation

    def _published(self, job):
        directory = self.generation_dir(job)
        if directory is None:
            return
        try:
            text = (directory / "encoder.m3u8").read_text()
        except FileNotFoundError:
            return
        # FFmpeg publishes the playlist atomically, after closing each TS file.
        lengths = [float(n) for n in re.findall(r"^#EXTINF:([\d.]+)", text, re.M)]
        names = re.findall(r"^segment_(\d+)\.ts$", text, re.M)
        if len(names) != len(lengths) or [int(n) for n in names] != list(range(len(names))):
            raise RuntimeError("Invalid continuous preview playlist")
        self.built[job["id"]] = set(range(len(names)))
        job["available_end"] = min(job["duration"], sum(lengths))

    def view(self, job):
        view = super().view(job)
        view.update(
            continuous=True,
            playable=bool(self.built.get(job["id"])),
            available_end=job.get("available_end", 0),
            available_seconds=job.get("available_end", 0),
            url=f"/api/vod/playlist.m3u8?id={job['id']}&generation={job.get('generation', '')}",
        )
        return view

    def playlist(self, job):
        lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-PLAYLIST-TYPE:VOD",
                 f"#EXT-X-TARGETDURATION:{SEGMENT_SECONDS}", "#EXT-X-MEDIA-SEQUENCE:0",
                 "#EXT-X-INDEPENDENT-SEGMENTS",
                 f"#EXT-X-START:TIME-OFFSET={job['resume_seconds']:.6f},PRECISE=YES"]
        for index in range(self.count(job)):
            length = min(SEGMENT_SECONDS, job["duration"] - index * SEGMENT_SECONDS)
            lines += [f"#EXTINF:{length:.6f},",
                      f"segment/segment_{index:05d}.ts?id={job['id']}&generation={job['generation']}"]
        return "\n".join([*lines, "#EXT-X-ENDLIST", ""])

    def continuous_command(self, job, source, directory):
        before, after = pipeline_args(job["mode"], height=240)
        return [
            "ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
            "-progress", "pipe:1", "-nostats", "-filter_threads", str(THREADS),
            *before, "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", *after,
            "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-ar", "48000",
            "-t", str(job["duration"]), "-f", "hls", "-hls_time", str(SEGMENT_SECONDS),
            "-hls_playlist_type", "event", "-hls_flags", "independent_segments+temp_file",
            "-hls_segment_options", "mpegts_copyts=1", "-muxdelay", "0", "-muxpreload", "0",
            "-hls_segment_filename", str(directory / "segment_%05d.ts"),
            str(directory / "encoder.m3u8"),
        ]

    async def _encode(self, job, source, directory):
        process = await asyncio.create_subprocess_exec(
            *self.continuous_command(job, source, directory),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        key = (job["id"], -1)
        self.processes[key] = process
        errors = bytearray()

        async def read_errors():
            while chunk := await process.stderr.read(4096):
                errors.extend(chunk)
                del errors[:-6000]

        reader = asyncio.create_task(read_errors())
        last_progress = time.monotonic()
        last_frame = -1
        try:
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), self.progress_timeout)
                if not line:
                    break
                if line.startswith(b"frame="):
                    frame = int(line.split(b"=", 1)[1])
                    if frame > last_frame:
                        last_frame = frame
                        last_progress = time.monotonic()
                if time.monotonic() - last_progress > self.progress_timeout:
                    raise TimeoutError("Continuous preview encoder stopped making progress")
                if line.startswith(b"progress="):
                    self._published(job)
            await asyncio.wait_for(process.wait(), self.progress_timeout)
            await reader
            self._published(job)
            if process.returncode:
                raise RuntimeError(errors.decode(errors="replace") or "Continuous preview encoder failed")
            if last_frame <= 0 or len(self.built[job["id"]]) != self.count(job):
                raise RuntimeError("Source ended before the advertised preview duration")
        except TimeoutError:
            raise RuntimeError(f"Continuous preview made no progress for {self.progress_timeout} seconds") from None
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            self.processes.pop(key, None)

    async def _build(self, job):
        await self.prepare(job)
        async with self.workers:
            source = await asyncio.to_thread(self.resolve, job["path"])
            if await asyncio.to_thread(self.signature, source) != job["signature"]:
                raise HTTPException(409, "Video changed; select it again")
            while True:
                old_directory = self.generation_dir(job)
                # Every retry/restart gets new URLs. No stream can accidentally
                # splice a restarted encoder's counters/timestamps into old TS.
                job.update(generation=uuid.uuid4().hex, available_end=0,
                           continuous_complete=False, build_status="building")
                self.built[job["id"]] = set()
                directory = self.generation_dir(job)
                directory.mkdir(parents=True)
                self.save(job)
                if old_directory:
                    await asyncio.to_thread(shutil.rmtree, old_directory, True)
                try:
                    await self._encode(job, source, directory)
                    break
                except RuntimeError as exc:
                    fallback = self.fallback_mode(job["mode"], str(exc))
                    if fallback is None:
                        raise
                    job.update(mode=fallback, pipeline=dict(PIPELINES)[fallback],
                               fallback_reason=str(exc)[-3000:])
            if await asyncio.to_thread(self.signature, source) != job["signature"]:
                raise HTTPException(409, "Video changed; select it again")
            job["continuous_complete"] = True
            self._completed(job)
            self.save(job)

    async def segment(self, job, index, generation=None):
        if not 0 <= index < self.count(job):
            raise HTTPException(404, "Segment not found")
        generation = generation or job.get("generation")
        deadline = asyncio.get_running_loop().time() + self.segment_wait_seconds
        while True:
            self.get(job["id"])
            if job["path"] in self.held or self.closing:
                raise HTTPException(409, "This proxy is being removed")
            if generation != job.get("generation") or self.generation_dir(job) is None:
                raise HTTPException(410, "Preview restarted; reconnect to the current preview")
            if index in self.built[job["id"]]:
                return FileResponse(self.generation_dir(job) / f"segment_{index:05d}.ts",
                                    media_type="video/mp2t",
                                    headers={"Cache-Control": "private, max-age=86400"})
            if job.get("build_status") == "retrying" or asyncio.get_running_loop().time() >= deadline:
                raise HTTPException(503, "Preview is still building", headers={"Retry-After": "2"})
            await asyncio.sleep(.25)

    async def delete_path(self, path):
        await super().delete_path(path)
        await self.legacy.delete_path(path)

    async def reap_once(self):
        await super().reap_once()
        await self.legacy.reap_once()

    async def close(self):
        await super().close()
        await self.legacy.close()

    def router(self):
        router = APIRouter()

        @router.get("/api/vod/status")
        async def status(id: str):
            return self.view(self.get(id))

        @router.get("/api/vod/playlist.m3u8")
        async def playlist(id: str, generation: str):
            job = self.get(id)
            if generation != job.get("generation"):
                raise HTTPException(410, "Preview restarted; reconnect")
            return Response(self.playlist(job), media_type="application/vnd.apple.mpegurl",
                            headers={"Cache-Control": "no-store"})

        @router.get("/api/vod/segment/{name}")
        async def segment(name: str, id: str, generation: str):
            if not re.fullmatch(r"segment_\d{5,8}\.ts", name):
                raise HTTPException(400, "Invalid segment name")
            return await self.segment(self.get(id), int(name[8:-3]), generation)

        return router
