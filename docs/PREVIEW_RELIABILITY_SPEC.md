# Test preview reliability — 2.1.6

## Evidence and scope
The user reports preview failures on test port 8001. Production stays pinned to its promoted snapshot. Real FFmpeg reproduction against 2.1.5 found: (1) playable=true after one of three sections while the advertised next section returns HTTP 503; (2) 6-second video with 20-second audio retries forever because format duration predicts four sections while the encoder closes two. Frontend reconnect also always resolves the original NAS source before reusing a cached job.

## Acceptance checks
- Safari receives a full VOD playlist only after its encoder finished and every listed section exists and is nonempty. No speculative future segment URLs. Preserve native controls and full native seeking; show preparation progress while waiting. Builds continue without a viewer.
- Use actual FFmpeg segment lengths/count and a successful ended playlist, not guessed container-duration counts, for completed preview metadata. Normalize audio length to video duration; no endless rebuild caused solely by longer audio.
- Reconnection reuses an existing cached job through the local status endpoint; fall back to source start only if the cached job is missing or belongs to a different source. Healthy playback remains attached.
- Preserve selection, marks, clip name, playback position/paused intent, queue, rename/delete, retention, and pull refresh. Test changes only.
- Real-media tests cover partial builds, delayed encoding, uneven track durations, finished cached playback, seeks/replay, restore and deletion. JavaScript checks exercise recovery offline from NAS and waiting until complete.

## Tickets
1. Reproduce and cover readiness and duration failures.
2. Fix finalized manifest validation/duration and preparation UI (depends on 1; full completion wait preserves the native full timeline).
3. Recover cached previews without NAS resolution; cover cached-job fallback and paused/playing intent.
4. Run relevant/full checks, inspect both app-copy diffs, push test only and provide pinned test-only install commands.

## Limits
No direct mserver, NVIDIA or iPhone execution is available. Automated media/HTTP tests demonstrate the server failures and fixes; Safari device behavior still requires installing on test and playing there.

## Review
Both checked-in app copies retain their existing UI differences and receive the same focused backend/recovery changes. Production branches and deployment configuration are not changed. The first-play wait is an intentional tradeoff: unfinished VOD URLs caused reproducible HTTP failures. No native control suppression is used.

Validation: full canonical Python suite 120 passed; all 17 canonical JavaScript suites passed; Ruff passed. The outer copy also passed all 14 JavaScript suites and 52 affected Python tests after the final cache-restore guard change. Real FFmpeg coverage includes audio longer/shorter than video, silent output, exact/short durations, sequential transport counters and timestamps, decoding the finalized playlist, replay/random access and cache-only playback with NAS resolution deliberately failing. The partial-build test now rejects playlist delivery and reports playable=false until completion. Recovery JavaScript tests exercise cached status reuse with zero source-start calls, expired-cache fallback, network errors, lock/wake and paused/playing intent with marks preserved.

Physical Safari and NVIDIA deployment have not been run here. The diagnosed server faults are fixed in tests, but any remaining device-specific failure needs the updated test build's events.
