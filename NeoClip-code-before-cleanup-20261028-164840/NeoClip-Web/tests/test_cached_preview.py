import asyncio
import time

import pytest
from fastapi import HTTPException

from app.persistent_proxy import PersistentProxyManager


def test_cached_segments_play_with_nas_offline_but_uncached_still_fail(tmp_path):
    def offline(_):
        raise OSError('NAS temporarily unavailable')
    async def run():
        manager = PersistentProxyManager(tmp_path, offline)
        job = {'id':'abc', 'path':'sample.mp4', 'duration':12,
               'expires_at':time.time()+86400, 'signature':'original'}
        manager.directory(job).mkdir(parents=True, exist_ok=True)
        segment = manager.directory(job)/'segment_00000.ts'
        segment.write_bytes(b'cached video')
        response = await manager.segment(job, 0)
        assert response.path == segment
        with pytest.raises(OSError, match='NAS'):
            await manager.segment(job, 1)
        assert segment.read_bytes() == b'cached video'
        job['expires_at'] = time.time()-1
        with pytest.raises(HTTPException):
            await manager.segment(job, 0)
        await manager.close()
    asyncio.run(run())
