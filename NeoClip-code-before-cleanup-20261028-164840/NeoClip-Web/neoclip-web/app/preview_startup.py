"""Return bounded, retryable startup failures while durable proxy jobs continue."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import HTTPException

STARTUP_TIMEOUT_SECONDS = 35
log = logging.getLogger(__name__)


@asynccontextmanager
async def preview_startup(path):
    try:
        async with asyncio.timeout(STARTUP_TIMEOUT_SECONDS):
            yield
    except TimeoutError as error:
        log.warning("Preview startup timed out: path=%s", path)
        raise HTTPException(
            503,
            "Opening this video took too long. Its storage may be slow or unavailable, "
            "or preview preparation is still busy. A queued build may continue; retry shortly.",
            headers={"Retry-After": "5"},
        ) from error
    except OSError as error:
        log.warning("Preview storage read failed: path=%s: %s", path, error)
        raise HTTPException(
            503,
            "The video storage could not be read. Check its connection and retry.",
            headers={"Retry-After": "5"},
        ) from error
    except HTTPException as error:
        if error.status_code >= 500:
            log.warning("Preview preparation failed: path=%s: %s", path, error.detail)
        raise
