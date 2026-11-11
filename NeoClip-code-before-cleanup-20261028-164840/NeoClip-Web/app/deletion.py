"""Delete only the selected file, with bounded retries for transient NAS errors."""

import asyncio
import errno


class VideoChangedError(Exception):
    pass


def fingerprint(source):
    stat = source.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


async def retry_transient(operation):
    for delay in (0, .25, .5, 1):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await asyncio.to_thread(operation)
        except OSError as exc:
            if exc.errno not in {errno.EAGAIN, errno.EBUSY} or delay == 1:
                raise


async def remove_video(source, expected):
    def unlink_selected():
        try:
            current = fingerprint(source)
        except FileNotFoundError:
            return
        if current != expected:
            raise VideoChangedError("The video changed while deletion was pending")
        source.unlink()

    def confirm_absent():
        try:
            source.stat()
        except FileNotFoundError:
            return
        # Never unlink again after a successful unlink: a new transfer could
        # have reused this filename, or the NAS may still cache the old entry.
        raise OSError(errno.EBUSY, "The NAS has not confirmed that the file is gone")

    await retry_transient(unlink_selected)
    await retry_transient(confirm_absent)
