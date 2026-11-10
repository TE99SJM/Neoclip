"""Fixed-duration HLS for Safari's native scrubber, encoded on demand.

Every advertised segment can be requested immediately, including the end of a
long recording. An uncached request encodes just that six-second section. The
playlist never grows or changes, and its time zero is the source's time zero.
"""

import asyncio
import math
import re
import time
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from .proxies import THREADS, ProxyManager, choose_pipeline, pipeline_args

SEGMENT_SECONDS = 6


class VodProxyManager(ProxyManager):
    def __init__(self, root, resolve):
        # Separate cache/schema: old offset EVENT jobs cannot become VOD jobs.
        super().__init__(root / "native-vod-v1", resolve)
        self.workers = asyncio.Semaphore(2)

    def view(self, job):
        return {
            **job,
            "playable": job["status"] == "ready",
            "available_seconds": job["duration"],
            "available_end": job["duration"],
            "segment_count": math.ceil(job["duration"] / SEGMENT_SECONDS),
            "delivery": "vod",
            "url": f"/api/vod/playlist.m3u8?id={job['id']}",
            "status_url": f"/api/vod/status?id={job['id']}",
            "switch_lead_seconds": 0,
        }

    async def start(self, path, requested):
        source = self.resolve(path)
        duration, audio = await self.probe(source)
        signature = self.signature(source)
        async with self.lock:
            candidate = next(
                (
                    j
                    for j in self.jobs.values()
                    if j["signature"] == signature and j["status"] == "ready"
                ),
                None,
            )
            for job_id in list(self.jobs):
                if not candidate or job_id != candidate["id"]:
                    await self.stop(job_id)
            if candidate:
                candidate["last_activity"] = time.time()
                return self.view(candidate)
            # This only tests three frames, never prepares the whole movie.
            start = min(max(0, requested), max(0, duration - 1))
            async with self.workers:
                try:
                    mode, label, failures = await choose_pipeline(source, start)
                except RuntimeError as exc:
                    raise HTTPException(422, str(exc)) from exc
            print(f"[native-vod] pipeline={label}", flush=True)
            if failures:
                print("[proxy-fallback] " + " | ".join(failures), flush=True)
            job = {
                "id": uuid.uuid4().hex,
                "path": path,
                "signature": signature,
                "start_seconds": 0,
                "resume_seconds": start,
                "duration": duration,
                "audio": audio,
                "mode": mode,
                "status": "ready",
                "pipeline": label,
                "fallback_reason": " | ".join(failures),
                "error": "",
                "message": "On-demand preview ready",
                "last_activity": time.time(),
            }
            self.jobs[job["id"]] = job
            self.save(job)
            return self.view(job)

    def playlist(self, job):
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:6",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            f"#EXT-X-TARGETDURATION:{SEGMENT_SECONDS}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-INDEPENDENT-SEGMENTS",
            f"#EXT-X-START:TIME-OFFSET={job['resume_seconds']:.6f},PRECISE=YES",
        ]
        for index in range(math.ceil(job["duration"] / SEGMENT_SECONDS)):
            length = min(SEGMENT_SECONDS, job["duration"] - index * SEGMENT_SECONDS)
            if index:
                # Each independently encoded section has its own PTS origin
                # and AAC priming. Explicitly reset the decoder's timestamp map.
                lines.append("#EXT-X-DISCONTINUITY")
            lines += [
                f"#EXTINF:{length:.6f},",
                f"segment/segment_{index:05d}.ts?id={job['id']}",
            ]
        return "\n".join([*lines, "#EXT-X-ENDLIST", ""])

    def segment_command(self, job, source, index, target):
        before, after = pipeline_args(job["mode"])
        start = index * SEGMENT_SECONDS
        length = min(SEGMENT_SECONDS, job["duration"] - start)
        return [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-filter_threads",
            str(THREADS),
            "-ss",
            str(start),
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
            "-t",
            str(length),
            "-f",
            "mpegts",
            "-mpegts_copyts",
            "1",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            str(target),
        ]

    async def encode_segment(self, job, index, target):
        temporary = target.with_suffix(".tmp")
        process = None
        key = (job["id"], index)
        try:
            async with self.workers:
                source = self.resolve(job["path"])
                if self.signature(source) != job["signature"]:
                    raise HTTPException(409, "Video changed; select it again")
                process = await asyncio.create_subprocess_exec(
                    *self.segment_command(job, source, index, temporary),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                self.processes[key] = process
                _, error = await asyncio.wait_for(process.communicate(), 45)
                if (
                    process.returncode
                    or not temporary.exists()
                    or not temporary.stat().st_size
                ):
                    raise RuntimeError(error.decode(errors="replace")[-1000:])
                # Source replacement while encoding must not poison the cache.
                if self.signature(source) != job["signature"]:
                    raise HTTPException(409, "Video changed; select it again")
                temporary.replace(target)
                job.update(error="", message="On-demand preview ready")
        except (RuntimeError, asyncio.TimeoutError, OSError) as exc:
            job.update(error=str(exc), message="Preview section failed; retry playback")
            raise HTTPException(
                503,
                "Cannot prepare preview section; retry playback",
                headers={"Retry-After": "1"},
            ) from exc
        finally:
            if process and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            temporary.unlink(missing_ok=True)
            self.processes.pop(key, None)
            self.tasks.pop(key, None)

    async def segment(self, job, index):
        if not 0 <= index < math.ceil(job["duration"] / SEGMENT_SECONDS):
            raise HTTPException(404, "Segment not found")
        source = self.resolve(job["path"])
        if self.signature(source) != job["signature"]:
            raise HTTPException(409, "Video changed; select it again")
        target = self.directory(job) / f"segment_{index:05d}.ts"
        if not target.is_file():
            key = (job["id"], index)
            task = self.tasks.get(key)
            if task is None:
                if len(self.tasks) >= 16:
                    raise HTTPException(
                        503, "Preview busy; retry shortly", headers={"Retry-After": "1"}
                    )
                task = asyncio.create_task(self.encode_segment(job, index, target))
                self.tasks[key] = task
                # Retrieving an abandoned request's error prevents unhandled
                # task warnings; every attached waiter still gets the error.
                task.add_done_callback(
                    lambda t: None if t.cancelled() else t.exception()
                )
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.cancelled():
                    raise HTTPException(
                        503,
                        "Preview paused; retry playback",
                        headers={"Retry-After": "1"},
                    ) from None
                raise
        return FileResponse(
            target,
            media_type="video/mp2t",
            headers={"Cache-Control": "private, max-age=86400"},
        )

    async def stop(self, job_id):
        tasks = [t for (owner, _), t in list(self.tasks.items()) if owner == job_id]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Completed sections and the immutable playlist stay reusable. Missing
        # sections can be requested again after a pause, reopen or restart.
        for key in list(self.tasks):
            if key[0] == job_id:
                self.tasks.pop(key, None)

    async def close(self):
        if self.reaper:
            self.reaper.cancel()
            await asyncio.gather(self.reaper, return_exceptions=True)
        for job_id in list(self.jobs):
            await self.stop(job_id)

    def router(self):
        router = APIRouter()

        @router.get("/api/vod/status")
        async def status(id: str):
            return self.view(self.get(id))

        @router.get("/api/vod/playlist.m3u8")
        async def playlist(id: str):
            return Response(
                self.playlist(self.get(id)),
                media_type="application/vnd.apple.mpegurl",
                headers={"Cache-Control": "no-store"},
            )

        @router.get("/api/vod/segment/{name}")
        async def segment(name: str, id: str):
            job = self.get(id)
            match = re.fullmatch(r"segment_(\d{5,8})\.ts", name)
            if not match:
                raise HTTPException(400, "Invalid segment name")
            return await self.segment(job, int(match[1]))

        return router
