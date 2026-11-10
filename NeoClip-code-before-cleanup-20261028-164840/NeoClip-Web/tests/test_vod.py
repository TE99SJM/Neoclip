"""Native HLS must expose a complete, immutable source timeline immediately."""

import asyncio
import json
import math
import re
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import vod
from app.vod import VodProxyManager


def test_full_timeline_without_preparing_whole_recording(tmp_path, monkeypatch):
    async def run():
        source = tmp_path / "recording.mkv"
        source.write_bytes(b"source")
        manager = VodProxyManager(tmp_path / "cache", lambda _: source)

        async def probe(_):
            return 7392.25, True

        async def choose(*_):
            return "cpu", "CPU", []

        monkeypatch.setattr(manager, "probe", probe)
        monkeypatch.setattr(vod, "choose_pipeline", choose)
        job = await manager.start("recording.mkv", 3600)
        assert job["start_seconds"] == 0
        assert job["available_end"] == 7392.25
        assert job["playable"]
        assert not manager.tasks and not manager.processes
        playlist = manager.playlist(job)
        lengths = [float(n) for n in re.findall(r"#EXTINF:([\d.]+)", playlist)]
        assert math.isclose(sum(lengths), 7392.25)
        assert lengths[-1] == 0.25
        assert playlist.endswith("#EXT-X-ENDLIST\n")
        assert "#EXT-X-PLAYLIST-TYPE:VOD" in playlist
        assert "TIME-OFFSET=3600.000000" in playlist
        assert len(list(manager.directory(job).glob("*.ts"))) == 0
        again = await manager.start("recording.mkv", 0)
        assert again["id"] == job["id"]
        assert manager.playlist(again) == playlist  # No rewriting on resume.
        command = manager.segment_command(job, source, 600, Path("out.ts"))
        assert command.index("-ss") < command.index("-i")
        assert command[command.index("-ss") + 1] == "3600"
        assert command[command.index("-t") + 1] == "6"
        assert "-readrate" not in command
        source.write_bytes(b"replaced source")
        new = await manager.start("recording.mkv", 0)
        assert new["id"] != job["id"]
        with pytest.raises(HTTPException) as error:
            await manager.segment(job, 0)
        assert error.value.status_code == 409
        await manager.close()

    asyncio.run(run())


def test_real_random_access_audio_silent_restart_and_routes(tmp_path, monkeypatch):
    # CPU is deterministic in CI; hardware pipeline selection is tested separately.
    async def choose(*_):
        return "cpu", "CPU", []

    monkeypatch.setattr(vod, "choose_pipeline", choose)
    for audio in (True, False):
        source = tmp_path / f"sample-{audio}.mkv"
        args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=30:duration=13.2",
        ]
        if audio:
            args += [
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=13.2",
                "-c:a",
                "aac",
            ]
        args += [
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-preset",
            "ultrafast",
            "-g",
            "60",
            str(source),
        ]
        subprocess.run(args, check=True)
        manager = VodProxyManager(tmp_path / f"cache-{audio}", lambda _: source)
        job = asyncio.run(manager.start(source.name, 7))
        app = FastAPI()
        app.include_router(manager.router())
        with TestClient(app) as client:
            assert client.get(job["status_url"]).json()["delivery"] == "vod"
            manifest = client.get(job["url"])
            assert manifest.status_code == 200
            assert "application/vnd.apple.mpegurl" in manifest.headers["content-type"]
            urls = [
                line
                for line in manifest.text.splitlines()
                if line and not line.startswith("#")
            ]
            assert len(urls) == 3
            assert (
                client.get(
                    "/api/vod/segment/job.json", params={"id": job["id"]}
                ).status_code
                == 400
            )
            assert (
                client.get(
                    "/api/vod/segment/segment_00003.ts", params={"id": job["id"]}
                ).status_code
                == 404
            )
            # Seek near EOF first, then return to the first second, then middle.
            for index in (2, 0, 1):
                response = client.get("/api/vod/" + urls[index])
                assert response.status_code == 200
                segment = manager.directory(job) / f"segment_{index:05d}.ts"
                info = json.loads(
                    subprocess.check_output(
                        [
                            "ffprobe",
                            "-v",
                            "error",
                            "-show_entries",
                            "stream=codec_type,start_time,duration:format=duration",
                            "-of",
                            "json",
                            str(segment),
                        ]
                    )
                )
                video = next(s for s in info["streams"] if s["codec_type"] == "video")
                assert 0 <= float(video["start_time"]) < 0.1
                expected = min(6, job["duration"] - index * 6)
                assert abs(float(video["duration"]) - expected) < 0.08
                assert any(s["codec_type"] == "audio" for s in info["streams"]) == audio
                # Every section decodes independently, including the short tail.
                subprocess.run(
                    ["ffmpeg", "-v", "error", "-i", str(segment), "-f", "null", "-"],
                    check=True,
                )
            assert client.get(job["url"]).text == manifest.text
            local_playlist = manager.directory(job) / "playback.m3u8"
            local_playlist.write_text(
                re.sub(r"segment/(segment_\d+\.ts)\?id=[a-f0-9]+", r"\1", manifest.text)
            )
            # Demux the entire VOD, including transitions between sections.
            playback = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    str(local_playlist),
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
            frames = re.findall(r"^frame=(\d+)", playback.stdout, re.M)
            # Container/audio padding may extend the last video frame at 30fps.
            assert abs(int(frames[-1]) / 30 - job["duration"]) <= 1 / 30
            assert not playback.stderr
            # No re-encode for replay and repeated native range requests.
            cached = manager.directory(job) / "segment_00000.ts"
            modified = cached.stat().st_mtime_ns
            assert client.get("/api/vod/" + urls[0]).status_code == 200
            assert cached.stat().st_mtime_ns == modified
        asyncio.run(manager.close())
        restored = VodProxyManager(tmp_path / f"cache-{audio}", lambda _: source)
        resumed = asyncio.run(restored.start(source.name, 1))
        assert resumed["id"] == job["id"]
        assert restored.playlist(resumed) == manifest.text
        asyncio.run(restored.delete_path(source.name))
        assert not restored.jobs and not restored.directory(resumed).exists()


def test_shared_requests_and_cancel_leave_no_partial_files(tmp_path, monkeypatch):
    async def run():
        source = tmp_path / "source.mp4"
        source.write_bytes(b"source")
        manager = VodProxyManager(tmp_path / "cache", lambda _: source)
        job = {
            "id": "a" * 32,
            "duration": 12,
            "path": source.name,
            "signature": manager.signature(source),
        }
        manager.jobs[job["id"]] = job
        manager.directory(job).mkdir()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def encode(job, index, target):
            calls.append(index)
            started.set()
            try:
                await release.wait()
                target.write_bytes(b"complete segment")
            finally:
                manager.tasks.pop((job["id"], index), None)

        monkeypatch.setattr(manager, "encode_segment", encode)
        first = asyncio.create_task(manager.segment(job, 0))
        await started.wait()
        second = asyncio.create_task(manager.segment(job, 0))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        assert calls == [0]
        release.clear()
        started.clear()
        pending = asyncio.create_task(manager.segment(job, 1))
        await started.wait()
        await manager.stop(job["id"])
        with pytest.raises(HTTPException) as error:
            await pending
        assert error.value.status_code == 503
        assert not manager.tasks
        assert not (manager.directory(job) / "segment_00001.ts").exists()
        await manager.close()

    asyncio.run(run())
