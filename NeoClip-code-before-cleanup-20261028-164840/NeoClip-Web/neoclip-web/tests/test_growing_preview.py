"""P1: consume real HTTP HLS before its continuous encoder finishes."""

import asyncio
import re
import socket

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, HTTPException

from app import persistent_proxy
from app.continuous_proxy import ContinuousProxyManager
from tests.test_continuous_proxy import cpu, source_file, wait_until


def test_real_http_playback_during_continuous_build(tmp_path, monkeypatch):
    monkeypatch.setattr(persistent_proxy, 'choose_pipeline', cpu)
    source = source_file(tmp_path, duration=36)

    async def exercise():
        manager = ContinuousProxyManager(tmp_path/'cache', lambda _:source)
        command = manager.continuous_command
        def throttled(job, src, directory):
            args = command(job, src, directory)
            args[args.index('-i'):args.index('-i')] = ['-readrate', '3']
            return args
        manager.continuous_command = throttled
        app = FastAPI()
        app.include_router(manager.router())
        responses = []
        @app.middleware('http')
        async def record(request, call_next):
            response = await call_next(request)
            responses.append((request.url.path, response.status_code))
            return response
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
        server_task = asyncio.create_task(server.serve(sockets=[sock]))
        consumer = None
        try:
            await wait_until(lambda: server.started)
            status = await manager.start(source.name, 0)
            job = manager.jobs[status['id']]
            await wait_until(lambda: manager.view(job)['playable'])
            assert job['build_status'] == 'building', 'must become playable before full encode'
            assert 18 <= job['available_end'] < 36
            with pytest.raises(HTTPException) as pending_vod:
                manager.playlist(job)
            assert pending_vod.value.status_code == 503, 'partial data must never masquerade as complete VOD'
            growing_url = manager.view(job)['url']
            assert 'growing=true' in growing_url
            base = f'http://127.0.0.1:{port}'
            async with httpx.AsyncClient(base_url=base, timeout=5, trust_env=False) as client:
                manifest = await client.get(growing_url)
                assert manifest.status_code == 200
                initial = manifest.text
                assert 'PLAYLIST-TYPE:EVENT' in initial
                assert '#EXT-X-ENDLIST' not in initial
                assert 'DISCONTINUITY' not in initial
                names = re.findall(r'^segment/.*', initial, re.M)
                assert len(names) >= 3
                for name in names:
                    response = await client.get('/api/vod/'+name)
                    assert response.status_code == 200 and len(response.content) > 188
                consumer = await asyncio.create_subprocess_exec(
                    'ffmpeg', '-v', 'error', '-progress', 'pipe:1', '-nostats',
                    '-http_proxy', '', '-live_start_index', '0', '-i', base+growing_url,
                    '-f', 'null', '-', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                async with asyncio.timeout(15):
                    while True:
                        line = await consumer.stdout.readline()
                        assert line, 'decoder ended without producing a picture'
                        if line.startswith(b'frame=') and int(line.split(b'=')[1]) > 0:
                            break
                assert job['build_status'] == 'building', 'decode must start while encoding is active'
                # Leave the HTTP consumer attached and let the source build finish.
                async with asyncio.timeout(25):
                    output, errors = await consumer.communicate()
                assert consumer.returncode == 0, errors.decode()
                assert not errors, errors.decode()
                assert max(int(n) for n in re.findall(rb'frame=(\d+)', output)) >= 1070
                assert job['build_status'] == 'complete'
                final = (await client.get(growing_url)).text
                assert final.startswith(initial), 'published EVENT prefix is immutable'
                assert 'PLAYLIST-TYPE:EVENT' in final and final.endswith('#EXT-X-ENDLIST\n')
                assert 'TARGETDURATION:6\n' in final
                # A newly opened completed proxy gets its separate fixed VOD URL.
                complete = manager.view(job)
                assert 'growing=' not in complete['url']
                assert 'PLAYLIST-TYPE:VOD' in (await client.get(complete['url'])).text
                for name in re.findall(r'^segment/.*', final, re.M):
                    assert (await client.get('/api/vod/'+name)).status_code == 200
            # FFmpeg legitimately requests byte ranges from the TS responses.
            assert responses and all(code in (200, 206) for _, code in responses), responses
        finally:
            if consumer and consumer.returncode is None:
                consumer.kill()
                await consumer.communicate()
            await manager.close()
            server.should_exit = True
            await server_task
            sock.close()

    asyncio.run(exercise())
