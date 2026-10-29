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
  -v /path/on/server:/host \
  -v "$PWD/data":/data \
  neoclip-web
```

Open `http://<server>:8000`, click **Settings**, and use **Browse…** to pick the
input folder (source videos) and output folder (rendered clips) anywhere under
`/host`. Click **Save**. The choices are stored in `data/neoclip_settings.json`
and restored on every launch, so the app always starts even before folders are
chosen; rendering is blocked only until a writable output folder is set.

Keep the `data/` folder between upgrades; it holds the settings, clip queues,
and resume positions. Each input folder keeps its own clip queue.

### Upgrading from 1.1

The old `/media` and `/exports` mounts still work and are used as the defaults
until you save folders in Settings. Existing queues carry over to `/media`.

### Options

| Variable | Default | Purpose |
| --- | --- | --- |
| `NEOCLIP_BROWSE_ROOTS` | `/host,/media,/exports` | Comma-separated container folders the Settings picker may browse (only ones that exist are shown) |
| `MEDIA_ROOT` | `/media` | Default input folder before Settings is saved |
| `EXPORT_ROOT` | `/exports` | Default output folder before Settings is saved |
| `DATA_ROOT` | `/data` | Settings, queue, and preview cache |

Mount `/host` read-write if you want to delete source videos or create new
folders from the GUI.
