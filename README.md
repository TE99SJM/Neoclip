# NeoClip

A simple web UI video clipper (FastAPI + FFmpeg, GPU-first with CPU fallback).

## Files

- `Dockerfile` – container image (requires a CUDA-enabled FFmpeg build)
- `requirements.txt` – Python dependencies
- `app/main.py` – web server and API
- `app/proxies.py` – FFmpeg preview/proxy pipeline
- `app/web/` – browser interface, icons, and PWA files

## Run

```bash
docker build -t neoclip-web .
docker run -d --name neoclip-web --gpus all -p 8000:8000 \
  -v /path/to/videos:/media \
  -v /path/to/exports:/exports \
  -v "$PWD/data":/data \
  neoclip-web
```

Open `http://<server>:8000`. Keep the `data/` folder between upgrades; it holds the clip queue and resume positions.
