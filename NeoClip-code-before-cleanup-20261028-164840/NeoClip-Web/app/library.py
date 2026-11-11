"""A shared, persistent library index that never scans in an HTTP request."""

import asyncio
import json
import logging
import sys
import time
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)


class VideoLibrary:
    def __init__(self, root, cache_path, interval=5, timeout=5):
        self.root = root
        self.cache_path = cache_path
        self.interval = interval
        self.timeout = timeout
        self.files = {}
        self.task = None
        self.process = None
        self.status = "scanning"
        self.location = "."
        self.unavailable = []
        try:
            cached = json.loads(cache_path.read_text())
            if cached.get("root") == str(root):
                for item in cached.get("files", []):
                    path = Path(item["path"])
                    if path.is_absolute() or ".." in path.parts:
                        continue
                    if (isinstance(item.get("name"), str)
                            and isinstance(item.get("size_bytes"), int)
                            and isinstance(item.get("modified_ns"), str)):
                        self.files[item["path"]] = item
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    def snapshot(self):
        return {
            "files": sorted(self.files.values(), key=lambda item: item["path"].lower()),
            "scan": {
                "status": self.status,
                "folder": self.location,
                "unavailable": list(self.unavailable),
            },
        }

    async def _spawn(self, directory):
        return await asyncio.create_subprocess_exec(
            sys.executable, "-u", str(Path(__file__).with_name("library_scan.py")),
            str(self.root), directory,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )

    async def _stop_process(self):
        process = self.process
        if process is None:
            return
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except asyncio.TimeoutError:
            # A kernel-blocked NAS read can resist termination. Keep its handle
            # and do not spawn replacements until it actually exits.
            return
        self.process = None

    async def _directory(self, directory, seen, folders):
        self.location = directory
        self.process = await self._spawn(directory)
        complete = False
        deadline = time.monotonic() + max(30, self.timeout)
        try:
            while True:
                remaining = min(self.timeout, deadline - time.monotonic())
                if remaining <= 0:
                    raise asyncio.TimeoutError
                line = await asyncio.wait_for(self.process.stdout.readline(), remaining)
                if not line:
                    break
                record = json.loads(line)
                if "file" in record:
                    item = record["file"]
                    self.files[item["path"]] = item
                    seen.add(item["path"])
                elif "directory" in record:
                    folders.append(record["directory"])
                elif "unavailable" in record:
                    self.unavailable.append(record["unavailable"])
                elif "checking" in record:
                    self.location = record["checking"]
                elif record.get("complete"):
                    complete = True
            await asyncio.wait_for(self.process.wait(), self.timeout)
            complete = complete and self.process.returncode == 0
        except (asyncio.TimeoutError, OSError, ValueError):
            complete = False
        finally:
            await self._stop_process()
        if not complete:
            self.unavailable.append(directory)
            log.warning("Video library directory did not finish: %s (last entry: %s)",
                        directory, self.location)

    async def scan_once(self):
        if self.process is not None:
            if self.process.returncode is None:
                self.status = "unavailable" if not self.files else "partial"
                return
            self.process = None
        self.status = "scanning"
        self.unavailable = []
        seen = set()
        folders = deque(["."])
        while folders:
            directory = folders.popleft()
            await self._directory(directory, seen, folders)
            if self.process is not None:
                # Preserve the unvisited tree if the OS has not released the read.
                self.unavailable.append(".")
                break
        # Remove missing files only from portions of the tree actually read.
        def failed(path):
            return any(prefix == "." or path == prefix or path.startswith(prefix + "/")
                       for prefix in self.unavailable)

        self.files = {path: item for path, item in self.files.items()
                      if path in seen or failed(path)}
        self.unavailable = list(dict.fromkeys(self.unavailable))
        self.status = ("partial" if self.files else "unavailable") if self.unavailable else "ready"
        self.location = ""
        content = json.dumps({"root": str(self.root), "files": list(self.files.values())})
        # The cache is local /data, never the NAS. A failed cache write must not
        # discard a usable listing. Finish the atomic write before cancellation.
        writing = asyncio.create_task(asyncio.to_thread(self._write, content))
        try:
            await asyncio.shield(writing)
        except asyncio.CancelledError:
            await writing
            raise

    def _write(self, content):
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_suffix(".tmp")
            temporary.write_text(content)
            temporary.replace(self.cache_path)
        except OSError:
            log.warning("Could not save video library cache", exc_info=True)

    async def _run(self):
        while True:
            try:
                await self.scan_once()
            except Exception:  # noqa: BLE001 -- keep the last listing during outages
                self.status = "partial" if self.files else "unavailable"
                log.exception("Video library scan failed")
                await self._stop_process()
            await asyncio.sleep(self.interval)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        await self._stop_process()
