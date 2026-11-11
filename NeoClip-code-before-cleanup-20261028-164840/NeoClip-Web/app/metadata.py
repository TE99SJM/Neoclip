"""Background duration lookup; directory scans never wait for FFprobe."""

import asyncio
import json
import math
import time

from .proxies import capture


class VideoMetadata:
    def __init__(self, cache_path, resolve):
        self.cache_path = cache_path
        self.resolve = resolve
        self.cache = {}
        self.requested = {}
        self.pending = set()
        self.queue = asyncio.Queue()
        self.workers = set()
        self.dirty = False
        self.save_lock = asyncio.Lock()
        try:
            data = json.loads(cache_path.read_text())
            if isinstance(data, dict) and data.get("version") == 1:
                self.cache = data.get("files", {})
                if not isinstance(self.cache, dict):
                    self.cache = {}
        except (OSError, ValueError):
            pass

    @staticmethod
    def fingerprint(entry):
        return entry["size_bytes"], entry["modified_ns"]

    def enrich(self, files, root):
        """Return cached values now, and queue missing/changed durations once."""
        results = []
        for item in files:
            key = str(root / item["path"])
            cached = self.cache.get(key, {})
            matches = isinstance(cached, dict) and (
                cached.get("size_bytes"),
                cached.get("modified_ns"),
            ) == self.fingerprint(item)
            duration = cached.get("duration_seconds") if matches else None
            if (
                not isinstance(duration, (int, float))
                or not math.isfinite(duration)
                or duration <= 0
            ):
                duration = None
            retry_after = cached.get("retry_after", 0) if matches else 0
            if not isinstance(retry_after, (int, float)):
                retry_after = 0
            if duration is None and time.time() >= retry_after:
                self.requested[key] = dict(item)
                if key not in self.pending:
                    self.pending.add(key)
                    self.queue.put_nowait(key)
            results.append(
                {
                    **item,
                    "duration_seconds": duration,
                    "duration_pending": duration is None and key in self.pending,
                }
            )
        # At most two probes, including across simultaneous browser requests.
        while not self.queue.empty() and len(self.workers) < 2:
            task = asyncio.create_task(self._worker())
            self.workers.add(task)
            task.add_done_callback(self.workers.discard)
        return results

    def _source(self, item):
        source = self.resolve(item["path"])
        stat = source.stat()
        if (stat.st_size, str(stat.st_mtime_ns)) != self.fingerprint(item):
            raise ValueError("Video changed while reading metadata")
        return source

    async def probe(self, source):
        code, out, _ = await capture(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,duration",
                "-of",
                "json",
                str(source),
            ],
            timeout=10,
        )
        if code:
            return None
        data = json.loads(out)
        videos = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
        if not videos:
            return None
        for value in [
            data.get("format", {}).get("duration"),
            *(s.get("duration") for s in videos),
        ]:
            try:
                duration = float(value)
            except (ValueError, TypeError):
                continue
            if math.isfinite(duration) and duration > 0:
                return duration
        return None

    async def _worker(self):
        completed = 0
        while not self.queue.empty():
            key = self.queue.get_nowait()
            item = self.requested[key]
            try:
                duration = None
                try:
                    source = await asyncio.to_thread(self._source, item)
                    duration = await self.probe(source)
                    await asyncio.to_thread(self._source, item)
                except Exception:  # noqa: BLE001 -- unreadable/partial videos must not break the list
                    duration = None
                # A newer scan may have observed an ongoing transfer meanwhile.
                if self.fingerprint(self.requested[key]) == self.fingerprint(item):
                    self.cache[key] = {
                        "size_bytes": item["size_bytes"],
                        "modified_ns": item["modified_ns"],
                        "duration_seconds": duration,
                        "retry_after": time.time() + 60 if duration is None else 0,
                    }
                    self.dirty = True
                    self.requested.pop(key, None)
            finally:
                self.pending.discard(key)
                self.queue.task_done()
            # Coalesce transfer updates into one follow-up probe per path.
            if key in self.requested:
                self.pending.add(key)
                self.queue.put_nowait(key)
            completed += 1
            if completed % 10 == 0:
                await self.save()
        await self.save()

    def _write(self, content):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(content)
        temporary.replace(self.cache_path)

    async def save(self):
        async with self.save_lock:
            if not self.dirty:
                return
            content = json.dumps({"version": 1, "files": self.cache})
            self.dirty = False
            writing = asyncio.create_task(asyncio.to_thread(self._write, content))
            try:
                await asyncio.shield(writing)
            except asyncio.CancelledError:
                # Keep the lock until an in-flight atomic write has finished.
                # Shutdown must not race another writer for the same temp file.
                try:
                    await writing
                except OSError:
                    self.dirty = True
                raise
            except OSError:
                # Read-only/full cache storage must not hide the media library.
                self.dirty = True

    async def close(self):
        tasks = list(self.workers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.workers.clear()
        self.pending.clear()
        self.requested.clear()
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        await self.save()
