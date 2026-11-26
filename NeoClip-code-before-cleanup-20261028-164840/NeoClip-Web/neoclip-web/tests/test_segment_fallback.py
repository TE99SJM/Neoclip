import asyncio
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import vod
from app.vod import VodProxyManager


def manager_job(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    manager = VodProxyManager(tmp_path / "cache", lambda _: source)
    job = dict(
        id="a" * 32,
        path="source.mkv",
        mode="cuda",
        duration=12,
        signature=manager.signature(source),
        status="ready",
        last_activity=0,
    )
    manager.directory(job).mkdir()
    manager.jobs[job["id"]] = job
    return manager, job


def mock_encoder(monkeypatch, outcomes):
    commands = []

    class Process:
        def __init__(self, args, outcome):
            self.args, self.outcome = args, outcome
            self.returncode = 1 if isinstance(outcome, str) else 0

        async def communicate(self):
            if isinstance(self.outcome, str):
                return b"", self.outcome.encode()
            Path(self.args[-1]).write_bytes(b"video" if self.outcome else b"audio-only")
            return f"frame={self.outcome}\nprogress=end\n".encode(), b""

    async def spawn(*args, **kwargs):
        commands.append(args)
        return Process(args, outcomes.pop(0))

    monkeypatch.setattr(vod.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(vod, "REQUIRE_GPU", False)
    return commands


@pytest.mark.parametrize(
    "failures,expected",
    [
        (["hw_frames_ctx must be set when using GPU frames as input"], "nvenc"),
        (["hw_frames_ctx must be set", "OpenEncodeSessionEx failed"], "cpu"),
    ],
)
def test_fallback_retries_same_section_and_persists(
    tmp_path, monkeypatch, failures, expected
):
    async def run():
        manager, job = manager_job(tmp_path)
        commands = mock_encoder(monkeypatch, [*failures, 180, 180])
        target = manager.directory(job) / "segment_00001.ts"
        await manager.encode_segment(job, 1, target)
        assert target.read_bytes() == b"video"
        assert job["mode"] == expected and not job["error"]
        assert json.loads((target.parent / "job.json").read_text())["mode"] == expected
        for args in commands:
            assert args[args.index("-ss") + 1] == "6"
        assert "-hwaccel" not in commands[-1]
        await manager.encode_segment(job, 0, target.parent / "segment_00000.ts")
        assert "-hwaccel" not in commands[-1], (
            "later sections reuse successful fallback"
        )
        assert not manager.processes
        assert not list(target.parent.glob("*.tmp"))

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure", ["No space left on device", "Permission denied", "Input/output error"]
)
def test_storage_errors_do_not_launch_extra_encoders(tmp_path, monkeypatch, failure):
    async def run():
        manager, job = manager_job(tmp_path)
        commands = mock_encoder(monkeypatch, [failure])
        with pytest.raises(HTTPException):
            await manager.encode_segment(
                job, 1, manager.directory(job) / "segment_00001.ts"
            )
        assert len(commands) == 1

    asyncio.run(run())


def test_gpu_only_policy_and_empty_video_are_not_bypassed(tmp_path, monkeypatch):
    async def run():
        manager, job = manager_job(tmp_path)
        commands = mock_encoder(monkeypatch, ["hw_frames_ctx must be set"])
        monkeypatch.setattr(vod, "REQUIRE_GPU", True)
        target = manager.directory(job) / "segment_00001.ts"
        with pytest.raises(HTTPException):
            await manager.encode_segment(job, 1, target)
        assert len(commands) == 1 and not target.exists()
        mock_encoder(monkeypatch, [0])
        with pytest.raises(HTTPException):
            await manager.encode_segment(job, 1, target)
        assert "No video frames" in job["error"]
        assert not target.exists(), (
            "audio-only output must not masquerade as a playable section"
        )

    asyncio.run(run())
