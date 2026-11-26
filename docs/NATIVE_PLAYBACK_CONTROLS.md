# Playback 1.1: finite timelines with Safari controls

## Requirements and scope
The user reports the native speed menu disappeared and asks for the native progress/seek bar for both proxy and original playback. Keep early proxy playback and the browser's controls; no replacement timeline. Deploy instructions target the existing test app on port 8001, as subsequently authorized. Main and stable/test branch refs remain unchanged.

## Spec and acceptance checks
- A growing continuous proxy uses hls.js with a finite MediaSource duration on supported browsers, including iPhone ManagedMediaSource. Keep the video element's controls, fullscreen and PiP. Safari chooses the exact native menu; physical iPhone verification is required.
- Only published segments are requested. The timeline uses the known recording duration; a seek beyond encoded coverage waits using the existing cancellable coverage flow. No fake completed HLS playlist or future segment URLs.
- Fully built proxies and directly playable originals retain native file/VOD delivery. Older Safari without a usable MediaSource waits for a completed proxy to retain finite native playback.
- Preserve speed, position, play/pause intent, marks, shared queue and pull refresh. Completion must not reattach a healthy player. No repeated playbackRate assignments or catch-up speed changes.
- Pin the hls.js version used by the integration. Keep an HLS alternative for AirPlay on ManagedMediaSource and restore native source state on teardown.

## Tickets
1. Confirm the regression in the current branch and WebKit source (done): live/open-ended duration suppresses playback rates.
2. Add finite proxy attachment and capability fallback, dependent on 1.
3. Exercise attachment, seek waiting/cancellation, speed retention, completion and direct-file routing; review both app copies, dependent on 2.
4. Publish only neoclip-playback and provide test-server update commands. Verify native controls on the user's iPhone after installation.

## References
- https://github.com/WebKit/WebKit/blob/main/Source/WebCore/Modules/modern-media-controls/media/overflow-support.js
- https://webkit.org/blog/14735/webkit-features-in-safari-17-1/
- https://github.com/video-dev/hls.js/blob/v1.7.4/src/controller/buffer-controller.ts

## Actual checks
- Canonical app: all 19 JavaScript suites passed. Outer app: all 16 JavaScript suites passed. New executable cases cover finite attachment, native VOD/original routing, unavailable MediaSource fallback, completion without reattachment, AirPlay alternative cleanup/return, finite error recovery and cancellation of an outstanding seek by a later slider gesture. The speed suite verifies native-menu choices survive metadata/attachment resets.
- Real HTTP/FFmpeg and cached playback: 12 focused Python tests passed (`test_continuous_proxy.py`, `test_growing_preview.py`, `test_cached_preview.py`). These include decoding before the encode finishes and consuming the same growing stream to completion.
- Tested the actual hls.js 1.7.4 buffer controller with its finite duration override for startup, playlist growth and exact completion duration. This is a library-level test, not a browser test.
- Repository-configured Ruff, inline JavaScript syntax and `git diff --check` passed. An initial Ruff invocation used inherited workspace settings before the repo configuration was downloaded; the final run used the checked-in pyproject.toml.
- Reviewed both app diffs: no custom slider, no encoder/manifest/queue changes; completed native playback and editing layout retained. Published only the playback feature branch.
- Browser runtime installation failed with HTTP 403 from the download host. Physical iPhone/MMS, fullscreen, PiP, AirPlay and NVIDIA behavior have not been verified here. Confirm the timeline and speed menu on the user's test app before promoting.

## Test on port 8001
1. Install this branch on the existing test checkout and recreate only `neoclip-test`; confirm `2.2.0-playback.2`.
2. Open an uncached recording longer than one minute. Once the initial buffer is ready, verify Safari's timeline shows the recording duration and its menu offers speed choices.
3. Select 1.5x or 2x in Safari; allow the proxy to finish and confirm the speed and position stay stable. Scrub backwards, then forwards beyond the built coverage; a later backwards scrub must cancel the pending wait.
4. Reopen a completed proxy and check native controls, end/replay, fullscreen and PiP. Test supported original-file playback when available; original-first startup is still P3.
5. Older Safari or a blocked hls.js dependency waits for a completed proxy; it should not attach an open-ended native stream.
