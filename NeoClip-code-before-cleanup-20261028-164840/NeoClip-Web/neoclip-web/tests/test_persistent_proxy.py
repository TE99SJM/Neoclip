import asyncio
import json
import re
import subprocess

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app import persistent_proxy
from app.library import VideoLibrary
from app.persistent_proxy import RETENTION_SECONDS, PersistentProxyManager


async def wait_until(condition, timeout=10):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def mock_pipeline(monkeypatch, duration=13):
    async def choose(source, start, *, height):
        assert height == 240
        return "cpu", "CPU", []

    async def probe(self, source):
        return duration, False

    monkeypatch.setattr(persistent_proxy, "choose_pipeline", choose)
    monkeypatch.setattr(PersistentProxyManager, "probe", probe)


def fake_encoder(manager, calls, gate=None):
    async def encode(job, index, target):
        try:
            async with manager.workers:
                calls.append((job["path"], index))
                if gate:
                    await gate(job, index)
                await asyncio.sleep(0.01)
                target.write_bytes(b"finished")
                manager.built[job["id"]].add(index)
                manager._completed(job)
                manager.save(job)
        finally:
            manager.tasks.pop((job["id"], index), None)

    manager.encode_segment = encode


def test_every_selection_builds_fully_without_viewers_or_heartbeats(
    tmp_path, monkeypatch
):
    mock_pipeline(monkeypatch)

    async def run():
        for name in ("first.mp4", "second.mp4"):
            (tmp_path / name).write_bytes(b"source")
        manager = PersistentProxyManager(tmp_path / "cache", lambda p: tmp_path / p)
        first_started, release = asyncio.Event(), asyncio.Event()

        async def gate(job, index):
            if job["path"] == "first.mp4" and index == 0:
                first_started.set()
                await release.wait()

        calls = []
        fake_encoder(manager, calls, gate)
        first = await manager.start("first.mp4", 0)
        await first_started.wait()
        second = await manager.start("second.mp4", 0)
        assert manager.build_id == first["id"], (
            "selection must not cancel the first job"
        )
        # A fake idle interval does not stop either job or remove its cached data.
        manager.clock = lambda: first["created_at"] + 600
        await manager.reap_once()
        assert set(manager.jobs) == {first["id"], second["id"]}
        release.set()
        await wait_until(
            lambda: all(j["build_status"] == "complete" for j in manager.jobs.values())
        )
        assert set(calls) == {
            (name, n) for name in ("first.mp4", "second.mp4") for n in range(3)
        }
        assert manager.view(manager.get(first["id"]))["progress"] == 100
        await manager.close()

    asyncio.run(run())


def test_cancelled_selection_request_does_not_cancel_the_job(tmp_path, monkeypatch):
    mock_pipeline(monkeypatch)

    async def run():
        source = tmp_path / "source.mp4"
        source.write_bytes(b"source")
        manager = PersistentProxyManager(tmp_path / "cache", lambda _: source)
        started, release = asyncio.Event(), asyncio.Event()

        async def probe(_):
            started.set()
            await release.wait()
            return 13, False

        manager.probe = probe
        calls = []
        fake_encoder(manager, calls)
        request = asyncio.create_task(manager.start(source.name, 0))
        await started.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        job = next(iter(manager.jobs.values()))
        assert (manager.directory(job) / "job.json").is_file(), (
            "job persisted before request wait"
        )
        release.set()
        await wait_until(lambda: job["build_status"] == "complete")
        assert len(calls) == 3
        await manager.close()

    asyncio.run(run())


def test_restart_resumes_partial_build_and_keeps_finished_sections(
    tmp_path, monkeypatch
):
    mock_pipeline(monkeypatch)

    async def run():
        source = tmp_path / "source.mp4"
        source.write_bytes(b"source")
        manager = PersistentProxyManager(tmp_path / "cache", lambda _: source)
        blocked = asyncio.Event()

        async def gate(job, index):
            if index == 1:
                blocked.set()
                await asyncio.Event().wait()

        fake_encoder(manager, [], gate)
        job = await manager.start(source.name, 0)
        await blocked.wait()
        completed = manager.directory(job) / "segment_00000.ts"
        stamp = completed.stat().st_mtime_ns
        await manager.close()
        assert not manager.tasks and not manager.processes
        restored = PersistentProxyManager(tmp_path / "cache", lambda _: source)
        calls = []
        fake_encoder(restored, calls)
        restored.start_background()  # No browser needed after container restart.
        await wait_until(lambda: restored.jobs[job["id"]]["build_status"] == "complete")
        assert calls == [(source.name, 1), (source.name, 2)]
        assert completed.stat().st_mtime_ns == stamp
        await restored.close()

    asyncio.run(run())


def test_expiration_is_fixed_after_completion_not_renewed_by_viewing(
    tmp_path, monkeypatch
):
    mock_pipeline(monkeypatch)

    async def run():
        source = tmp_path / "source.mp4"
        source.write_bytes(b"source")
        now = [1000]
        manager = PersistentProxyManager(
            tmp_path / "cache", lambda _: source, clock=lambda: now[0]
        )
        fake_encoder(manager, [])
        result = await manager.start(source.name, 0)
        job = manager.jobs[result["id"]]
        await wait_until(lambda: job["build_status"] == "complete")
        expiry = job["completed_at"] + RETENTION_SECONDS
        assert job["expires_at"] == expiry
        now[0] = expiry - 1
        for _ in range(3):
            assert (await manager.start(source.name, 0))["id"] == job["id"]
            manager.get(job["id"])
        assert job["expires_at"] == expiry
        await manager.reap_once()
        assert manager.directory(job).exists()
        now[0] = expiry
        with pytest.raises(HTTPException) as error:
            manager.get(job["id"])
        assert error.value.status_code == 404
        await manager.reap_once()
        assert not manager.jobs and not manager.directory(job).exists()
        fresh = await manager.start(source.name, 0)
        assert fresh["id"] != job["id"]
        await manager.close()

    asyncio.run(run())


def test_failed_source_delete_retains_cache_success_deletes_active_job(
    tmp_path, monkeypatch
):
    mock_pipeline(monkeypatch)

    async def run():
        source = tmp_path / "source.mp4"
        source.write_bytes(b"source")
        manager = PersistentProxyManager(tmp_path / "cache", lambda _: source)
        blocked = asyncio.Event()

        async def gate(job, index):
            if index == 1:
                blocked.set()
                await asyncio.Event().wait()

        fake_encoder(manager, [], gate)
        job = await manager.start(source.name, 0)
        await blocked.wait()
        cached = manager.directory(job) / "segment_00000.ts"
        with pytest.raises(PermissionError):
            async with manager.pause_path(source.name):
                assert not manager.tasks
                raise PermissionError("NAS still busy")
        assert cached.exists() and job["id"] in manager.jobs
        blocked.clear()
        await blocked.wait()  # Unfinished build automatically resumed.
        async with manager.pause_path(source.name):
            source.unlink()
            await manager.delete_path(source.name)
        await asyncio.sleep(0.02)
        assert not manager.jobs and not manager.directory(job).exists()
        assert not manager.tasks and not manager.preparations
        await manager.close()

    asyncio.run(run())


def test_confirmed_external_deletion_removes_proxy_but_outage_keeps_it(
    tmp_path, monkeypatch
):
    mock_pipeline(monkeypatch)

    async def run():
        media = tmp_path / "media"
        media.mkdir()
        source = media / "source.mp4"
        source.write_bytes(b"source")
        manager = PersistentProxyManager(tmp_path / "cache", lambda p: media / p)
        fake_encoder(manager, [])
        job = await manager.start(source.name, 0)
        await wait_until(lambda: manager.jobs[job["id"]]["build_status"] == "complete")
        index = VideoLibrary(media, tmp_path / "library.json")
        index.on_remove = manager.delete_path
        await index.scan_once()
        media.rename(tmp_path / "offline")
        await index.scan_once()
        assert job["id"] in manager.jobs, "unavailable folder is not proof of deletion"
        (tmp_path / "offline").rename(media)
        source.unlink()
        await index.scan_once()
        assert not manager.jobs and not manager.directory(job).exists()
        await manager.close()

    asyncio.run(run())


def test_retry_queue_and_unfinished_deadline_survive_restart(tmp_path, monkeypatch):
    mock_pipeline(monkeypatch)

    async def run():
        source = tmp_path / "source.mp4"
        source.write_bytes(b"source")
        now = [2000]
        manager = PersistentProxyManager(
            tmp_path / "cache", lambda _: source, clock=lambda: now[0]
        )

        async def failed(job, index, target):
            manager.tasks.pop((job["id"], index), None)
            raise OSError("NAS offline")

        manager.encode_segment = failed
        result = await manager.start(source.name, 0)
        job = manager.jobs[result["id"]]
        await wait_until(lambda: job["build_status"] == "retrying")
        assert job["retry_after"] == now[0] + 60
        assert job["completed_at"] is None
        await manager.close()
        now[0] = job["expires_at"]
        restored = PersistentProxyManager(
            tmp_path / "cache", lambda _: source, clock=lambda: now[0]
        )
        assert not restored.jobs and not restored.directory(job).exists()
        await restored.close()

    asyncio.run(run())


@pytest.mark.parametrize("audio", [False, True])
def test_real_full_240p_proxy_build_decode_and_routes_without_playback(
    tmp_path, monkeypatch, audio
):
    source = tmp_path / "real.mkv"
    args = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=640x360:rate=30:duration=13.2",
    ]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=duration=13.2", "-c:a", "aac"]
    subprocess.run(
        [
            *args,
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-preset",
            "ultrafast",
            str(source),
        ],
        check=True,
    )

    async def choose(source, start, *, height):
        assert height == 240
        return "cpu", "CPU", []

    monkeypatch.setattr(persistent_proxy, "choose_pipeline", choose)

    async def run():
        manager = PersistentProxyManager(tmp_path / "cache", lambda _: source)
        job = await manager.enqueue(source.name, 7)
        await wait_until(lambda: job["build_status"] == "complete", timeout=30)
        # No browser/segment requests were made to generate the full video.
        assert len(manager.built[job["id"]]) == 3
        for index in range(3):
            segment = manager.directory(job) / f"segment_{index:05d}.ts"
            info = json.loads(
                subprocess.check_output(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "stream=codec_type,height,duration",
                        "-of",
                        "json",
                        str(segment),
                    ]
                )
            )
            assert (
                next(s for s in info["streams"] if s["codec_type"] == "video")["height"]
                == 240
            )
            assert any(s["codec_type"] == "audio" for s in info["streams"]) == audio
        app = FastAPI()
        app.include_router(manager.router())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            view = manager.view(job)
            assert (await client.get(view["status_url"])).json()["progress"] == 100
            manifest = (await client.get(view["url"])).text
            assert (
                "#EXT-X-ENDLIST" in manifest and "#EXT-X-PLAYLIST-TYPE:VOD" in manifest
            )
            for index in (2, 0, 1):
                assert (
                    await client.get(
                        f"/api/vod/segment/segment_{index:05d}.ts?id={job['id']}"
                    )
                ).status_code == 200
        playlist = manager.directory(job) / "test.m3u8"
        playlist.write_text(
            re.sub(r"segment/(segment_\d+\.ts)\?id=[a-f0-9]+", r"\1", manifest)
        )
        decoded = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(playlist),
                "-map",
                "0:v:0",
                "-fps_mode",
                "passthrough",
                "-progress",
                "pipe:1",
                "-f",
                "null",
                "-",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        frames = re.findall(r"^frame=(\d+)", decoded.stdout, re.M)
        assert abs(int(frames[-1]) / 30 - job["duration"]) <= 1 / 30
        assert not decoded.stderr
        await manager.close()

    asyncio.run(run())


def test_background_and_expiry_workers_recover_from_cache_io_errors(
    tmp_path, monkeypatch
):
    mock_pipeline(monkeypatch)

    async def run():
        source = tmp_path / "source.mp4"
        source.write_bytes(b"source")
        now = [1000]
        manager = PersistentProxyManager(
            tmp_path / "cache", lambda _: source, clock=lambda: now[0]
        )
        original_save = manager.save

        def full_disk(job):
            if job["build_status"] == "retrying":
                raise OSError("cache disk full")
            original_save(job)

        async def failed_encode(job, index, target):
            manager.tasks.pop((job["id"], index), None)
            raise OSError("cache disk full")

        manager.save = full_disk
        manager.encode_segment = failed_encode
        job = await manager.enqueue(source.name)
        await wait_until(lambda: job["build_status"] == "retrying")
        assert not manager.builder.done()
        manager.save = original_save
        fake_encoder(manager, [])
        now[0] += 61
        manager.wake.set()
        await wait_until(lambda: job["build_status"] == "complete")
        now[0] = job["expires_at"]
        original_remove = manager._remove

        async def busy_cache(job):
            raise OSError("cache temporarily busy")

        manager._remove = busy_cache
        await manager.reap_once()
        assert job["id"] in manager.jobs
        manager._remove = original_remove
        await manager.reap_once()
        assert not manager.jobs and not manager.directory(job).exists()
        await manager.close()

    asyncio.run(run())
