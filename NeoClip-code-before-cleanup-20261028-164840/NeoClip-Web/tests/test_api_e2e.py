"""Real FFmpeg integration, including audio, source seeking and both exports."""

import importlib
import json
import subprocess
import time

from fastapi.testclient import TestClient


def test_real_ffmpeg_resume_and_exports(tmp_path, monkeypatch):
    media = tmp_path / "media"
    exports = tmp_path / "exports"
    data = tmp_path / "data"
    media.mkdir()
    exports.mkdir()
    data.mkdir()
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("EXPORT_ROOT", str(exports))
    monkeypatch.setenv("DATA_ROOT", str(data))
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
            "testsrc2=size=320x180:rate=30:duration=12",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=12",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-threads",
            "2",
            "-g",
            "60",
            "-c:a",
            "aac",
            "-shortest",
            str(media / "sample.mp4"),
        ],
        check=True,
    )
    main = importlib.import_module("app.main")
    with TestClient(main.app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/manifest.webmanifest").json()["display"] == "standalone"
        assert client.get("/sw.js").status_code == 200
        assert client.get("/static/icon-192.png").status_code == 200
        assert client.post("/api/render", json={}).status_code == 400
        assert (
            client.post("/api/source", json={"path": "../etc/passwd"}).status_code
            == 400
        )
        assert client.get("/api/proxy/status?id=invalid").status_code == 404
        client.post("/api/session", json={"path": "sample.mp4", "current_time": 6})
        source = client.post("/api/source", json={"path": "sample.mp4"}).json()
        job = source["proxy"]
        assert job["start_seconds"] == 6
        assert source["current_session"]["current_time"] == 6
        deadline = time.time() + 25
        while time.time() < deadline:
            job = client.get("/api/proxy/status", params={"id": job["id"]}).json()
            if job["status"] == "error":
                raise AssertionError(job["error"])
            if job["status"] == "ready":
                break
            time.sleep(0.1)
        assert job["status"] == "ready"
        assert job["playable"] and 5.8 < job["available_seconds"] < 6.2
        text = client.get(job["url"]).text
        assert f"id={job['id']}" in text
        assert "TIME-OFFSET=0" in text
        segment = client.get(f"/api/proxy/segment/segment_00000.ts?id={job['id']}")
        assert segment.status_code == 200 and len(segment.content) > 1000
        assert (
            client.get(f"/api/proxy/segment/job.json?id={job['id']}").status_code == 400
        )
        same = client.post(
            "/api/proxy/start", json={"path": "sample.mp4", "current_time": 8}
        ).json()
        assert same["id"] == job["id"]
        # Going earlier starts an independent offset window.
        earlier = client.post(
            "/api/proxy/start", json={"path": "sample.mp4", "current_time": 2}
        ).json()
        assert earlier["start_seconds"] == 2
        assert earlier["id"] != job["id"]
        for start, end, name in [(6, 7, "A's clip"), (8, 9.5, "B")]:
            response = client.post(
                "/api/clips", json={"name": name, "start_s": start, "end_s": end}
            )
            assert response.status_code == 200
        assert (
            client.post(
                "/api/render", json={"mode": "both", "prefix": "test"}
            ).status_code
            == 200
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            status = client.get("/api/render/status").json()
            if status["status"] == "error":
                raise AssertionError(status["error"])
            if status["status"] == "done":
                break
            time.sleep(0.1)
        assert status["status"] == "done"
        files = sorted(exports.glob("sample/*.mp4"))
        assert len(files) == 3
        for path in files:
            info = json.loads(
                subprocess.check_output(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration:stream=codec_type,width,height",
                        "-of",
                        "json",
                        str(path),
                    ]
                )
            )
            video = next(s for s in info["streams"] if s["codec_type"] == "video")
            assert (video["width"], video["height"]) == (320, 180)
            assert any(s["codec_type"] == "audio" for s in info["streams"])
            if path.name.endswith("_reel.mp4"):
                assert 2.4 < float(info["format"]["duration"]) < 2.8
        assert client.get("/api/state").json()["clips"] == []
        assert (data / "neoclip_queue.json").exists()
        # Cancel and reap are exercised by TestClient lifespan shutdown.
    assert not main.proxies.processes
