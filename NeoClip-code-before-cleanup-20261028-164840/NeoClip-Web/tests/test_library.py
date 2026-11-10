import importlib
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def library(tmp_path, monkeypatch):
    media = tmp_path / 'media'
    media.mkdir()
    monkeypatch.setenv('MEDIA_ROOT', str(media))
    monkeypatch.setenv('DATA_ROOT', str(tmp_path / 'data'))
    monkeypatch.setenv('EXPORT_ROOT', str(tmp_path / 'exports'))
    main = importlib.import_module('app.main')
    monkeypatch.setattr(main, 'MEDIA_ROOT', media)
    monkeypatch.setattr(main, 'QUEUE_FILE', tmp_path / 'queue.json')
    monkeypatch.setattr(main, 'state', {'source_path': '', 'queues': {}, 'editor_sessions': {}})
    return main, media, TestClient(main.app)


def test_recursive_transfer_change_rename_delete_and_cache_headers(library):
    _, media, client = library
    assert client.get('/api/files').json() == []
    nested = media / 'transfers' / 'day one'
    nested.mkdir(parents=True)
    source = nested / 'new.MP4'
    source.write_bytes(b'first')
    response = client.get('/api/files')
    assert response.headers['cache-control'] == 'no-store'
    first = response.json()
    assert first[0]['path'] == 'transfers/day one/new.MP4'
    assert first[0]['size_bytes'] == 5
    assert '%20' in first[0]['original_url']
    source.write_bytes(b'longer transfer')
    grown = client.get('/api/files').json()
    assert grown[0]['size_bytes'] > first[0]['size_bytes']
    # A same-size replacement must be visible too.
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert client.get('/api/files').json()[0]['modified_ns'] != grown[0]['modified_ns']
    renamed = media / 'renamed.mkv'
    source.rename(renamed)
    assert [item['path'] for item in client.get('/api/files').json()] == ['renamed.mkv']
    renamed.unlink()
    assert client.get('/api/files').json() == []


def test_scan_survives_racing_removal_and_ignores_unsupported_files(library, monkeypatch):
    main, media, _ = library
    for name in ['good.mp4', 'vanishing.mp4', 'notes.txt', '.partial.mp4']:
        (media / name).write_bytes(b'data')
    (media / 'empty.mp4').touch()
    outside = media.parent / 'private.mp4'
    outside.write_bytes(b'private')
    (media / 'outside.mp4').symlink_to(outside)
    stat = Path.stat

    def racing_stat(path, *args, **kwargs):
        if path.name == 'vanishing.mp4':
            raise FileNotFoundError(path)
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', racing_stat)
    assert [item['path'] for item in main.list_videos()] == ['good.mp4']


def test_empty_mount_can_appear_later(library, monkeypatch):
    main, media, client = library
    mount = media / 'mounted-later'
    monkeypatch.setattr(main, 'MEDIA_ROOT', mount)
    assert client.get('/api/files').json() == []
    mount.mkdir()
    (mount / 'arrived.mov').write_bytes(b'video')
    assert client.get('/api/files').json()[0]['name'] == 'arrived.mov'


def test_reopening_eof_bookmark_resets_time_but_preserves_edits(library, monkeypatch):
    main, media, client = library
    (media / 'sample.mp4').write_bytes(b'video')

    async def probe(source):
        return 12, True

    async def start(path, requested):
        return {'id': 'test', 'start_seconds': requested, 'duration': 12}

    monkeypatch.setattr(main.proxies, 'probe', probe)
    monkeypatch.setattr(main.proxies, 'start', start)
    client.post('/api/session', json={
        'path': 'sample.mp4', 'current_time': 12, 'mark_in': 2,
        'mark_out': 4, 'clip_name': 'keep this',
    })
    main.state['queues']['sample.mp4'] = [{'name': 'clip', 'start_s': 2, 'end_s': 4}]
    result = client.post('/api/source', json={'path': 'sample.mp4'}).json()
    assert result['proxy']['start_seconds'] == 0
    assert result['current_session']['current_time'] == 0
    assert result['current_session']['mark_in'] == 2
    assert result['current_session']['mark_out'] == 4
    assert result['current_session']['clip_name'] == 'keep this'
    assert len(result['clips']) == 1
    stored = json.loads(main.QUEUE_FILE.read_text())
    assert stored['editor_sessions']['sample.mp4']['current_time'] == 0
    # Deliberate seeks toward the end still retain the requested timestamp.
    seek = client.post('/api/proxy/start', json={'path': 'sample.mp4', 'current_time': 11.8})
    assert seek.json()['start_seconds'] == 11.8


def test_native_source_and_seek_use_full_timeline_manager(library, monkeypatch):
    main, media, client = library
    (media / 'sample.mp4').write_bytes(b'video')
    calls = []

    async def probe(source):
        return 4000, True

    async def start(path, requested):
        calls.append((path, requested))
        return {'id': 'native', 'start_seconds': 0, 'duration': 4000,
                'delivery': 'vod', 'status_url': '/api/vod/status?id=native'}

    async def wrong_manager(*args):
        raise AssertionError('Safari must not receive an offset EVENT proxy')

    monkeypatch.setattr(main.proxies, 'probe', probe)
    monkeypatch.setattr(main.proxies, 'start', wrong_manager)
    monkeypatch.setattr(main.native_proxies, 'start', start)
    client.post('/api/session', json={'path': 'sample.mp4', 'current_time': 3600})
    result = client.post('/api/source', json={'path': 'sample.mp4', 'native_vod': True})
    assert result.status_code == 200
    assert result.json()['current_session']['current_time'] == 3600
    assert result.json()['proxy']['start_seconds'] == 0
    seek = client.post('/api/proxy/start', json={
        'path': 'sample.mp4', 'native_vod': True, 'current_time': 0,
    })
    assert seek.json()['delivery'] == 'vod'
    assert calls == [('sample.mp4', 3600), ('sample.mp4', 0)]
