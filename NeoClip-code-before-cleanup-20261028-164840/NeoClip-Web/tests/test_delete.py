import asyncio
import errno
import importlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app.deletion import VideoChangedError, fingerprint, remove_video
from app.library import VideoLibrary
from app.metadata import VideoMetadata


@pytest.fixture
def application(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("EXPORT_ROOT", str(tmp_path / "exports"))
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "MEDIA_ROOT", media)
    monkeypatch.setattr(main, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(main, "media_change_lock", asyncio.Lock())
    monkeypatch.setattr(main, "deleting_paths", set())
    monkeypatch.setattr(main, "render_task", None)
    monkeypatch.setattr(main, "render_state", {})
    monkeypatch.setattr(main, "state", {
        "source_path": "selected.mp4",
        "queues": {"selected.mp4": [{"name": "keep", "start_s": 2, "end_s": 4}],
                   "other.mp4": [{"name": "other"}]},
        "editor_sessions": {"selected.mp4": {"mark_in": 2, "mark_out": 4}},
    })
    monkeypatch.setattr(main, "video_metadata", VideoMetadata(
        tmp_path / "metadata.json", main.resolve_media_path))
    monkeypatch.setattr(main, "library_index", VideoLibrary(media, tmp_path / "library.json"))
    monkeypatch.setattr(main.proxies, "delete_path", AsyncMock())
    monkeypatch.setattr(main.native_proxies, "delete_path", AsyncMock())
    source = media / "selected.mp4"
    source.write_bytes(b"original video")
    return main, source


def test_busy_delete_waits_for_render_and_retries_then_clears_only_selected(application, monkeypatch):
    main, source = application
    unlink = Path.unlink
    attempts = []
    closed = asyncio.Event()

    def busy_then_delete(path, *args, **kwargs):
        if path == source:
            attempts.append(path)
            assert closed.is_set(), "render cleanup must finish before unlink"
            assert str(source) in main.video_metadata.held
            if len(attempts) < 3:
                raise BlockingIOError(errno.EAGAIN, "temporary NAS error")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", busy_then_delete)

    async def run():
        await main.library_index.scan_once()
        async def render():
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(.01)
                closed.set()
        main.render_state["source_path"] = source.name
        main.render_task = asyncio.create_task(render())
        await asyncio.sleep(0)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            response = await client.post("/api/media/delete", json={"path": source.name})
        assert response.status_code == 200, response.text
        assert response.json()["exists"] is False
        assert len(attempts) == 3 and not source.exists()
        assert main.state["queues"] == {"other.mp4": [{"name": "other"}]}
        assert not main.state["editor_sessions"] and main.state["source_path"] == ""
        assert main.library_index.files == {}
        assert json.loads(main.library_index.cache_path.read_text())["files"] == []
        assert not main.deleting_paths and not main.video_metadata.held
        main.proxies.delete_path.assert_awaited_once_with(source.name)
        main.native_proxies.delete_path.assert_awaited_once_with(source.name)
    asyncio.run(run())


@pytest.mark.parametrize("error,attempts", [(errno.EAGAIN, 4), (errno.EACCES, 1)])
def test_failed_delete_retains_source_queue_marks_and_listing(application, monkeypatch, error, attempts):
    main, source = application
    unlink = Path.unlink
    calls = []
    before = json.loads(json.dumps(main.state))
    def locked(path, *args, **kwargs):
        if path == source:
            calls.append(path)
            raise OSError(error, "NAS denied delete")
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", locked)
    async def run():
        await main.library_index.scan_once()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            result = await client.post("/api/media/delete", json={"path": source.name})
        assert result.status_code == 409, result.text
        assert len(calls) == attempts
        assert source.exists() and main.state == before
        assert source.name in main.library_index.files
        assert not main.deleting_paths and not main.video_metadata.held
    asyncio.run(run())


def test_retry_will_not_delete_a_replaced_file(tmp_path, monkeypatch):
    source = tmp_path / "selected.mp4"
    source.write_bytes(b"original")
    expected = fingerprint(source)
    calls = []
    def replace_before_retry(path):
        calls.append(path)
        source.write_bytes(b"a new transfer with the same filename")
        raise BlockingIOError(errno.EAGAIN, "temporary NAS error")
    monkeypatch.setattr(Path, "unlink", replace_before_retry)
    with pytest.raises(VideoChangedError):
        asyncio.run(remove_video(source, expected))
    assert len(calls) == 1 and source.read_bytes().startswith(b"a new transfer")


def test_post_unlink_confirmation_never_deletes_new_transfer(tmp_path, monkeypatch):
    source = tmp_path / "selected.mp4"
    source.write_bytes(b"original")
    expected = fingerprint(source)
    unlink = Path.unlink
    calls = []
    def replace_after_unlink(path):
        calls.append(path)
        unlink(path)
        path.write_bytes(b"replacement")
    monkeypatch.setattr(Path, "unlink", replace_after_unlink)
    with pytest.raises(OSError, match="not confirmed"):
        asyncio.run(remove_video(source, expected))
    assert len(calls) == 1 and source.read_bytes() == b"replacement"


def test_inflight_preview_finishes_before_delete_and_new_work_is_rejected(application, monkeypatch):
    main, source = application
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def startup(path, requested):
            started.set()
            await release.wait()
            assert source.exists()
            return {"id": "preview"}
        monkeypatch.setattr(main.proxies, "start", startup)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            preview = asyncio.create_task(client.post("/api/proxy/start", json={"path": source.name}))
            await started.wait()
            deletion = asyncio.create_task(client.post("/api/media/delete", json={"path": source.name}))
            await asyncio.sleep(0)
            assert source.name in main.deleting_paths and not deletion.done()
            for endpoint in ("/api/source", "/api/proxy/start", "/api/media/delete"):
                response = await client.post(endpoint, json={"path": source.name})
                assert response.status_code == 409
            assert (await client.post("/api/render", json={})).status_code == 409
            assert (await client.get("/api/media", params={"path": source.name})).status_code == 409
            release.set()
            assert (await preview).status_code == 200
            assert (await deletion).status_code == 200
        assert not source.exists()
    asyncio.run(run())
