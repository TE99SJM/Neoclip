"""Same-folder source renames with no overwrite and recoverable app state."""

import asyncio
import ctypes
import errno
import json
import logging
import os
import re
from contextlib import AsyncExitStack
from pathlib import Path, PurePosixPath

from fastapi import HTTPException

from .deletion import VideoChangedError, fingerprint, retry_transient

log = logging.getLogger(__name__)


def renamed_path(path, name):
    source = PurePosixPath(path)
    if (
        source.is_absolute()
        or ".." in source.parts
        or "\\" in path
        or str(source) != path
        or not source.name
    ):
        raise HTTPException(400, "Invalid video path")
    if source.suffix.lower() not in {".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm"}:
        raise HTTPException(400, "Select a supported video file")
    name = name.strip()
    if not name or name in {".", ".."} or name.startswith("."):
        raise HTTPException(400, "Enter a filename")
    if re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', name) or name.endswith("."):
        raise HTTPException(
            400, "Use a filename without folder separators or special characters"
        )
    suffix = source.suffix
    if not Path(name).suffix:
        name += suffix
    if Path(name).suffix.lower() != suffix.lower():
        raise HTTPException(400, f"Keep the original {suffix} file extension")
    if len(name.encode("utf-8")) > 255:
        raise HTTPException(400, "The filename is too long")
    return str(source.with_name(name))


def clipped_name(path):
    source = PurePosixPath(path)
    if re.search(r"(?:^|[ _-])clipped$", source.stem, re.I):
        return source.name
    return source.stem + " clipped" + source.suffix


def rename_noreplace(source, target, expected):
    if fingerprint(source) != tuple(expected):
        raise VideoChangedError(
            "The source video changed; try again after its transfer finishes"
        )
    # Linux renameat2 performs the destination-existence check atomically,
    # including on supported NAS mounts. Never fall back to overwriting rename.
    libc = ctypes.CDLL(None, use_errno=True)
    call = getattr(libc, "renameat2", None)
    if call is None:
        raise OSError(
            errno.ENOTSUP, "This system cannot safely rename without overwriting"
        )
    call.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    call.restype = ctypes.c_int
    if call(-100, os.fsencode(source), -100, os.fsencode(target), 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(target))


class MediaRenamer:
    def __init__(
        self,
        *,
        root,
        data_root,
        state,
        save_state,
        managers,
        library,
        metadata,
        lock,
        busy,
        rendering,
        move_state=None,
    ):
        self.root, self.state, self.save_state = root, state, save_state
        self.managers = list(dict.fromkeys(managers))
        self.library, self.metadata = library, metadata
        self.lock, self.busy, self.rendering = lock, busy, rendering
        self.journal = data_root / "rename_pending.json"
        self.move_state = move_state

    def _source(self, relative):
        path = self.root / relative
        if path.is_symlink():
            raise HTTPException(400, "Rename the original video, not a symbolic link")
        try:
            path.resolve().relative_to(self.root)
        except ValueError as exc:
            raise HTTPException(400, "Invalid video path") from exc
        if not path.is_file():
            raise HTTPException(404, "Video not found")
        return path

    def _commit(self, record):
        old, new = record["old"], record["new"]
        source = self._source(new)
        if fingerprint(source) != tuple(record["fingerprint"]):
            raise VideoChangedError(
                "Renamed video changed before its saved state was updated"
            )
        if self.move_state:
            self.move_state(old, new)
        else:
            queues, sessions = self.state["queues"], self.state["editor_sessions"]
            if old in queues:
                queues.setdefault(new, []).extend(queues.pop(old))
            if old in sessions:
                saved = sessions.pop(old)
                sessions.setdefault(new, saved)
            if self.state["source_path"] == old:
                self.state["source_path"] = new
        changed = []
        for manager in self.managers:
            for job_id in record["proxies"].get(str(manager.root), []):
                job = manager.jobs.get(job_id)
                if job and job["path"] in {old, new}:
                    job.update(path=new, signature=manager.signature(source.resolve()))
                    changed.append((manager, job))
        self.library.rename_entry(old, new, source.stat())
        cache = self.metadata.cache.pop(str(self.root / old), None)
        if cache is not None:
            self.metadata.cache[str(self.root / new)] = cache
            self.metadata.dirty = True
        # Persist edits before optional derived caches. The intent remains until
        # both queue and proxy identities are durable, so restart can finish it.
        self.save_state()
        for manager, job in changed:
            manager.save(job)
        self.journal.unlink(missing_ok=True)

    def recover(self):
        if not self.journal.exists():
            return
        record = json.loads(self.journal.read_text())
        old, new = self.root / record["old"], self.root / record["new"]
        if (
            new.exists()
            and fingerprint(new) == tuple(record["fingerprint"])
            and not old.exists()
        ):
            self._commit(record)
        elif old.exists() and fingerprint(old) == tuple(record["fingerprint"]):
            self.journal.unlink()  # The physical rename did not happen.
        else:
            raise RuntimeError(
                "A previous rename needs review before another rename can run"
            )

    async def rename(self, path, name, *, after_render=False, expected=None):
        # Finish a filesystem operation even if the HTTP client disconnects.
        task = asyncio.create_task(self._rename(path, name, after_render, expected))
        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        return await asyncio.shield(task)

    async def _rename(self, path, name, after_render, expected):
        new = renamed_path(path, name)
        async with self.lock:
            if path in self.busy or new in self.busy:
                raise HTTPException(409, "This video is already being changed")
            if self.rendering(path) and not after_render:
                raise HTTPException(
                    409, "Wait for this video to finish rendering before renaming it"
                )
            self.busy.update((path, new))
            try:
                if self.journal.exists():
                    self.recover()
                source = await asyncio.to_thread(self._source, path)
                if after_render and self.state["queues"].get(path):
                    return {
                        "ok": True,
                        "old_path": path,
                        "path": path,
                        "name": source.name,
                    }
                if new == path:
                    return {
                        "ok": True,
                        "old_path": path,
                        "path": new,
                        "name": source.name,
                    }
                target = source.with_name(PurePosixPath(new).name)
                if new in self.state["queues"] or new in self.state["editor_sessions"]:
                    raise HTTPException(
                        409,
                        "That filename already has saved edits; choose another name",
                    )
                original = await asyncio.to_thread(fingerprint, source)
                if expected is not None and tuple(expected) != original:
                    raise VideoChangedError(
                        "The source changed while its clips were rendering"
                    )
                async with AsyncExitStack() as stack:
                    await stack.enter_async_context(self.metadata.pause(source))
                    for manager in self.managers:
                        if hasattr(manager, "pause_path"):
                            await stack.enter_async_context(manager.pause_path(path))
                        else:
                            await manager.lock.acquire()
                            stack.callback(manager.lock.release)
                            blocked = getattr(manager, "renaming_paths", set())
                            manager.renaming_paths = blocked
                            blocked.add(path)
                            stack.callback(blocked.discard, path)
                            for job in list(manager.jobs.values()):
                                if job["path"] == path:
                                    await manager.stop(job["id"])
                    record = {
                        "old": path,
                        "new": new,
                        "fingerprint": original,
                        "proxies": {},
                    }
                    for manager in self.managers:
                        signature = manager.signature(source.resolve())
                        record["proxies"][str(manager.root)] = [
                            job["id"]
                            for job in manager.jobs.values()
                            if job["path"] == path and job["signature"] == signature
                        ]
                    temp = self.journal.with_suffix(".tmp")
                    temp.write_text(json.dumps(record))
                    temp.replace(self.journal)
                    try:
                        await retry_transient(
                            lambda: rename_noreplace(source, target, original)
                        )
                    except OSError:
                        # A NAS may complete a rename before reporting a transport
                        # error. Reconcile by file identity instead of doing it twice.
                        completed = (
                            not source.exists()
                            and target.is_file()
                            and fingerprint(target) == original
                        )
                        if not completed:
                            if source.is_file() and fingerprint(source) == original:
                                self.journal.unlink(missing_ok=True)
                            raise
                    warning = ""
                    try:
                        self._commit(record)
                    except OSError as exc:
                        warning = "File renamed; saving its updated metadata will retry on restart."
                        log.exception("Could not finish rename metadata: %s", exc)
                    await self.library.save()
                    return {
                        "ok": True,
                        "old_path": path,
                        "path": new,
                        "name": target.name,
                        "warning": warning,
                    }
            except FileExistsError as exc:
                raise HTTPException(
                    409, "A file with that name already exists; choose another name"
                ) from exc
            except VideoChangedError as exc:
                raise HTTPException(409, str(exc)) from exc
            except PermissionError as exc:
                raise HTTPException(
                    409, "The storage denied the rename. Check its write permission."
                ) from exc
            except OSError as exc:
                raise HTTPException(
                    409, f"The file could not be renamed: {exc.strerror or exc}"
                ) from exc
            finally:
                self.busy.difference_update((path, new))
