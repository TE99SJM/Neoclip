"""240p VOD previews with a durable whole-video queue and fixed retention."""

import asyncio
import json
import logging
import math
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import HTTPException

from .proxies import choose_pipeline
from .vod import SEGMENT_SECONDS, VodProxyManager

log = logging.getLogger(__name__)
RETENTION_SECONDS = 14 * 24 * 60 * 60


class PersistentProxyManager(VodProxyManager):
    height = 240
    cache_name = "240-v1"

    def __init__(self, root, resolve, clock=time.time):
        # Do not run the legacy initializer: it purges unfinished/idle jobs.
        self.root = root / self.cache_name
        self.root.mkdir(parents=True, exist_ok=True)
        self.resolve = resolve
        self.clock = clock
        self.jobs = {}
        self.tasks = {}
        self.processes = {}
        self.preparations = {}
        self.built = {}
        self.lock = asyncio.Lock()
        self.workers = asyncio.Semaphore(2)
        self.probe_cache = {}
        self.reaper = None
        self.builder = None
        self.build_task = None
        self.build_id = None
        self.wake = asyncio.Event()
        self.held = set()
        self.closing = False
        for meta in sorted(self.root.glob("*/job.json")):
            try:
                job = json.loads(meta.read_text())
                if (
                    not re.fullmatch(r"[a-f0-9]{32}", job["id"])
                    or job["id"] != meta.parent.name
                    or job["height"] != 240
                    or not isinstance(job["path"], str)
                    or not math.isfinite(job["expires_at"])
                ):
                    continue
                if job["expires_at"] <= self.clock():
                    shutil.rmtree(meta.parent)
                    continue
                job.pop("deleting", None)
                self.jobs[job["id"]] = job
                self.built[job["id"]] = {
                    int(p.stem.split("_")[-1])
                    for p in meta.parent.glob("segment_*.ts")
                    if re.fullmatch(r"segment_\d{5,8}", p.stem) and p.stat().st_size > 0
                }
                for partial in meta.parent.glob("segment_*.tmp"):
                    partial.unlink(missing_ok=True)
                if len(self.built[job["id"]]) < self.count(job):
                    job["build_status"] = "queued"
            except (OSError, ValueError, TypeError, KeyError):
                log.warning("Could not restore 240p proxy %s", meta, exc_info=True)

    @staticmethod
    def count(job):
        return math.ceil(job.get("duration", 0) / SEGMENT_SECONDS)

    def start_background(self):
        self.closing = False
        if self.builder is None or self.builder.done():
            self.builder = asyncio.create_task(self._background())
        self.wake.set()

    def view(self, job):
        view = super().view(job)
        count = self.count(job)
        progress = (
            min(100, round(100 * len(self.built.get(job["id"], ())) / count))
            if count
            else 0
        )
        view.update(
            progress=progress,
            height=240,
            message=(
                "240p proxy ready"
                if job.get("build_status") == "complete"
                else f"Building full 240p proxy: {progress}% — continues in background"
            ),
        )
        return view

    def get(self, job_id):
        job = self.jobs.get(job_id)
        if not job or job.get("deleting") or job["expires_at"] <= self.clock():
            raise HTTPException(404, "Proxy expired; select the video again")
        # Viewing never extends the fixed retention deadline.
        job["last_activity"] = self.clock()
        return job

    async def enqueue(self, path, requested=0):
        async with self.lock:
            if path in self.held or self.closing:
                raise HTTPException(409, "This video is being removed")
            source = await asyncio.to_thread(self.resolve, path)
            signature = await asyncio.to_thread(self.signature, source)
            for job in list(self.jobs.values()):
                if job["path"] != path:
                    continue
                if (
                    not job.get("deleting")
                    and job["signature"] == signature
                    and job["expires_at"] > self.clock()
                ):
                    self.start_background()
                    return job
                # A replaced source must never reuse the previous video's cache.
                await self._remove(job)
            now = self.clock()
            job = dict(
                id=uuid.uuid4().hex,
                path=path,
                signature=signature,
                height=240,
                start_seconds=0,
                resume_seconds=max(0, requested),
                duration=0,
                audio=False,
                mode=None,
                pipeline="",
                status="creating",
                build_status="queued",
                error="",
                message="Preparing 240p proxy",
                created_at=now,
                completed_at=None,
                expires_at=now + RETENTION_SECONDS,
                last_activity=now,
                retry_after=0,
            )
            self.jobs[job["id"]] = job
            self.built[job["id"]] = set()
            self.save(job)  # Persist before any probe/encoding or response wait.
            self.start_background()
            return job

    async def start(self, path, requested):
        job = await self.enqueue(path, requested)
        await self.prepare(job)
        return self.view(job)

    async def prepare(self, job):
        if job["status"] == "ready":
            return
        task = self.preparations.get(job["id"])
        if task is None:
            task = asyncio.create_task(self._prepare(job))
            self.preparations[job["id"]] = task
            task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled() and not asyncio.current_task().cancelling():
                raise HTTPException(409, "Proxy preparation stopped") from None
            raise

    async def _prepare(self, job):
        try:
            source = await asyncio.to_thread(self.resolve, job["path"])
            if await asyncio.to_thread(self.signature, source) != job["signature"]:
                raise HTTPException(409, "Video changed; select it again")
            async with self.workers:
                duration, audio = await self.probe(source)
                start = (
                    job["resume_seconds"]
                    if job["resume_seconds"] < duration - 0.25
                    else 0
                )
                mode, label, failures = await choose_pipeline(source, start, height=240)
            job.update(
                duration=duration,
                audio=audio,
                mode=mode,
                pipeline=label,
                resume_seconds=start,
                fallback_reason=" | ".join(failures),
                status="ready",
                error="",
                retry_after=0,
            )
            self.save(job)
        finally:
            self.preparations.pop(job["id"], None)

    async def encode_segment(self, job, index, target):
        await super().encode_segment(job, index, target)
        if self.jobs.get(job["id"]) is job and not job.get("deleting"):
            self.built[job["id"]].add(index)
            self._completed(job)
            self.save(job)

    def _completed(self, job):
        if self.count(job) and len(self.built[job["id"]]) >= self.count(job):
            if job["completed_at"] is None:
                job["completed_at"] = self.clock()
                job["expires_at"] = job["completed_at"] + RETENTION_SECONDS
            job.update(build_status="complete", error="", retry_after=0)

    async def segment(self, job, index):
        if job.get("deleting") or job["path"] in self.held or self.closing:
            raise HTTPException(409, "This proxy is being removed")
        if job["expires_at"] <= self.clock():
            raise HTTPException(404, "Proxy expired; select the video again")
        return await super().segment(job, index)

    async def _build(self, job):
        await self.prepare(job)
        job["build_status"] = "building"
        self.save(job)
        # Prepare the saved playhead first, then every section from the beginning.
        resume = min(int(job["resume_seconds"] / SEGMENT_SECONDS), self.count(job) - 1)
        for index in [resume, *range(self.count(job))]:
            if index in self.built[job["id"]]:
                continue
            await self.segment(job, index)
        self._completed(job)
        self.save(job)

    async def _background(self):
        while not self.closing:
            self.wake.clear()
            job = next(
                (
                    j
                    for j in sorted(self.jobs.values(), key=lambda j: j["created_at"])
                    if j["build_status"] != "complete"
                    and not j.get("deleting")
                    and j["path"] not in self.held
                    and j["expires_at"] > self.clock()
                    and j.get("retry_after", 0) <= self.clock()
                ),
                None,
            )
            if job is None:
                try:
                    await asyncio.wait_for(self.wake.wait(), 10)
                except asyncio.TimeoutError:
                    pass
                continue
            self.build_id = job["id"]
            self.build_task = asyncio.create_task(self._build(job))
            try:
                await self.build_task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except Exception as exc:  # noqa: BLE001 -- retry NAS/GPU outages without losing the queue
                job.update(
                    build_status="retrying",
                    retry_after=self.clock() + 60,
                    # The HTTP wrapper is generic; retain the encoder's cause.
                    error=(
                        "FFmpeg timed out after 45 seconds"
                        if isinstance(exc.__cause__, asyncio.TimeoutError)
                        else str(
                            getattr(
                                exc.__cause__ or exc, "detail", exc.__cause__ or exc
                            )
                        ).strip()
                        or "FFmpeg produced no preview data"
                    ),
                )
                try:
                    self.save(job)
                except OSError:
                    log.warning("Could not persist 240p retry state", exc_info=True)
                log.warning("240p build will retry %s: %s", job["path"], job["error"])
            finally:
                self.build_id = None
                self.build_task = None

    async def stop(self, job_id):
        if self.build_id == job_id and self.build_task:
            self.build_task.cancel()
            await asyncio.gather(self.build_task, return_exceptions=True)
        task = self.preparations.get(job_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await super().stop(job_id)

    @asynccontextmanager
    async def pause_path(self, path):
        # A failed source unlink keeps the cache and resumes its unfinished build.
        added = False
        try:
            async with self.lock:
                if path in self.held:
                    raise HTTPException(409, "This video is already being removed")
                self.held.add(path)
                added = True
                for job in list(self.jobs.values()):
                    if job["path"] == path:
                        await self.stop(job["id"])
            yield
        finally:
            if added:
                self.held.discard(path)
                self.wake.set()

    async def _remove(self, job):
        job["deleting"] = True
        await self.stop(job["id"])
        try:
            await asyncio.to_thread(shutil.rmtree, self.directory(job))
        except FileNotFoundError:
            pass
        self.jobs.pop(job["id"], None)
        self.built.pop(job["id"], None)

    async def delete_path(self, path):
        async with self.lock:
            for job in list(self.jobs.values()):
                if job["path"] == path:
                    await self._remove(job)

    async def reap_once(self):
        async with self.lock:
            for job in list(self.jobs.values()):
                if job["expires_at"] <= self.clock():
                    try:
                        await self._remove(job)
                    except OSError:
                        log.warning(
                            "Could not remove expired 240p proxy; retrying",
                            exc_info=True,
                        )

    async def reap(self):
        while True:
            await self.reap_once()
            await asyncio.sleep(30)

    async def close(self):
        self.closing = True
        for task in (self.builder, self.reaper):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        for job_id in list(self.jobs):
            await self.stop(job_id)
        self.builder = self.reaper = None
