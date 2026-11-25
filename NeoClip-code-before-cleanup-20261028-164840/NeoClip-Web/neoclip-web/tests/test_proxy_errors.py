import asyncio
import json

import pytest
from fastapi import HTTPException

from app import vod
from app.vod import VodProxyManager


def fake_process(monkeypatch, failure):
    class Process:
        returncode = 1

        async def communicate(self):
            if failure == "timeout":
                self.returncode = None
                raise TimeoutError
            return b"", failure.encode()

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def spawn(*args, **kwargs):
        return Process()

    monkeypatch.setattr(vod.asyncio, "create_subprocess_exec", spawn)


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("No space left on device", "No space left on device"),
        ("Cannot load libcuda.so.1", "Cannot load libcuda.so.1"),
        ("timeout", "FFmpeg timed out after 45 seconds"),
        ("", "FFmpeg produced no preview data"),
    ],
)
def test_segment_logs_underlying_failure_and_position(
    tmp_path, monkeypatch, caplog, failure, expected
):
    async def run():
        source = tmp_path / "video.mp4"
        source.write_bytes(b"source")
        manager = VodProxyManager(tmp_path / "cache", lambda _: source)
        job = dict(
            id="a" * 32,
            path="video.mp4",
            mode="cpu",
            duration=12,
            signature=manager.signature(source),
        )
        manager.directory(job).mkdir()
        fake_process(monkeypatch, failure)
        with pytest.raises(HTTPException) as error:
            await manager.encode_segment(
                job, 1, manager.directory(job) / "segment_00001.ts"
            )
        assert error.value.status_code == 503
        assert job["error"] == expected
        assert job["last_failure"]["message"] == expected
        assert job["last_failure"]["start_seconds"] == 6
        assert job["last_failure"]["segment"] == 1
        assert job["last_failure"]["mode"] == "cpu"
        assert expected in caplog.text
        assert "segment=1 time=6s" in caplog.text
        assert not manager.processes
        assert not list(manager.directory(job).glob("*.tmp"))
        await manager.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("No space left on device", "No space left on device"),
        ("timeout", "FFmpeg timed out after 45 seconds"),
    ],
)
def test_background_retry_persists_cause(
    tmp_path, monkeypatch, caplog, failure, expected
):
    persistent = pytest.importorskip("app.persistent_proxy")

    async def run():
        source = tmp_path / "video.mp4"
        source.write_bytes(b"source")
        manager = persistent.PersistentProxyManager(
            tmp_path / "cache", lambda _: source
        )

        async def probe(_):
            return 12, False

        async def pipeline(*args, **kwargs):
            return "cpu", "CPU", []

        monkeypatch.setattr(manager, "probe", probe)
        monkeypatch.setattr(persistent, "choose_pipeline", pipeline)
        fake_process(monkeypatch, failure)
        result = await manager.start("video.mp4", 6)
        job = manager.jobs[result["id"]]
        async with asyncio.timeout(3):
            while job["build_status"] != "retrying":
                await asyncio.sleep(0.01)
        saved = json.loads((manager.directory(job) / "job.json").read_text())
        assert saved["error"] == expected
        assert saved["last_failure"]["message"] == expected
        assert saved["last_failure"]["segment"] == 1
        assert expected in caplog.text
        assert "240p build will retry video.mp4" in caplog.text
        await manager.close()

    asyncio.run(run())
