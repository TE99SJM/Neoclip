import asyncio
from pathlib import Path

from app import proxies
from app.proxies import ProxyManager, pipeline_args


def test_cuda_keeps_frames_on_gpu():
    before, after = pipeline_args("cuda")
    assert ["-hwaccel", "cuda"] == before[2:4]
    assert "-hwaccel_output_format" in before
    assert "scale_cuda=-2:360:format=nv12" in after
    assert "-pix_fmt" not in after
    assert "hwdownload" not in ",".join(after)
    assert "h264_nvenc" in after


def test_software_decode_can_still_resize_on_gpu():
    before, after = pipeline_args("upload")
    assert "-hwaccel" not in before
    assert "format=nv12,hwupload_cuda,scale_cuda=-2:360:format=nv12" in after
    assert "h264_nvenc" in after


def test_cpu_fallback_limited_threads():
    before, after = pipeline_args("cpu")
    assert "-threads" in before and "-threads" in after
    assert "libx264" in after


def test_resume_seek_is_before_input(tmp_path):
    manager = ProxyManager(tmp_path, lambda p: Path(p))
    job = {"id": "a" * 32, "start_seconds": 3600}
    args = manager.command(job, Path("input.mkv"), "cuda")
    assert args[args.index("-ss") + 1] == "3600"
    assert args.index("-ss") < args.index("-i")
    assert "-readrate" in args
    assert args.count("-i") == 1


def test_manifest_duration_not_assumed_segment_length(tmp_path):
    manager = ProxyManager(tmp_path, lambda p: Path(p))
    job = {"id": "a" * 32, "start_seconds": 3600, "duration": 4000}
    manager.directory(job).mkdir()
    (manager.directory(job) / "index.m3u8").write_text(
        "#EXTM3U\n#EXTINF:2.000,\nsegment_00000.ts\n#EXTINF:0.700,\nsegment_00001.ts\n"
    )
    info = manager.view(job)
    assert info["available_seconds"] == 2.7
    assert info["available_end"] == 3602.7
    assert info["playable"]
    assert info["segment_count"] == 2


def test_duplicate_resume_reuses_task_and_seek_gets_immutable_id(tmp_path, monkeypatch):
    async def run():
        source = tmp_path / "video.mp4"
        source.write_bytes(b"test")
        manager = ProxyManager(tmp_path / "cache", lambda p: source)

        async def probe(source):
            return 4000, True

        async def encode(job, source):
            try:
                await asyncio.sleep(1000)
            finally:
                job["status"] = "stopped"

        monkeypatch.setattr(manager, "probe", probe)
        monkeypatch.setattr(manager, "encode", encode)
        first = await manager.start("video.mp4", 3600)
        await asyncio.sleep(0)
        same = await manager.start("video.mp4", 3600)
        assert first["id"] == same["id"]
        assert len(manager.tasks) == 1
        earlier = await manager.start("video.mp4", 3500)
        assert earlier["id"] != first["id"]
        assert earlier["start_seconds"] == 3500
        assert len(manager.tasks) == 1
        assert manager.jobs[first["id"]]["status"] == "stopped"
        # Old URLs still refer to the old offset, not the replacement job.
        assert manager.get(first["id"])["start_seconds"] == 3600
        await manager.close()

    asyncio.run(run())


def test_completed_cache_reused_only_when_it_covers_target(tmp_path, monkeypatch):
    async def run():
        source = tmp_path / "video.mp4"
        source.write_bytes(b"test")
        manager = ProxyManager(tmp_path / "cache", lambda p: source)

        async def probe(source):
            return 4000, False

        async def encode(job, source):
            await asyncio.sleep(1000)

        monkeypatch.setattr(manager, "probe", probe)
        monkeypatch.setattr(manager, "encode", encode)
        job = {
            "id": "b" * 32,
            "path": "video.mp4",
            "signature": manager.signature(source),
            "status": "ready",
            "start_seconds": 3600,
            "duration": 4000,
            "last_activity": 0,
        }
        manager.jobs[job["id"]] = job
        manager.directory(job).mkdir()
        (manager.directory(job) / "index.m3u8").write_text(
            "#EXTM3U\n#EXTINF:400,\nsegment_00000.ts\n#EXT-X-ENDLIST\n"
        )
        cached = await manager.start("video.mp4", 3700)
        assert cached["id"] == job["id"]
        assert len(manager.tasks) == 0
        earlier = await manager.start("video.mp4", 3590)
        assert earlier["id"] != job["id"]
        await manager.close()

    asyncio.run(run())


def test_bad_source_does_not_reuse_old_cache(tmp_path, monkeypatch):
    async def run():
        source = tmp_path / "source.mp4"
        source.write_bytes(b"one")
        manager = ProxyManager(tmp_path / "cache", lambda p: source)

        async def probe(source):
            return 100, False

        async def encode(job, source):
            await asyncio.sleep(1000)

        monkeypatch.setattr(manager, "probe", probe)
        monkeypatch.setattr(manager, "encode", encode)
        first = await manager.start("source.mp4", 25)
        source.write_bytes(b"replacement file")
        second = await manager.start("source.mp4", 25)
        assert first["id"] != second["id"]
        await manager.close()

    asyncio.run(run())


def test_strict_gpu_mode_never_silently_uses_cpu(monkeypatch):
    calls = []

    async def failed(args):
        calls.append(args)
        return 1, "", "GPU unavailable"

    monkeypatch.setattr(proxies, "REQUIRE_GPU", True)
    monkeypatch.setattr(proxies, "capture", failed)

    async def run():
        try:
            await proxies.choose_pipeline(Path("video.mp4"), 3600)
        except RuntimeError as error:
            assert "GPU unavailable" in str(error)
        else:
            raise AssertionError("Strict GPU mode accepted a CPU fallback")

    asyncio.run(run())
    assert len(calls) == 1
    assert "cuda" in calls[0]


def test_stopping_a_not_yet_started_task_cannot_leave_it_creating(
    tmp_path, monkeypatch
):
    async def run():
        source = tmp_path / "test.mp4"
        source.write_bytes(b"x")
        manager = ProxyManager(tmp_path / "cache", lambda p: source)

        async def probe(source):
            return 100, False

        async def encode(job, source):
            await asyncio.sleep(1000)

        monkeypatch.setattr(manager, "probe", probe)
        monkeypatch.setattr(manager, "encode", encode)
        first = await manager.start("test.mp4", 50)
        await manager.stop(first["id"])
        assert manager.jobs[first["id"]]["status"] == "stopped"
        second = await manager.start("test.mp4", 50)
        assert second["id"] != first["id"]
        await manager.close()

    asyncio.run(run())
