import asyncio
import subprocess

from app.metadata import VideoMetadata


def entry(path):
    stat = path.stat()
    return {
        "name": path.name,
        "path": path.name,
        "size_bytes": stat.st_size,
        "modified_ns": str(stat.st_mtime_ns),
    }


async def finish(metadata):
    await asyncio.gather(*list(metadata.workers))


def test_large_library_returns_immediately_with_bounded_cached_probes(tmp_path):
    async def run():
        paths = [tmp_path / f"video-{n}.mp4" for n in range(485)]
        for path in paths:
            path.write_bytes(b"video")
        files = [entry(path) for path in paths]
        metadata = VideoMetadata(tmp_path / "cache.json", lambda p: tmp_path / p)
        gate = asyncio.Event()
        two_running = asyncio.Event()
        calls = []
        active = 0
        peak = 0

        async def probe(source):
            nonlocal active, peak
            calls.append(source)
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_running.set()
            await gate.wait()
            active -= 1
            return 5025.4

        metadata.probe = probe
        first = metadata.enrich(files, tmp_path)
        assert len(first) == 485
        assert not calls  # Listing did not wait for or run any probes itself.
        assert all(
            f["duration_pending"] and f["duration_seconds"] is None for f in first
        )
        await asyncio.wait_for(two_running.wait(), 3)
        for _ in range(3):
            metadata.enrich(files, tmp_path)
        assert len(metadata.pending) == 485
        assert len(metadata.workers) == 2
        assert len(calls) == 2
        gate.set()
        await finish(metadata)
        result = metadata.enrich(files, tmp_path)
        assert len(calls) == 485 and peak == 2
        assert all(
            f["duration_seconds"] == 5025.4 and not f["duration_pending"]
            for f in result
        )
        await metadata.close()
        restored = VideoMetadata(tmp_path / "cache.json", lambda p: tmp_path / p)
        assert restored.enrich(files, tmp_path) == result
        assert not restored.workers  # Server restart does not reprobe 485 files.
        await restored.close()

    asyncio.run(run())


def test_changes_during_probe_are_coalesced_and_old_duration_is_not_reused(tmp_path):
    async def run():
        path = tmp_path / "transfer.mp4"
        path.write_bytes(b"initial")
        metadata = VideoMetadata(tmp_path / "cache.json", lambda _: path)
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def probe(_):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
                return 5
            return 15

        metadata.probe = probe
        metadata.enrich([entry(path)], tmp_path)
        await started.wait()
        path.write_bytes(b"transfer has grown")
        waiting = metadata.enrich([entry(path)], tmp_path)[0]
        assert waiting["duration_seconds"] is None and waiting["duration_pending"]
        release.set()
        await finish(metadata)
        assert calls == 2
        result = metadata.enrich([entry(path)], tmp_path)[0]
        assert result["duration_seconds"] == 15
        # Same size but changed mtime also invalidates the cache.
        path.touch()
        assert metadata.enrich([entry(path)], tmp_path)[0]["duration_seconds"] is None
        await finish(metadata)
        assert calls == 3
        await metadata.close()

    asyncio.run(run())


def test_bad_or_incomplete_video_retries_without_holding_the_list(tmp_path):
    async def run():
        path = tmp_path / "partial.mp4"
        path.write_bytes(b"incomplete")
        metadata = VideoMetadata(tmp_path / "cache.json", lambda _: path)
        calls = 0

        async def failed(_):
            nonlocal calls
            calls += 1
            raise asyncio.TimeoutError()

        metadata.probe = failed
        metadata.enrich([entry(path)], tmp_path)
        await finish(metadata)
        for _ in range(3):
            result = metadata.enrich([entry(path)], tmp_path)[0]
            assert result["duration_seconds"] is None and not result["duration_pending"]
        assert calls == 1
        metadata.cache[str(path)]["retry_after"] = 0
        assert metadata.enrich([entry(path)], tmp_path)[0]["duration_pending"]
        await finish(metadata)
        assert calls == 2
        await metadata.close()

    asyncio.run(run())


def test_shutdown_cancels_pending_probes(tmp_path):
    async def run():
        path = tmp_path / "source.mp4"
        path.write_bytes(b"source")
        metadata = VideoMetadata(tmp_path / "cache.json", lambda _: path)
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def slow(_):
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                stopped.set()

        metadata.probe = slow
        metadata.enrich([entry(path)], tmp_path)
        await started.wait()
        await metadata.close()
        assert stopped.is_set()
        assert not metadata.workers and not metadata.pending and metadata.queue.empty()

    asyncio.run(run())


def test_real_ffprobe_duration_and_invalid_file(tmp_path):
    async def run():
        path = tmp_path / "short video.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=160x90:rate=30:duration=2.5",
                "-c:v",
                "libx264",
                "-threads",
                "1",
                "-preset",
                "ultrafast",
                str(path),
            ],
            check=True,
        )
        metadata = VideoMetadata(tmp_path / "cache.json", lambda name: tmp_path / name)
        metadata.enrich([entry(path)], tmp_path)
        await finish(metadata)
        result = metadata.enrich([entry(path)], tmp_path)[0]
        assert abs(result["duration_seconds"] - 2.5) < 0.05
        bad = tmp_path / "bad.mp4"
        bad.write_text("not a video")
        metadata.enrich([entry(bad)], tmp_path)
        await finish(metadata)
        assert metadata.enrich([entry(bad)], tmp_path)[0]["duration_seconds"] is None
        await metadata.close()

    asyncio.run(run())


def test_cache_write_failure_does_not_hide_duration(tmp_path):
    async def run():
        path = tmp_path / "source.mp4"
        path.write_bytes(b"video")
        cache = tmp_path / "cache.json"
        cache.mkdir()  # Cannot replace a directory with a metadata file.
        metadata = VideoMetadata(cache, lambda _: path)

        async def probe(_):
            return 12.5

        metadata.probe = probe
        metadata.enrich([entry(path)], tmp_path)
        await finish(metadata)
        assert metadata.enrich([entry(path)], tmp_path)[0]["duration_seconds"] == 12.5
        await metadata.close()

    asyncio.run(run())


def test_delete_pauses_active_and_queued_probes_and_failure_can_resume(tmp_path):
    async def run():
        paths = [tmp_path / f"{n}.mp4" for n in range(4)]
        for path in paths:
            path.write_bytes(b"video")
        metadata = VideoMetadata(tmp_path / "cache.json", lambda p: tmp_path / p)
        files = [entry(path) for path in paths]
        started, released, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []
        async def probe(path):
            calls.append(path)
            if path == paths[0] and not closed.is_set():
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    closed.set()
            elif path == paths[1]:
                await released.wait()
            return 20
        metadata.probe = probe
        metadata.enrich(files, tmp_path)
        await started.wait()
        async with metadata.pause(paths[2]):
            async with metadata.pause(paths[0]):
                assert closed.is_set(), "active probe has closed before deletion"
                for _ in range(3):
                    metadata.enrich(files, tmp_path)
                released.set()
                await finish(metadata)
                assert calls.count(paths[0]) == 1
                assert paths[2] not in calls
                assert paths[3] in calls, "other videos continue probing"
                await metadata.forget(paths[0])
        # Failed deletion releases the hold, so subsequent polling can retry.
        metadata.enrich(files, tmp_path)
        await finish(metadata)
        assert calls.count(paths[0]) == 2 and calls.count(paths[2]) == 1
        assert all(item["duration_seconds"] == 20 for item in metadata.enrich(files, tmp_path))
        await metadata.close()
    asyncio.run(run())
