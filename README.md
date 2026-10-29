# NeoClip

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
git clone https://github.com/TE99SJM/Neoclip.git
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

Read [CONTRIBUTING.md](CONTRIBUTING.md) before proposing changes. Project promotion and demo guidance live in [docs/PROMOTION.md](docs/PROMOTION.md).

## Support development

NeoClip is available to view and evaluate on GitHub. If you find the project useful, you can star the repository, share it with another video editor, or support ongoing development when the maintainer's GitHub Sponsors profile is enabled. Installation and customization services may be offered separately in the future; no paid services are currently advertised as available.

## License

**A license has not yet been selected.** Publicly visible source is not automatically open-source licensed. Until a license is added, do not assume permission to redistribute, modify, or use the code commercially. The maintainer should choose a license before promoting NeoClip as open-source software.
