import asyncio
import importlib.util
import json
import sys
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from app import preview_startup, proxies
from app.persistent_proxy import PersistentProxyManager


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    for key in ("MEDIA_ROOT", "DATA_ROOT", "EXPORT_ROOT"):
        path = tmp_path / key
        path.mkdir()
        monkeypatch.setenv(key, str(path))
    monkeypatch.setenv("NEOCLIP_QUEUE_FILE", str(tmp_path / "queue.json"))
    spec = importlib.util.spec_from_file_location(
        "app.startup_test", Path(__file__).parents[1] / "app/main.py"
    )
    main = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, main)
    spec.loader.exec_module(main)
    (main.MEDIA_ROOT / "sample.mp4").write_bytes(b"video")
    monkeypatch.setattr(preview_startup, "STARTUP_TIMEOUT_SECONDS", 0.04)
    return main


def client(main):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    )


@pytest.mark.parametrize(
    "endpoint", ["/api/source", "/api/proxy/start", "/api/proxy/queue"]
)
def test_stalled_request_returns_retryable_json_and_releases_locks(
    app_module, monkeypatch, endpoint
):
    main = app_module

    async def stuck(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(main.proxies, "start", stuck)
    monkeypatch.setattr(main.proxies, "enqueue", stuck)

    async def run():
        async with client(main) as api:
            response = await asyncio.wait_for(
                api.post(endpoint, json={"path": "sample.mp4"}), 1
            )
            assert response.status_code == 503
            assert response.headers["retry-after"] == "5"
            assert "took too long" in response.json()["detail"]
            assert not main.media_change_lock.locked()

    asyncio.run(run())


def test_lock_wait_is_bounded_and_next_selection_preserves_edits(
    app_module, monkeypatch
):
    main = app_module

    async def ready(path, start):
        return {"id": "ready", "duration": 60, "start_seconds": 0}

    monkeypatch.setattr(main.proxies, "start", ready)

    async def run():
        async with client(main) as api:
            await api.post(
                "/api/session",
                json={
                    "path": "sample.mp4",
                    "current_time": 12,
                    "mark_in": 3,
                    "mark_out": 8,
                    "clip_name": "keep",
                },
            )
            await api.post(
                "/api/clips",
                json={"path": "sample.mp4", "start_s": 3, "end_s": 8, "name": "keep"},
            )
            before = json.loads(main.QUEUE_FILE.read_text())
            await main.media_change_lock.acquire()
            try:
                response = await asyncio.wait_for(
                    api.post("/api/source", json={"path": "sample.mp4"}), 1
                )
                assert response.status_code == 503
            finally:
                main.media_change_lock.release()
            assert json.loads(main.QUEUE_FILE.read_text()) == before
            response = await api.post("/api/source", json={"path": "sample.mp4"})
            assert response.status_code == 200
            assert response.json()["current_session"]["clip_name"] == "keep"
            assert len(response.json()["clips"]) == 1

    asyncio.run(run())


def test_stalled_path_resolution_does_not_freeze_other_requests(
    app_module, monkeypatch
):
    main = app_module
    entered, release = threading.Event(), threading.Event()

    def resolve(path):
        entered.set()
        release.wait(2)
        return main.MEDIA_ROOT / path

    monkeypatch.setattr(main, "resolve_media_path", resolve)

    async def run():
        async with client(main) as api:
            opening = asyncio.create_task(
                api.post("/api/source", json={"path": "sample.mp4"})
            )
            try:
                await asyncio.to_thread(entered.wait, 1)
                assert entered.is_set()
                state = await asyncio.wait_for(api.get("/api/state"), 0.2)
                assert state.status_code == 200
                response = await asyncio.wait_for(opening, 1)
                assert response.status_code == 503
            finally:
                release.set()
                await opening

    asyncio.run(run())


def test_probe_timeout_is_reported_and_does_not_poison_cache(tmp_path, monkeypatch):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"video")
    manager = PersistentProxyManager(tmp_path / "cache", lambda _: source)

    async def stalled(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(proxies, "capture", stalled)

    async def run():
        with pytest.raises(HTTPException) as caught:
            await manager.probe(source)
        assert caught.value.status_code == 503
        assert "video information timed out after 25 seconds" in caught.value.detail
        assert not manager.probe_cache

        async def working(*args, **kwargs):
            return (
                0,
                json.dumps(
                    {"format": {"duration": "60"}, "streams": [{"codec_type": "video"}]}
                ),
                "",
            )

        monkeypatch.setattr(proxies, "capture", working)
        assert await manager.probe(source) == (60, False)

    asyncio.run(run())


def test_background_retry_keeps_probe_stage_in_error(tmp_path, monkeypatch):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"video")
    manager = PersistentProxyManager(tmp_path / "cache", lambda _: source)

    async def stalled(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(proxies, "capture", stalled)

    async def run():
        try:
            job = await manager.enqueue("sample.mp4")
            async with asyncio.timeout(1):
                while job["build_status"] != "retrying":
                    await asyncio.sleep(0.01)
            assert "video information timed out after 25 seconds" in job["error"]
            assert "45 seconds" not in job["error"]
        finally:
            await manager.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure,expected",
    [(HTTPException(404, "missing"), 404), (OSError("unavailable"), 503)],
)
def test_source_read_failures_are_actionable(
    app_module, monkeypatch, failure, expected
):
    main = app_module

    def resolve(path):
        raise failure

    monkeypatch.setattr(main, "resolve_media_path", resolve)

    async def run():
        async with client(main) as api:
            response = await api.post("/api/source", json={"path": "sample.mp4"})
            assert response.status_code == expected
            assert response.json()["detail"]

    asyncio.run(run())
