"""Two independent app instances use the same JSON and separate data roots."""
import asyncio
import importlib.util
import sys
from pathlib import Path

import httpx


def test_two_apps_share_clips_without_sharing_selection_or_rendering_twice(tmp_path, monkeypatch):
    media = tmp_path / 'media'
    media.mkdir()
    for name in ('a.mp4', 'b.mp4'):
        (media/name).write_bytes(b'video')
    modules = []
    for label in ('one', 'two'):
        monkeypatch.setenv('MEDIA_ROOT', str(media))
        monkeypatch.setenv('DATA_ROOT', str(tmp_path/label))
        monkeypatch.setenv('EXPORT_ROOT', str(tmp_path/label/'exports'))
        monkeypatch.setenv('NEOCLIP_QUEUE_FILE', str(tmp_path/'main-queue.json'))
        spec = importlib.util.spec_from_file_location('app.shared_'+label, Path(__file__).parents[1]/'app/main.py')
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        modules.append(module)
    main, test = modules

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://main') as a, httpx.AsyncClient(transport=httpx.ASGITransport(app=test.app), base_url='http://test') as b:
            async def add(client, path, name):
                r = await client.post('/api/clips', json={'path':path,'name':name,'start_s':1,'end_s':2})
                assert r.status_code == 200, r.text
                return r.json()['clips'][-1]
            first = await add(a, 'a.mp4', 'main clip')
            second = await add(b, 'a.mp4', 'test clip')
            await add(b, 'b.mp4', 'other video')
            response = (await a.get('/api/state')).json()
            assert response['source_path'] == 'a.mp4'
            assert response['clip_counts'] == {'a.mp4':2, 'b.mp4':1}
            assert (await b.get('/api/state')).json()['source_path'] == 'b.mp4'
            await a.delete('/api/clips/0', params={'path':'a.mp4','clip_id':first['id']})
            # The old row index no longer exists, but its stable ID is valid.
            r = await b.delete('/api/clips/1', params={'path':'a.mp4','clip_id':second['id']})
            assert r.status_code == 200 and r.json()['clips'] == []
            await add(a, 'a.mp4', 'render me')
            entered, release = asyncio.Event(), asyncio.Event()
            async def fake_render(*args):
                entered.set()
                await release.wait()
            monkeypatch.setattr(main, 'render_job', fake_render)
            assert (await a.post('/api/render', json={'path':'a.mp4'})).status_code == 200
            await entered.wait()
            assert (await b.post('/api/render', json={'path':'a.mp4'})).status_code == 409
            release.set()
            await main.render_task
            with test.queue_store().acquire_render():
                pass
            assert main.DATA_ROOT != test.DATA_ROOT
            assert main.QUEUE_FILE == test.QUEUE_FILE
    asyncio.run(run())
