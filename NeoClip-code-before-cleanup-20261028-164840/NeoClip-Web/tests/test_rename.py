import asyncio
import errno
import importlib
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app import renaming
from app.deletion import fingerprint
from app.library import VideoLibrary
from app.metadata import VideoMetadata
from app.renaming import clipped_name, rename_noreplace, renamed_path


@pytest.mark.parametrize(
    "path,name,result",
    [
        ("Game/movie.mp4", "New name", "Game/New name.mp4"),
        ("Game/movie.MKV", "New name.MKV", "Game/New name.MKV"),
        ("movie.mp4", "  film clipped.mp4 ", "film clipped.mp4"),
    ],
)
def test_same_folder_and_extension(path, name, result):
    assert renamed_path(path, name) == result


@pytest.mark.parametrize(
    "path,name",
    [
        ("../movie.mp4", "x"),
        ("/movie.mp4", "x"),
        ("a//movie.mp4", "x"),
        ("movie.mp4", "../x.mp4"),
        ("movie.mp4", "x/m.mp4"),
        ("movie.mp4", "x\\m.mp4"),
        ("movie.mp4", "x.txt"),
        ("movie.mp4", ""),
        ("movie.mp4", ".hidden.mp4"),
        ("movie.mp4", "x\x00.mp4"),
        ("notes.txt", "notes2.txt"),
        ("movie.mp4", "x" * 256),
    ],
)
def test_invalid_names_are_rejected(path, name):
    with pytest.raises(HTTPException) as error:
        renamed_path(path, name)
    assert error.value.status_code == 400


def test_clipped_name_is_idempotent():
    assert clipped_name("Folder/movie.mkv") == "movie clipped.mkv"
    for name in ("movie clipped.mkv", "movie_CLIPPED.mp4", "movie-clipped.mov"):
        assert clipped_name(name) == name


def test_atomic_rename_never_overwrites(tmp_path):
    source, target = tmp_path / "source.mp4", tmp_path / "target.mp4"
    source.write_bytes(b"source")
    target.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        rename_noreplace(source, target, fingerprint(source))
    assert source.read_bytes() == b"source" and target.read_bytes() == b"existing"
    target.unlink()
    rename_noreplace(source, target, fingerprint(source))
    assert not source.exists() and target.read_bytes() == b"source"


@pytest.fixture
def application(tmp_path, monkeypatch):
    media, data = tmp_path / "media", tmp_path / "data"
    media.mkdir()
    data.mkdir()
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("DATA_ROOT", str(data))
    monkeypatch.setenv("EXPORT_ROOT", str(tmp_path / "exports"))
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "MEDIA_ROOT", media)
    monkeypatch.setattr(main, "DATA_ROOT", data)
    monkeypatch.setattr(main, "QUEUE_FILE", data / "queue.json")
    manager = type(main.proxies)(data / "proxies", main.resolve_media_path)
    native = (
        manager
        if main.proxies is main.native_proxies
        else type(main.native_proxies)(data / "native", main.resolve_media_path)
    )
    monkeypatch.setattr(main, "proxies", manager)
    monkeypatch.setattr(main, "native_proxies", native)
    monkeypatch.setattr(main, "media_change_lock", asyncio.Lock())
    monkeypatch.setattr(main, "deleting_paths", set())
    monkeypatch.setattr(main, "render_task", None)
    monkeypatch.setattr(main, "render_state", {})
    monkeypatch.setattr(
        main,
        "state",
        {
            "source_path": "selected.mp4",
            "queues": {
                "selected.mp4": [{"name": "keep", "start_s": 2, "end_s": 4}],
                "other.mp4": [{"name": "other"}],
            },
            "editor_sessions": {
                "selected.mp4": {
                    "mark_in": 2,
                    "mark_out": 4,
                    "current_time": 29,
                    "clip_name": "draft",
                }
            },
        },
    )
    monkeypatch.setattr(
        main,
        "video_metadata",
        VideoMetadata(data / "metadata.json", main.resolve_media_path),
    )
    monkeypatch.setattr(
        main, "library_index", VideoLibrary(media, data / "library.json")
    )
    source = media / "selected.mp4"
    source.write_bytes(b"original video")
    main.library_index.rename_entry("unused", source.name, source.stat())
    main.save_queue()
    return main, source


def seed_proxy(manager, source):
    job = dict(
        id="a" * 32,
        path=source.name,
        signature=manager.signature(source),
        status="ready",
        height=getattr(manager, "height", 360),
        start_seconds=0,
        resume_seconds=29,
        duration=60,
        audio=False,
        mode="cpu",
        pipeline="CPU",
        build_status="complete",
        completed_at=123,
        expires_at=9999999999,
        created_at=100,
        last_activity=100,
        error="",
        message="Ready",
    )
    manager.jobs[job["id"]] = job
    manager.save(job)
    (manager.directory(job) / "segment_00000.ts").write_bytes(b"cached frames")
    if hasattr(manager, "built"):
        manager.built[job["id"]] = {0}
    return job


async def rename_http(main, name):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        return await client.post(
            "/api/media/rename", json={"path": "selected.mp4", "name": name}
        )


def test_manual_rename_preserves_edits_proxy_and_fixed_deadline(application):
    main, source = application
    jobs = [
        (m, seed_proxy(m, source))
        for m in dict.fromkeys([main.proxies, main.native_proxies])
    ]
    main.video_metadata.cache[str(source)] = {"duration": 60}
    response = asyncio.run(rename_http(main, "New title"))
    assert response.status_code == 200, response.text
    new = "New title.mp4"
    assert (
        not source.exists() and (source.parent / new).read_bytes() == b"original video"
    )
    assert main.state["source_path"] == new
    assert main.state["queues"][new][0]["name"] == "keep"
    assert [c["name"] for c in main.state["queues"]["other.mp4"]] == ["other"]
    assert main.state["editor_sessions"][new]["current_time"] == 29
    assert main.state["editor_sessions"][new]["clip_name"] == "draft"
    assert (
        source.name not in main.library_index.files and new in main.library_index.files
    )
    assert main.video_metadata.cache[str(source.parent / new)]["duration"] == 60
    for manager, job in jobs:
        assert job["path"] == new and job["id"] == "a" * 32
        assert job["expires_at"] == 9999999999 and job["completed_at"] == 123
        assert job["signature"] == manager.signature(source.parent / new)
        assert (
            manager.directory(job) / "segment_00000.ts"
        ).read_bytes() == b"cached frames"
        assert (
            json.loads((manager.directory(job) / "job.json").read_text())["path"] == new
        )
    assert json.loads(main.QUEUE_FILE.read_text())["source_path"] == new
    assert not main.deleting_paths and not main.media_renamer().journal.exists()


@pytest.mark.parametrize("failure", ["collision", "permission", "transient"])
def test_failed_rename_keeps_original_and_edits(application, monkeypatch, failure):
    main, source = application
    before = json.loads(json.dumps(main.state))
    if failure == "collision":
        (source.parent / "taken.mp4").write_bytes(b"other video")
    else:
        code = errno.EACCES if failure == "permission" else errno.EAGAIN

        def fail(*args):
            raise OSError(code, "NAS busy")

        monkeypatch.setattr(renaming, "rename_noreplace", fail)
    response = asyncio.run(rename_http(main, "taken.mp4"))
    assert response.status_code == 409, response.text
    assert source.read_bytes() == b"original video" and main.state == before
    assert not main.deleting_paths
    if failure == "collision":
        assert (source.parent / "taken.mp4").read_bytes() == b"other video"


def test_in_progress_render_blocks_manual_rename(application):
    main, source = application

    async def run():
        main.render_state["source_path"] = source.name
        main.render_task = asyncio.create_task(asyncio.Event().wait())
        try:
            response = await rename_http(main, "new.mp4")
            assert response.status_code == 409 and source.exists()
        finally:
            main.render_task.cancel()
            await asyncio.gather(main.render_task, return_exceptions=True)

    asyncio.run(run())


def test_restart_finishes_state_migration_after_file_rename(application, monkeypatch):
    main, source = application
    job = seed_proxy(main.proxies, source)
    original_save = main.save_queue

    def fail():
        raise OSError(errno.ENOSPC, "Data disk full")

    monkeypatch.setattr(main, "save_queue", fail)
    result = asyncio.run(rename_http(main, "Renamed.mp4"))
    assert result.status_code == 200 and result.json()["warning"]
    assert not source.exists() and main.media_renamer().journal.exists()
    # Restore the on-disk state as a new process would, then replay the intent.
    old = json.loads(main.QUEUE_FILE.read_text())
    main.state.update(
        source_path=old["source_path"],
        queues=old["queues"],
        editor_sessions=old["editor_sessions"],
    )
    job["path"] = source.name
    monkeypatch.setattr(main, "save_queue", original_save)
    main.media_renamer().recover()
    assert main.state["source_path"] == "Renamed.mp4"
    assert len(main.state["queues"]["Renamed.mp4"]) == 1
    assert job["path"] == "Renamed.mp4" and not main.media_renamer().journal.exists()


def test_lost_nas_reply_reconciles_completed_rename(application, monkeypatch):
    main, source = application
    real = renaming.rename_noreplace

    def interrupted(*args):
        real(*args)
        raise OSError(errno.EIO, "Lost NAS reply")

    monkeypatch.setattr(renaming, "rename_noreplace", interrupted)
    response = asyncio.run(rename_http(main, "new.mp4"))
    assert response.status_code == 200, response.text
    assert main.state["source_path"] == "new.mp4" and not source.exists()


@pytest.mark.parametrize("mode", ["individual", "reel", "both"])
def test_complete_render_appends_clipped_once(application, monkeypatch, mode):
    main, source = application
    monkeypatch.setattr(main.proxies, "probe", AsyncMock(return_value=(60, False)))
    monkeypatch.setattr(
        main, "choose_pipeline", AsyncMock(return_value=("cpu", "CPU", []))
    )
    individual, reel = AsyncMock(), AsyncMock()
    monkeypatch.setattr(main, "render_individual", individual)
    monkeypatch.setattr(main, "render_reel", reel)
    clips = list(main.state["queues"][source.name])
    asyncio.run(main.render_job(mode, "test", source.name, clips))
    assert main.render_state["status"] == "done", main.render_state
    assert main.render_state["renamed_to"] == "selected clipped.mp4"
    assert (source.parent / "selected clipped.mp4").exists() and not source.exists()
    assert main.state["queues"]["selected clipped.mp4"] == []
    assert [c["name"] for c in main.state["queues"]["other.mp4"]] == ["other"]
    assert individual.await_count == (mode in {"individual", "both"})
    assert reel.await_count == (mode in {"reel", "both"})
    assert clipped_name(main.render_state["renamed_to"]) == "selected clipped.mp4"


@pytest.mark.parametrize("outcome", ["failure", "cancel", "more_clips", "collision"])
def test_auto_rename_only_after_all_clips_succeed(application, monkeypatch, outcome):
    main, source = application
    monkeypatch.setattr(main.proxies, "probe", AsyncMock(return_value=(60, False)))
    monkeypatch.setattr(
        main, "choose_pipeline", AsyncMock(return_value=("cpu", "CPU", []))
    )

    async def render(*args):
        if outcome == "failure":
            raise RuntimeError("Encoder failed")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        if outcome == "more_clips":
            main.state["queues"][source.name].append(
                {"name": "new", "start_s": 10, "end_s": 15}
            )

    monkeypatch.setattr(main, "render_individual", render)
    if outcome == "collision":
        (source.parent / "selected clipped.mp4").write_bytes(b"keep this")

    async def run():
        try:
            await main.render_job(
                "individual",
                "test",
                source.name,
                list(main.state["queues"][source.name]),
            )
        except asyncio.CancelledError:
            assert outcome == "cancel"

    asyncio.run(run())
    assert source.exists() and main.state["source_path"] == source.name
    assert not main.render_state.get("renamed_to")
    if outcome in {"failure", "cancel"}:
        assert main.state["queues"][source.name][0]["name"] == "keep"
    elif outcome == "more_clips":
        assert main.state["queues"][source.name][0]["name"] == "new"
    else:
        assert (
            main.render_state["status"] == "done"
            and "already exists" in main.render_state["rename_warning"]
        )
        assert (source.parent / "selected clipped.mp4").read_bytes() == b"keep this"


def test_rename_survives_scan_already_in_flight(application, monkeypatch):
    main, source = application

    async def run():
        library = main.library_index
        ready, finish = asyncio.Event(), asyncio.Event()

        async def scan(directory, seen, folders):
            ready.set()
            await finish.wait()

        monkeypatch.setattr(library, "_directory", scan)
        scan_task = asyncio.create_task(library.scan_once())
        await ready.wait()
        assert (await rename_http(main, "new.mp4")).status_code == 200
        finish.set()
        await scan_task
        assert "new.mp4" in library.files and source.name not in library.files

    asyncio.run(run())


def test_partial_both_render_does_not_mark_source(application, monkeypatch):
    main, source = application
    monkeypatch.setattr(main.proxies, "probe", AsyncMock(return_value=(60, False)))
    monkeypatch.setattr(
        main, "choose_pipeline", AsyncMock(return_value=("cpu", "CPU", []))
    )
    monkeypatch.setattr(main, "render_individual", AsyncMock())
    monkeypatch.setattr(
        main, "render_reel", AsyncMock(side_effect=RuntimeError("reel failed"))
    )
    asyncio.run(
        main.render_job(
            "both", "test", source.name, list(main.state["queues"][source.name])
        )
    )
    assert main.render_state["status"] == "error"
    assert source.exists() and main.state["queues"][source.name]


def test_disconnected_client_does_not_leave_half_rename(application, monkeypatch):
    main, source = application

    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        retry = renaming.retry_transient

        async def delayed(operation):
            started.set()
            await release.wait()
            return await retry(operation)

        monkeypatch.setattr(renaming, "retry_transient", delayed)
        request = asyncio.create_task(
            main.media_renamer().rename(source.name, "new.mp4")
        )
        await started.wait()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        release.set()
        async with asyncio.timeout(3):
            while main.deleting_paths:
                await asyncio.sleep(0.01)
        assert main.state["source_path"] == "new.mp4"
        assert not source.exists() and not main.media_renamer().journal.exists()

    asyncio.run(run())


def test_unfinished_240_proxy_continues_under_new_name(application, monkeypatch):
    main, source = application
    manager = main.proxies
    if not hasattr(manager, "start_background"):
        pytest.skip("Durable background generation is specific to the 240p branch")

    async def run():
        job = seed_proxy(manager, source)
        job.update(
            build_status="building",
            duration=13,
            resume_seconds=0,
            completed_at=None,
            retry_after=0,
        )
        started = asyncio.Event()
        calls = []

        async def encode(job, index, target):
            try:
                calls.append((job["path"], index))
                if job["path"] == "selected.mp4":
                    started.set()
                    await asyncio.Event().wait()
                target.write_bytes(b"new cached frames")
                manager.built[job["id"]].add(index)
                manager._completed(job)
                manager.save(job)
            finally:
                manager.tasks.pop((job["id"], index), None)

        monkeypatch.setattr(manager, "encode_segment", encode)
        manager.start_background()
        try:
            await asyncio.wait_for(started.wait(), 3)
            response = await rename_http(main, "new.mp4")
            assert response.status_code == 200, response.text
            async with asyncio.timeout(3):
                while job["build_status"] != "complete":
                    await asyncio.sleep(0.01)
            assert job["id"] == "a" * 32 and job["path"] == "new.mp4"
            assert ("new.mp4", 1) in calls and ("new.mp4", 2) in calls
            assert (
                manager.directory(job) / "segment_00000.ts"
            ).read_bytes() == b"cached frames"
        finally:
            await manager.close()

    asyncio.run(run())
