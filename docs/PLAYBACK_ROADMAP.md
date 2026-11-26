# NeoClip playback branch — small verified releases

## Current test deployment and native controls
The user subsequently authorized replacing the existing test app on port 8001. The original P1 isolation instructions below describe the initial publication only; a third container is no longer required. Main on port 8000 remains outside this experiment.

Playback 1.1 (`2.2.0-playback.2`) addresses missing native speed/timeline controls with a finite MediaSource timeline for growing proxies. See [native playback controls spec and checks](NATIVE_PLAYBACK_CONTROLS.md). P2–P4 remain future work; this does not add original-first startup or prioritized encoding after a skip.

## Clarified requirements and isolation
Start from test commit 46b0cc23f8136fc61ff0838f8c5796048d69728b on a new `neoclip-playback` branch. User explicitly requires the current test server be left intact. Publish branch code only: do not advance `neoclip-240-test` or stable, restart either existing container, repoint their checkouts, or modify their data/shared queue.

An eventual live trial needs a third playback-lab container, unused port and separate checkout/data/exports/queue. Source mounts should be read-only for the playback experiment. That trial is not deployed by this change; shared GPU/CPU resources would still need monitoring.

## Strategy and tickets
| Ticket | Small release | Dependency | Observable acceptance |
| --- | --- | --- | --- |
| P1 | Start from 00:00 and play a ready buffer while encoding continues | Current continuous encoder | Decode video before full completion; only completed segments advertised; no per-segment decoder resets; background build continues without a viewer |
| P2 | Prioritize missing media after a skip | P1 device playback verified | Rapid skips coalesce to the latest target; cached skips are immediate; a missing target gets priority; source-time marks stay accurate; do not splice independently reset segments |
| P3 | Original-quality startup for supported sources | P1 verified; compatibility checks | Use browser-playable original sources with working byte ranges, bounded loading and fallback; MKV/unsupported codecs fall back to proxy without an error loop |
| P4 | One controlled handoff when proxy gets ahead | P2 + P3 verified | Switch once with adequate lead, matching source time and paused/playing intent; no duplicate audio, lost marks, repeat switching or healthy-player resets |

Only P1 is implemented/pushed in this update. Evaluate it before implementing P2–P4.

## P1 spec — 2.2.0-playback.1
- Keep one continuous 240p encode per video, beginning at 00:00, even with no viewer. Keep the existing queue order, fixed retention and source-delete cleanup.
- Use a separate growing EVENT playlist URL that appends only closed, published segments. Start after three 6-second segments (18 seconds of media), or immediately when a shorter proxy finishes. This is encoded media lead, not a promised 18-second wall-clock wait.
- Keep EVENT headers, segment URLs, durations and target duration stable. Append ENDLIST only after successful encoder completion/source validation. Do not switch its type or replace the player's source when it completes. A new attachment to an already-completed proxy may use the existing fixed VOD URL.
- First playback/resume and uncovered seeks wait until there is enough encoded lead. No requests to imaginary future segments. Preserve native controls. While encoding, Safari's timeline/labels are browser-managed and may represent a growing stream; the full recording becomes seekable when complete. No custom replacement timeline.
- P1 does not prioritize encoding on skip or play the original file. An uncovered skip waits for the sequential encoder, clearly showing the requested time and build coverage.
- Keep healthy playback attached during status polling and completion; preserve marks, clip name, selection, playback speed/intent, cached reconnect and pull refresh.

## Verification plan
1. Real FFmpeg: expose a throttled continuous encode over local HTTP and start a decoder before completion; read every advertised segment successfully; playlist grows by append, then ends without reattachment; decoded audio/video timestamps/counters remain continuous.
2. Short/full cached previews retain finite VOD playback, replay and zero/end seeking; incomplete VOD URLs stay blocked.
3. Frontend executable tests: early readiness, resume lead, unbuilt-skip wait, cancelled selection, stale generation recovery, and no reattachment at completion. Existing playback, recovery, rename/delete, library and shared-queue suites remain green.
4. Review complete diff in both app copies; publish only the feature branch. Record actual results below. Physical iPhone/NVIDIA validation is deferred to an isolated lab.

## Protocol reference
RFC 8216 sections 4.3.3.5, 6.2.1 and 6.3.3: EVENT is append-only; listed media must be immediately downloadable; target duration cannot change; an unfinished stream needs playback lead. https://www.rfc-editor.org/rfc/rfc8216.html

## P1 review and actual checks
- Full canonical run: 120 existing Python tests passed. The new HTTP test initially rejected valid 206 byte-range responses; after correcting that test assertion, it passed. The final focused run passed 12 tests covering the real HTTP stream, all continuous-encoder cases, and export/API integration.
- Real HTTP/FFmpeg experiment: a throttled 36-second source was already decoding while build_status was still building; at least 1,070 frames were decoded through the same growing playlist. Published sections returned 200/206, the EVENT prefix stayed unchanged, and ENDLIST arrived only at completion. Incomplete fixed VOD delivery was rejected.
- Canonical JavaScript: all 18 suites passed. Outer-copy JavaScript: all 15 suites passed. Ruff passed for both copies. Executable tests verify safe early startup/resume, sequential waiting for an uncovered skip, cancellation, and completion heartbeat without writes to video.src/currentTime/paused state.
- Reviewed both application copies against the exact test-2.1.6 base; UI differences between copies are preserved. Backend changes are limited to growing playlist delivery/readiness and accepting an already-published tail section when the container-duration estimate rounded down. Existing encoding, cache, queue and render implementations are retained.
- No changes deployed to mserver. No current test or production branch/container/queue modified. This release requires a separate lab for physical Safari and NVIDIA verification; the native browser may show live-style controls while the playlist is growing.

