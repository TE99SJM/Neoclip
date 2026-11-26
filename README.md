# NeoClip 240

**Cut video clips in your browser. Keep the files on your own server.**

[![GitHub stars](https://img.shields.io/github/stars/TE99SJM/Neoclip?style=flat)](https://github.com/TE99SJM/Neoclip/stargazers)
[![GitHub issues](https://img.shields.io/github/issues/TE99SJM/Neoclip?style=flat)](https://github.com/TE99SJM/Neoclip/issues)

NeoClip is a self-hosted video-clipping web interface built with **Python, FastAPI, and FFmpeg**. Browse videos in a directory you control, mark the moments you want, queue clips, and export individual MP4 clips or a combined highlight reel.

> **Project status:** Early public source snapshot. The repository layout and deployment instructions are still being cleaned up. Please read the setup and security notes before running it.

## What NeoClip can do

- **Browser-based controls:** Play, pause, seek, jump to a timestamp, and adjust playback speed.
- **Mark in / mark out:** Pick precise start and end times, name clips, and add them to a queue.
- **Flexible exports:** Render individual clips, a highlight reel, or both.
- **Saved editing sessions:** The server keeps queues and per-video editing state in its data directory.
- **Phone-friendly interface:** Responsive web UI and installable web-app metadata.
- **Self-hosted media:** Read source files and write exports to directories on your own machine or server.
- **FFmpeg processing:** Encoding pipeline checks for accelerated options and has a CPU fallback.

## NeoClip 240 branch (v2.1.0-240)

This branch is based on NeoClip v2.0.7 and keeps its dark editor, native Safari
slider, replay/seek controls, video details, and background library scans.

- Selecting a video queues a **full 240p proxy** in the container immediately.
  The build continues when playback stops, the page closes, or another video
  is selected. Clicking several videos queues them; the library scan alone
  does not queue every file. Browser autoplay is not required.
- Jobs and completed six-second sections persist under `/data/proxies/240-v1`.
  Container restarts resume unfinished builds using already completed sections.
  Keep the existing persistent `/data` mount.
- One background job builds at a time. At most two pipeline/section operations
  run at once, allowing requested playback sections to share the same cache.
  Safari and other browsers use the same fixed-duration VOD timeline, so a
  long video can play or seek while the rest builds. First-use sections may
  buffer while encoding. GPU selection and CPU fallback are retained; exports
  keep their original resolution.
- A completed proxy expires **14 days after completion**. Watching/reopening
  does not extend that deadline. Cleanup runs every 30 seconds and on startup.
  Unfinished jobs also have a 14-day deadline from their initial selection, so
  an unrecoverable partial build is not retained indefinitely. Temporary build
  failures retain the queue and retry after one minute.
- Deleting a source in NeoClip stops its workers, removes the source, and then
  removes its proxy. If source deletion fails, its cached proxy, marks and clips
  are kept and the build can resume. Confirmed deletions found by a successful
  directory scan also remove that file's proxy; unreadable NAS folders do not.
- Source replacement invalidates the old proxy to avoid serving the wrong
  video. Selecting an expired/replaced video creates a fresh job.

Both checked-in app copies implement this behavior. This branch does not change
Docker ports or media/export/data mounts, and does not alter the existing 360p
cache. Full proxies consume disk space for every selected video. No automatic
backup step is required to install this branch.

Validation includes real FFmpeg audio/silent 240p output, full builds with no
browser requests, queue independence, restart recovery, fixed expiration,
source deletion, failed-delete retention, and existing playback/library tests.
iPhone Safari and the server's NVIDIA GPU still require deployment testing.

## Screenshots and demo

Screenshots and a short walkthrough are being prepared.

- **Screenshot 1:** Main player and playback controls
- **Screenshot 2:** In/out marks and clip queue
- **Screenshot 3:** Rendering and completed exports
- **Demo video:** Coming soon

See [docs/MEDIA.md](docs/MEDIA.md) for where the images and video link will go.

## Quick start for local evaluation

These commands use one of the source copies currently included in the repository. They are a **developer evaluation path**, not a packaged release.

**Requirements:** Python 3.12, FFmpeg and FFprobe on your PATH, and a folder containing video files. The app supports common video containers including MP4, MKV, MOV, M4V, AVI, and WebM.

```bash
git clone --branch neoclip-240 https://github.com/TE99SJM/Neoclip.git
cd Neoclip/NeoClip-code-before-cleanup-20261028-164840/NeoClip-Web/neoclip-web

python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Change this to the video directory you want NeoClip to browse.
export MEDIA_ROOT="$HOME/Videos"
export EXPORT_ROOT="$PWD/exports"
export DATA_ROOT="$PWD/data"

uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000** on that computer. On Windows, the virtual-environment activation and environment-variable commands differ.

**Docker note:** The checked-in Dockerfile explicitly requires CUDA-related FFmpeg features (including `scale_cuda` and `h264_nvenc`) during its build. It may **not** build with an ordinary FFmpeg package. Docker installation is therefore not presented as a one-command setup yet.

### Important security note

**Do not publish this server directly to the internet.** The current API does not implement user login/access control, and it includes an endpoint that deletes the selected source video from disk when the media directory is writable. Use only on a trusted machine/network until access controls and deployment safeguards are in place. See [SECURITY.md](SECURITY.md).

## Project structure

The current repository contains a backup-style source directory with two copies of the web app. The example above uses the copy that includes the additional browser tests. A cleanup into a simpler root-level source layout is planned; avoid assuming the nested folder names are permanent.

## Feedback and development

Found a bug or have an improvement in mind? [Open an issue](https://github.com/TE99SJM/Neoclip/issues/new/choose).

Read [CONTRIBUTING.md](CONTRIBUTING.md) before proposing changes.

## Support development

NeoClip is available to view and evaluate on GitHub. If you find the project useful, you can star the repository, share it with another video editor, or support ongoing development when the maintainer's GitHub Sponsors profile is enabled. Installation and customization services may be offered separately in the future; no paid services are currently advertised as available.

## License

Copyright (C) 2026 TE99SJM

NeoClip is free, open-source software licensed under the [GNU Affero General Public License v3.0](LICENSE) (AGPLv3).

- **Use it for anything, including paid work.** Individuals, schools, and businesses can use NeoClip, including to make money from the videos they produce.
- **Changes have to stay open.** If you share a modified NeoClip, or run a modified version as a website or service other people use, you must make your full source code available to them under the AGPLv3. You can't turn NeoClip into a closed, private product.
- **Keep the notices.** Keep the `LICENSE` file and copyright notices, mark what you changed, and keep the link to the source code in the app.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the [LICENSE](LICENSE) for details.

## 2.1.2-240: source filenames

Select a video and use **Rename video** to change its filename in the same folder. The original extension is preserved (and added if omitted). Queued clips, saved marks, playback position and existing proxy IDs follow the new name. Existing destination files are never overwritten; renaming waits until that video's active render finishes.

After all clips in a successful render finish, NeoClip appends ` clipped` before the source extension: `movie.mkv` becomes `movie clipped.mkv`. The suffix is not repeated. Failed/cancelled renders do not mark the source; newly queued clips defer marking until they are processed. If the NAS denies a rename or the destination already exists, the successful exports remain available and the render status explains the filename warning. The 240p cache's retention deadline is unchanged.


## 2.1.5 test: continuous 240p preview

`neoclip-240-stable` now holds the promoted 2.1.4 production snapshot. `neoclip-240-test` adds a single continuous encode per preview, removing the per-section decoder resets. The native full-duration timeline remains; playback/seek waits for an unfinished section. Selection still queues a whole-video build without a viewer, and completed proxies retain their fixed 14-day expiry.

Each interrupted build restarts from the beginning with fresh generation URLs; completed runs survive restart. Existing test caches are retained separately until their original expiry or source deletion. They are not reused as continuous streams, so the first selection builds a new preview. The main and test apps continue using the existing shared clip queue and separate proxy/export directories.

See [the spec and review](docs/CONTINUOUS_PROXY_SPEC.md). `scripts/deploy_current_and_continuous.sh <test-commit>` installs the pinned production snapshot on port 8000 before switching the test checkout on port 8001. It stops on an active render or failed version check and leaves existing Compose mounts and queue files in place. This script is specific to Samuel's documented mserver paths.
