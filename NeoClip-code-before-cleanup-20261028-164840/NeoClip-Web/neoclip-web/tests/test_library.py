import asyncio
import importlib
import json
import os
import sys
import threading
import time
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
    monkeypatch.setattr(main, 'video_metadata', main.VideoMetadata(
        tmp_path / 'metadata.json', main.resolve_media_path,
    ))
    monkeypatch.setattr(main, 'library_index', main.VideoLibrary(
        media, tmp_path / 'library.json', interval=.03,
    ))
    with TestClient(main.app) as client:
        yield main, media, client


def wait_listing(client, predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get('/api/files?details=true')
        listing = response.json()
        if listing['scan']['status'] == 'ready' and predicate(listing['files']):
            return response, listing['files']
        time.sleep(.01)
    raise AssertionError(f"Library did not settle: {listing}")


def test_recursive_transfer_change_rename_delete_and_cache_headers(library):
    _, media, client = library
    assert client.get('/api/files').json() == []
    nested = media / 'transfers' / 'day one'
    nested.mkdir(parents=True)
    source = nested / 'new.MP4'
    source.write_bytes(b'first')
    response, first = wait_listing(client, lambda files: len(files) == 1)
    assert response.headers['cache-control'] == 'no-store'
    assert first[0]['path'] == 'transfers/day one/new.MP4'
    assert first[0]['size_bytes'] == 5
    assert '%20' in first[0]['original_url']
    source.write_bytes(b'longer transfer')
    _, grown = wait_listing(client, lambda files: files and files[0]['size_bytes'] > 5)
    assert grown[0]['size_bytes'] > first[0]['size_bytes']
    # A same-size replacement must be visible too.
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    wait_listing(client, lambda files: files and files[0]['modified_ns'] != grown[0]['modified_ns'])
    renamed = media / 'renamed.mkv'
    source.rename(renamed)
    wait_listing(client, lambda files: [item['path'] for item in files] == ['renamed.mkv'])
    renamed.unlink()
    wait_listing(client, lambda files: not files)


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
    main.library_index.root = mount
    assert client.get('/api/files').json() == []
    mount.mkdir()
    (mount / 'arrived.mov').write_bytes(b'video')
    wait_listing(client, lambda files: files and files[0]['name'] == 'arrived.mov')


def test_duration_metadata_fills_in_on_existing_files_endpoint(library, monkeypatch):
    main, media, client = library
    (media / 'sample.mp4').write_bytes(b'video')
    calls = []

    async def probe(source):
        calls.append(source.name)
        return 90.5

    monkeypatch.setattr(main.video_metadata, 'probe', probe)
    _, files = wait_listing(client, lambda files: bool(files))
    assert files[0]['size_bytes'] == 5
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        updated = client.get('/api/files').json()[0]
        if not updated['duration_pending']:
            break
        time.sleep(.01)
    assert updated['duration_seconds'] == 90.5
    assert calls == ['sample.mp4']


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


def test_api_returns_cached_videos_while_directory_reader_is_stuck(library, monkeypatch):
    main, media, client = library
    (media / 'sample.mp4').write_bytes(b'video')
    wait_listing(client, lambda files: len(files) == 1)
    started = threading.Event()
    processes = []

    async def spawn(directory):
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-u', '-c', 'import time; time.sleep(60)',
            stdout=asyncio.subprocess.PIPE,
        )
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(main.library_index, '_spawn', spawn)
    assert started.wait(3)
    start = time.monotonic()
    for _ in range(10):
        response = client.get('/api/files?details=true')
        assert response.status_code == 200
        assert response.json()['files'][0]['path'] == 'sample.mp4'
        assert response.json()['scan']['status'] == 'scanning'
    assert time.monotonic() - start < 1
    assert len(processes) == 1
    assert client.get('/api/state').status_code == 200
