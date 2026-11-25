import json
import multiprocessing
import uuid

import pytest

from app.queue_store import QueueStore


def client(path, source='a.mp4'):
    state = {'source_path': source, 'queues': {}, 'editor_sessions': {}}
    store = QueueStore(path, state)
    store.refresh()
    return store, state


def add(state, name, path='a.mp4'):
    clip = {'id': uuid.uuid4().hex, 'name': name, 'start_s': 1, 'end_s': 2}
    state['queues'].setdefault(path, []).append(clip)
    return clip


def test_stale_writers_preserve_both_queues_and_selections(tmp_path):
    a, sa = client(tmp_path/'queue.json')
    b, sb = client(a.path, 'b.mp4')
    add(sa, 'main')
    add(sb, 'test', 'b.mp4')
    a.save()
    b.save()
    a.refresh()
    assert sa['source_path'] == 'a.mp4' and sb['source_path'] == 'b.mp4'
    assert sa['queues'] == sb['queues']
    assert sum(map(len, sa['queues'].values())) == 2


def test_render_snapshot_removal_keeps_concurrent_adds(tmp_path):
    a, sa = client(tmp_path/'queue.json')
    rendered = add(sa, 'rendered')
    a.save()
    b, sb = client(a.path)
    added = add(sb, 'added during render')
    b.save()
    sa['queues']['a.mp4'].remove(rendered)
    a.save()
    assert sa['queues']['a.mp4'] == [added]
    # A stale session save must not put the rendered clip back.
    sb['editor_sessions']['a.mp4'] = {'current_time': 50}
    b.save()
    assert sb['queues']['a.mp4'] == [added]


def test_rename_moves_other_clients_pending_edits(tmp_path):
    a, sa = client(tmp_path/'queue.json')
    add(sa, 'old')
    a.save()
    b, sb = client(a.path)
    added = add(sb, 'pending')
    a.rename('a.mp4', 'renamed.mp4')
    b.save()
    assert 'a.mp4' not in sb['queues']
    assert sb['source_path'] == 'renamed.mp4'
    assert sb['queues']['renamed.mp4'][-1] == added
    assert b.resolve('a.mp4') == 'renamed.mp4'


def test_bad_json_is_not_overwritten(tmp_path):
    path = tmp_path/'queue.json'
    a, sa = client(path)
    add(sa, 'keep')
    path.write_text('{broken')
    with pytest.raises(ValueError):
        a.save()
    assert path.read_text() == '{broken'


def test_import_is_deduplicated_and_idempotent(tmp_path):
    path = tmp_path/'queue.json'
    a, sa = client(path)
    add(sa, 'same')
    sa['editor_sessions']['a.mp4'] = {'mark_in': 9}
    a.save()
    test = tmp_path/'test.json'
    test.write_text(json.dumps({'queues': {'a.mp4': [
        {'name': name, 'start_s': 1, 'end_s': 2} for name in ['same', 'same', 'new']
    ]}, 'editor_sessions': {'a.mp4': {'mark_in': 1}}}))
    a.import_queue(test)
    a.refresh()
    assert [c['name'] for c in sa['queues']['a.mp4']] == ['same', 'same', 'new']
    assert sa['editor_sessions']['a.mp4']['mark_in'] == 9
    sa['queues']['a.mp4'].clear()
    a.save()
    a.import_queue(test)
    assert a.read()['queues']['a.mp4'] == []


def test_legacy_ids_stable_across_processes_and_deletes(tmp_path):
    path = tmp_path/'queue.json'
    path.write_text(json.dumps({'source_path':'a.mp4', 'clips':[
        {'name':'same'}, {'name':'same'}, {'name':'other'}]}))
    a, sa = client(path)
    b, sb = client(path)
    ids = [c['id'] for c in sa['queues']['a.mp4']]
    assert len(set(ids)) == 3
    assert sa['queues'] == sb['queues']
    del sa['queues']['a.mp4'][0]
    a.save()
    del sb['queues']['a.mp4'][2]
    b.save()
    assert [c['id'] for c in b.read()['queues']['a.mp4']] == [ids[1]]


def worker(path, barrier, prefix):
    a, state = client(path)
    barrier.wait(timeout=10)
    for i in range(20):
        add(state, f'{prefix}-{i}')
        a.save()


def test_actual_processes_do_not_lose_concurrent_adds(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    barrier = ctx.Barrier(2)
    path = tmp_path/'queue.json'
    processes = [ctx.Process(target=worker, args=(path,barrier,p)) for p in ('main','test')]
    for p in processes:
        p.start()
    for p in processes:
        p.join(20)
        assert p.exitcode == 0
    a, state = client(path)
    assert len(state['queues']['a.mp4']) == 40
    assert len({c['id'] for c in state['queues']['a.mp4']}) == 40


def test_render_lease_rejects_other_container_and_releases(tmp_path):
    a, _ = client(tmp_path/'queue.json')
    b, _ = client(a.path)
    with a.acquire_render():
        with pytest.raises(BlockingIOError):
            b.acquire_render()
    with b.acquire_render():
        pass
