"""Read one directory in an isolated process, streaming results as JSON lines."""

import json
import os
import stat
import sys
from pathlib import Path
from urllib.parse import quote

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm"}


def scan_directory(root, relative):
    # The caller supplies directories discovered beneath root, never user input.
    try:
        (root / relative).resolve().relative_to(root)
    except ValueError:
        return
    with os.scandir(root / relative) as entries:
        for entry in entries:
            if entry.name.startswith("."):
                continue
            path = Path(entry.path)
            name = path.relative_to(root).as_posix()
            yield {"checking": name}
            try:
                if entry.is_dir(follow_symlinks=False):
                    yield {"directory": name}
                    continue
                if path.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                if entry.is_symlink():
                    path.resolve().relative_to(root)
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_size < 1:
                    continue
            except (FileNotFoundError, ValueError):
                continue
            except OSError:
                yield {"unavailable": name}
                continue
            yield {"file": {
                "name": entry.name,
                "path": name,
                "original_url": f"/api/media?path={quote(name)}",
                "size_bytes": info.st_size,
                "modified_ns": str(info.st_mtime_ns),
            }}


if __name__ == "__main__":
    try:
        for record in scan_directory(Path(sys.argv[1]), sys.argv[2]):
            print(json.dumps(record), flush=True)
        print(json.dumps({"complete": True}), flush=True)
    except OSError:
        print(json.dumps({"unavailable": sys.argv[2]}), flush=True)
        sys.exit(1)
