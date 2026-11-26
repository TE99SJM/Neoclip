# Continuous 240p preview — CIDER

## Requirements and evidence

Promote the current test snapshot a87dd2e1ffd3a8a2109bbd46013ff5f83b048280 to the production release branch before changing test playback. Production keeps that snapshot's behavior; the continuous encoder runs only on test.

Phone diagnostics show video/audio track removal and addition during playback at section boundaries, with a decode error. Current six-second encodes reset timestamps and advertise a discontinuity at every join. A real FFmpeg comparison reproduced those resets; a single encoder kept adjacent audio/video timestamps sequential and decoded without errors. The observed Safari UI response remains to be verified on the phone.

The user accepts waiting when seeking into an unfinished portion of a new proxy. Keep the native full-duration slider, start playback as soon as the requested section is available, continue building without viewers, retain proxies for 14 days after completion, and delete them with their source. Preserve UI, pull-to-refresh, the shared clip queue, rename and exports.

## Acceptance checks

- One continuous FFmpeg run creates a proxy's successive sections; healthy playback has no per-section discontinuity or player reload.
- A fixed VOD manifest advertises the source timeline; the browser waits through status polling before a seek to an unbuilt section. Completed sections are served locally even with the NAS offline.
- Adjacent video and audio packet timestamps and MPEG-TS continuity counters remain continuous. Audio and silent inputs decode through multiple boundaries and a short final section.
- Builds survive viewer disconnection. Cancellation/restart never publishes partial sections. Failed encoders retry in a new generation, never mix old/new runs in one playlist.
- GPU fallback starts a clean generation; disk/read errors do not loop through GPU modes. Completion starts the fixed retention deadline; source deletion/rename and shared queue locking still work.
- Existing v1 caches retain their expiry and remain available to the old production app. Test creates new continuous-format jobs instead of serving v1 sections as the new format.
- Production and test deployment use separate caches and the existing shared queue, with production pinned before test changes are applied.

## Tickets and dependencies

1. Promote snapshot to production branch; change only its build label and deployment documentation.
2. Implement continuous encoder/cache generation and bounded waits (depends on evidence above).
3. Integrate frontend waiting without recovery/reload churn (depends on 2).
4. Exercise real media, lifecycle, shared queue and UI regression tests; review full diff (depends on 2–3).
5. Publish test and supply ordered production/test installation commands (depends on 1 and 4).

## Review record

Production promotion committed as `070d2115a0b1a1c771630f3ab6e1ec0114f30b0b` (2.1.4-240). Test is 2.1.5-240-test. All five tickets implemented; deployment itself awaits execution on mserver.

Review: continuous timestamps and MPEG-TS counters replace per-section resets; native controls remain enabled. Generation-qualified immutable URLs prevent mixing encoder runs. Unfinished seeks poll coverage before proceeding; a changed selection cancels its prior wait. Interrupted encodes rebuild from the start in a fresh generation. Completed caches survive restart/NAS outage. Legacy caches keep expiry and follow source rename/deletion. Existing shared queue paths and locks are unchanged.

Checks performed: canonical Python suite 114 passed before final edge-case additions; expanded continuous-media suite: 8 passed, covering short/exact-boundary, audio/silent and 25/30fps files, transport counters, timestamp continuity, complete decode, cached random access, interrupted process cleanup, restart, fallback, deletion and retention. Outer-copy affected Python suite 47 passed. All existing/new JavaScript suites passed in both copies; updated continuous seeking/recovery checks passed after final cancellation changes. Ruff and shell syntax checks passed. No physical iPhone, NVIDIA GPU or mserver deployment was available here.

References: [Apple HLS authoring requirements](https://developer.apple.com/streaming/hls-authoring-specification-for-apple-devices.html) (media continuity and sequential transport timestamps/counters); [FFmpeg HLS muxer](https://ffmpeg.org/ffmpeg-formats.html#hls-2).
