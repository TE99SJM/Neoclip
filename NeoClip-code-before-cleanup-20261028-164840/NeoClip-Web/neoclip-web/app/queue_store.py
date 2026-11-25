"""Concurrent clip queues shared by independent NeoClip containers.

The lock lives beside the JSON, so mount its directory, never just the file.
All disk operations are short synchronous transactions; no lock crosses await.
"""
import copy
import fcntl
import hashlib
import json
import os
import tempfile
import threading
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path


def document(value):
    if not isinstance(value, dict):
        raise ValueError("The clip queue must contain a JSON object")
    result = copy.deepcopy(value)
    source = str(result.get("source_path", ""))
    queues = result.get("queues")
    if not isinstance(queues, dict):
        queues = {source: result.get("clips", [])} if source else {}
    result["queues"] = {}
    for path, clips in queues.items():
        if not isinstance(clips, list):
            raise ValueError("Invalid clip list: " + path)
        clean = []
        for index, item in enumerate(clips):
            if not isinstance(item, dict):
                raise ValueError("Invalid clip in " + path)
            clip = dict(item)
            if not clip.get("id"):
                key = json.dumps([path, index, clip], sort_keys=True)
                clip["id"] = hashlib.sha256(key.encode()).hexdigest()[:32]
            clean.append(clip)
        result["queues"][path] = clean
    result.setdefault("editor_sessions", {})
    result.setdefault("path_aliases", {})
    result["source_path"] = source
    result["version"] = 4
    return result


def canonical(path, aliases):
    seen = set()
    while path in aliases and path not in seen:
        seen.add(path)
        path = aliases[path]
    return path


def merge(base, local, latest):
    """Apply only this process's edits; retain changes from other writers."""
    result = copy.deepcopy(latest)
    aliases = result["path_aliases"]
    for path in set(base["queues"]) | set(local["queues"]):
        before = {c["id"]: c for c in base["queues"].get(path, [])}
        after = {c["id"]: c for c in local["queues"].get(path, [])}
        target = canonical(path, aliases)
        current = result["queues"].setdefault(target, [])
        removed = before.keys() - after.keys()
        current[:] = [c for c in current if c["id"] not in removed]
        existing = {c["id"] for c in current}
        for key, clip in after.items():
            # A stale writer cannot resurrect a clip removed elsewhere.
            if key not in before and key not in existing:
                current.append(copy.deepcopy(clip))
    for path, session in local["editor_sessions"].items():
        old = base["editor_sessions"].get(path, {})
        target = canonical(path, aliases)
        current = result["editor_sessions"].setdefault(target, {})
        for key, value in session.items():
            if key not in old or old[key] != value:
                current[key] = copy.deepcopy(value)
    if local["source_path"] != base["source_path"]:
        result["source_path"] = canonical(local["source_path"], aliases)
    return result


class QueueStore:
    def __init__(self, path, state):
        self.path, self.state = Path(path), state
        self.base = document({})
        self.mutex = threading.RLock()

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.mutex, open(str(self.path) + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def read(self):
        try:
            return document(json.loads(self.path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return document({})
        # Corruption/permission errors must not silently erase saved clips.

    def write(self, data):
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def adopt(self, data, *, startup=False):
        selected = data["source_path"] if startup else self.state.get("source_path", "")
        self.base = copy.deepcopy(data)
        self.state["queues"] = copy.deepcopy(data["queues"])
        self.state["editor_sessions"] = copy.deepcopy(data["editor_sessions"])
        # Selecting a video in the test app must not switch the main player.
        self.state["source_path"] = canonical(selected, data["path_aliases"])
        self.base["source_path"] = self.state["source_path"]

    def refresh(self, *, startup=False):
        with self.locked():
            latest = self.read()
            pending = merge(self.base, document(self.state), latest)
            self.adopt(latest, startup=startup)
            if not startup:
                self.state["queues"] = pending["queues"]
                self.state["editor_sessions"] = pending["editor_sessions"]

    def save(self):
        with self.locked():
            result = merge(self.base, document(self.state), self.read())
            self.write(result)
            self.adopt(result)

    def resolve(self, path):
        return canonical(path, self.base["path_aliases"])

    def rename(self, old, new):
        with self.locked():
            result = merge(self.base, document(self.state), self.read())
            if old != new:
                dest = result["queues"].setdefault(new, [])
                ids = {clip["id"] for clip in dest}
                dest.extend(c for c in result["queues"].pop(old, []) if c["id"] not in ids)
                session = result["editor_sessions"].pop(old, None)
                if session is not None:
                    result["editor_sessions"].setdefault(new, session)
                result["path_aliases"][old] = new
                result["source_path"] = canonical(result["source_path"], result["path_aliases"])
            self.write(result)
            self.adopt(result)

    def forget(self, path):
        with self.locked():
            result = merge(self.base, document(self.state), self.read())
            result["queues"].pop(path, None)
            result["editor_sessions"].pop(path, None)
            if result["source_path"] == path:
                result["source_path"] = ""
            self.write(result)
            if self.state["source_path"] == path:
                self.state["source_path"] = ""
            self.adopt(result)

    def acquire_render(self):
        """A shared render lease prevents both apps processing the same queue."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = open(str(self.path) + ".render.lock", "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise
        return lock  # Keep open until render/rename finishes; close releases it.

    def import_queue(self, source):
        """Merge existing test edits once, without duplicating the main queue."""
        raw = Path(source).read_bytes()
        token = hashlib.sha256(raw).hexdigest()
        incoming = document(json.loads(raw))
        with self.locked():
            result = self.read()
            imports = result.setdefault("imports", [])
            if token in imports:
                return
            for path, clips in incoming["queues"].items():
                target = canonical(path, result["path_aliases"])
                dest = result["queues"].setdefault(target, [])
                def content(c):
                    return json.dumps({k: v for k, v in c.items() if k != "id"}, sort_keys=True)
                existing = Counter(content(c) for c in dest)
                incoming_counts = Counter()
                for clip in clips:
                    key = content(clip)
                    incoming_counts[key] += 1
                    if incoming_counts[key] > existing[key]:
                        dest.append({**clip, "id": uuid.uuid4().hex})
            for path, session in incoming["editor_sessions"].items():
                result["editor_sessions"].setdefault(canonical(path, result["path_aliases"]), session)
            imports.append(token)
            self.write(result)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("queue")
    parser.add_argument("--import-from", dest="source")
    args = parser.parse_args()
    store = QueueStore(args.queue, {})
    if args.source and Path(args.source).exists():
        store.import_queue(args.source)
    print("Shared queued clips:", sum(map(len, store.read()["queues"].values())))
