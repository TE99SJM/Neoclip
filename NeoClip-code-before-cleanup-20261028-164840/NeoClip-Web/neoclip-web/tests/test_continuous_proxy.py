"""Real-media continuity plus durable build lifecycle tests."""

import asyncio
import json
import re
import subprocess

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import persistent_proxy
from app.continuous_proxy import ContinuousProxyManager
from app.persistent_proxy import RETENTION_SECONDS


async def wait_until(check):
    async with asyncio.timeout(30):
        while not check():
            await asyncio.sleep(.02)


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True)


def source_file(root, audio=True, duration=13.2, rate=30, audio_duration=None):
    source = root / 'source.mkv'
    args = ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            f'testsrc2=size=320x180:rate={rate}:duration={duration}']
    if audio:
        args += ['-f', 'lavfi', '-i', f'sine=frequency=440:sample_rate=44100:duration={audio_duration or duration}', '-c:a', 'aac']
    run([*args, '-c:v', 'libx264', '-threads', '2', '-preset', 'ultrafast', str(source)])
    return source


async def cpu(*args, **kwargs):
    return 'cpu', 'CPU', []


def packets(path):
    return json.loads(run(['ffprobe', '-v', 'error', '-show_packets', '-show_entries',
                          'packet=codec_type,pts_time,duration_time,flags', '-of', 'json', str(path)]).stdout)['packets']


def assert_transport_continuity(paths):
    previous = {}
    for path in paths:
        data = path.read_bytes()
        assert len(data) % 188 == 0
        for i in range(0, len(data), 188):
            packet = data[i:i+188]
            assert packet[0] == 0x47
            pid = ((packet[1] & 31) << 8) | packet[2]
            if pid == 8191:
                continue
            payload = bool(packet[3] & 16)
            counter = packet[3] & 15
            if pid in previous:
                assert counter == (previous[pid] + payload) % 16, (path, pid)
            previous[pid] = counter


@pytest.mark.parametrize('audio,duration,rate,audio_duration', [(True,13.2,30,None), (False,13.2,30,None), (True,6,30,None), (True,6.01,25,None), (False,.2,30,None), (True,6,30,20), (True,13.2,30,2)])
def test_real_continuous_audio_video_and_routes(tmp_path, monkeypatch, audio, duration, rate, audio_duration):
    monkeypatch.setattr(persistent_proxy, 'choose_pipeline', cpu)
    source = source_file(tmp_path, audio, duration, rate, audio_duration)

    async def build():
        manager = ContinuousProxyManager(tmp_path/'cache', lambda _: source)
        view = await manager.start(source.name, 7)
        job = manager.jobs[view['id']]
        await wait_until(lambda: job['build_status'] in ('complete', 'retrying'))
        assert job['build_status'] == 'complete', job.get('error')
        assert abs(job['duration'] - duration) < .15
        assert manager.view(job)['playable']
        assert job['expires_at'] == job['completed_at'] + RETENTION_SECONDS
        await manager.close()
        return manager, job

    manager, job = asyncio.run(build())
    paths = sorted(manager.generation_dir(job).glob('*.ts'))
    assert len(paths) == manager.count(job)
    assert_transport_continuity(paths)
    previous = {}
    for path in paths:
        data = packets(path)
        assert 'K' in next(p for p in data if p['codec_type'] == 'video')['flags']
        for kind in ('video', 'audio') if audio else ('video',):
            stream = [p for p in data if p['codec_type'] == kind]
            start = float(stream[0]['pts_time'])
            if kind in previous:
                assert abs(start - previous[kind]) < .00001, (kind, start, previous[kind])
            previous[kind] = float(stream[-1]['pts_time']) + float(stream[-1]['duration_time'])
    manifest = manager.playlist(job)
    assert [float(n) for n in re.findall(r'#EXTINF:([\d.]+)', manifest)] == job['segment_lengths']
    assert 'DISCONTINUITY' not in manifest and 'PLAYLIST-TYPE:VOD' in manifest
    local = manager.generation_dir(job)/'play.m3u8'
    local.write_text(re.sub(r'segment/(segment_\d+\.ts)\?.*', r'\1', manifest))
    decoded = run(['ffmpeg', '-v', 'error', '-i', str(local), '-f', 'null', '-'])
    assert not decoded.stderr
    # Cached playback is independent of NAS resolution, including after restart.
    def offline(_):
        raise OSError('NAS offline')
    restored = ContinuousProxyManager(tmp_path/'cache', offline)
    app = FastAPI()
    app.include_router(restored.router())
    with TestClient(app) as client:
        view = client.get(f"/api/vod/status?id={job['id']}").json()
        assert view['build_status'] == 'complete' and view['progress'] == 100
        assert client.get(view['url']).text == manifest
        for index in (len(paths)-1, 0, min(1,len(paths)-1), 0):
            response = client.get(f"/api/vod/segment/segment_{index:05d}.ts", params={'id':job['id'], 'generation':job['generation']})
            assert response.status_code == 200
        assert client.get('/api/vod/playlist.m3u8', params={'id':job['id'], 'generation':'old'}).status_code == 410
    asyncio.run(restored.close())
    # Missing completed cache data must queue a rebuild rather than crash or
    # claim a complete preview after a filesystem interruption.
    paths[0].unlink()
    damaged = ContinuousProxyManager(tmp_path/'cache', offline)
    assert damaged.jobs[job['id']]['build_status'] == 'queued'
    assert not damaged.view(damaged.jobs[job['id']])['playable']
    asyncio.run(damaged.close())


def test_shutdown_kills_a_running_encoder_without_publishing_partial_data(tmp_path, monkeypatch):
    monkeypatch.setattr(persistent_proxy, 'choose_pipeline', cpu)
    source = source_file(tmp_path)

    async def exercise():
        manager = ContinuousProxyManager(tmp_path/'cache', lambda _:source)
        command = manager.continuous_command
        def slow(job, src, directory):
            args = command(job, src, directory)
            args.insert(args.index('-i'), '-re')
            return args
        manager.continuous_command = slow
        result = await manager.start(source.name, 0)
        await wait_until(lambda: bool(manager.processes))
        process = next(iter(manager.processes.values()))
        await manager.close()
        assert process.returncode is not None and not manager.processes
        restored = ContinuousProxyManager(tmp_path/'cache', lambda _:source)
        assert not restored.view(restored.jobs[result['id']])['playable']
        await restored.close()

    asyncio.run(exercise())


def mock_build(monkeypatch, duration=12):
    monkeypatch.setattr(persistent_proxy, 'choose_pipeline', cpu)
    async def probe(self, source):
        return duration, True
    monkeypatch.setattr(ContinuousProxyManager, 'probe', probe)


def publish(manager, job, count):
    directory = manager.generation_dir(job)
    lines = ['#EXTM3U']
    for n in range(count):
        (directory/f'segment_{n:05d}.ts').write_bytes(b'closed')
        lines += ['#EXTINF:6.000000,', f'segment_{n:05d}.ts']
    if count == manager.count(job):
        lines.append('#EXT-X-ENDLIST')
    (directory/'encoder.m3u8').write_text('\n'.join(lines)+'\n')
    manager._published(job)


def test_wait_restart_no_mixed_generations_and_retention(tmp_path, monkeypatch):
    mock_build(monkeypatch)
    source = tmp_path/'source.mp4'
    source.write_bytes(b'source')

    async def exercise():
        manager = ContinuousProxyManager(tmp_path/'cache', lambda _:source)
        gate = asyncio.Event()
        async def partial(job, src, directory):
            publish(manager, job, 1)
            await gate.wait()
        manager._encode = partial
        result = await manager.start(source.name, 9)
        job = manager.jobs[result['id']]
        await wait_until(lambda: bool(manager.built[job['id']]))
        generation = job['generation']
        assert not manager.view(job)['playable'], 'partial media cannot be advertised as complete VOD'
        with pytest.raises(HTTPException) as error:
            manager.playlist(job)
        assert error.value.status_code == 503
        # Requests wait without launching any separate segment encoder.
        request = asyncio.create_task(manager.segment(job, 1, generation))
        await asyncio.sleep(.05)
        assert not request.done()
        manager.segment_wait_seconds = .01
        with pytest.raises(HTTPException) as error:
            await manager.segment(job, 1, generation)
        assert error.value.status_code == 503
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await manager.close()

        restored = ContinuousProxyManager(tmp_path/'cache', lambda _:source)
        job = restored.jobs[result['id']]
        assert not restored.view(job)['playable']
        async def full(job, src, directory):
            publish(restored, job, 2)
        restored._encode = full
        restored.start_background()
        await wait_until(lambda: job['build_status'] == 'complete')
        assert generation != job['generation']
        with pytest.raises(HTTPException) as error:
            await restored.segment(job, 0, generation)
        assert error.value.status_code == 410
        expires = job['expires_at']
        assert restored.get(job['id'])['expires_at'] == expires
        restored.clock = lambda: expires + 1
        await restored.reap_once()
        assert not restored.jobs and not restored.directory(job).exists()
        await restored.close()

    asyncio.run(exercise())


def test_gpu_fallback_restarts_whole_generation_and_delete_clears_both_caches(tmp_path, monkeypatch):
    mock_build(monkeypatch)
    source = tmp_path/'source.mp4'
    source.write_bytes(b'source')

    async def exercise():
        manager = ContinuousProxyManager(tmp_path/'cache', lambda _:source)
        calls = []
        async def encode(job, src, directory):
            calls.append((job['generation'], job['mode']))
            publish(manager, job, 1)
            if len(calls) == 1:
                job['mode'] = 'cuda'
                raise RuntimeError('hw_frames_ctx must be set')
            publish(manager, job, 2)
        manager._encode = encode
        view = await manager.start(source.name, 0)
        job = manager.jobs[view['id']]
        await wait_until(lambda: job['build_status'] == 'complete')
        assert len(calls) == 2 and calls[0][0] != calls[1][0]
        assert calls[1][1] == 'nvenc'
        old = {**job, 'id':'b'*32}
        manager.legacy.jobs[old['id']] = old
        manager.legacy.save(old)
        await manager.delete_path(source.name)
        assert not manager.jobs and not manager.legacy.jobs
        assert not manager.directory(job).exists()
        assert source.exists()
        await manager.close()

    asyncio.run(exercise())
