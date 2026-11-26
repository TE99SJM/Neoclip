# Bounded preview startup during storage delays

## Evidence and scope
The test server returned an unhandled TimeoutError from the media capture/probe path on POST /api/source. Directory scans repeatedly expired at five seconds, and the scanner was observed in D state. That points to a storage-read problem but does not distinguish host/NAS trouble from a stale container mount. Preserve the current preview design and test-only diagnostics. Do not alter main deployment or NAS mounts.

## Acceptance checks
- Source/start/queue HTTP requests have a 35-second application deadline. A timeout returns a readable 503 JSON detail and Retry-After, including when waiting for another source's lock.
- Source resolution and probe signature reads do not block the asyncio event loop. Probe timeout reports the failed stage instead of an unhandled 500; background jobs retain retry behavior.
- Browser source/queue requests use a 45-second deadline. Failed source selection clears the spinner, releases the selection lock, displays a useful message and exposes Retry preview without a page exit. Proxy polling has a deadline and shows its last request error while retrying.
- Pending/failed selections cannot save the previous video's draft against the next source. Retrying an unopened source reruns selection; active-player recovery stays unchanged.
- Preserve native controls, proxy encoding/retention, queues, and cached playback. The container diagnostics flag remains outside git.

## Tickets
1. Bound and translate startup failures (backend).
2. Bound browser waits and retry safely (depends on 1).
3. Exercise stalled probe/lock/resolve, retry and existing playback tests; publish only neoclip-playback.

## Verification
- 58 focused Python checks passed: startup deadline/lock wait/nonblocking resolver, readable FFprobe timeouts and retry cache, background error detail, source validation, persistent/continuous proxies, cached playback, library, diagnostics and shared queue API. These include existing real FFmpeg media checks.
- All 21 canonical and 18 outer Node suites passed. The new startup harness covers failure/retry, spinner cleanup, draft protection, request limits and unchanged active-player recovery. Existing native timeline, speed, seeking, deletion, rename and pull-refresh checks passed.
- Repository-configured Ruff passed for changed Python modules/tests; git diff --check passed. Full diff reviewed against the acceptance checks.
- Not tested against the user's NAS/GPU or physical iPhone. This change handles storage delays safely; it does not repair the underlying NAS connection or prove a stale Docker mount. Compare a host directory read with the same directory inside the test container before changing mounts.
