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

## License

NeoClip is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE).

- **Free for noncommercial use.** Personal use, hobby projects, study, and use by schools, charities, and other nonprofit or public organizations are allowed. You may fork, modify, and share it for those purposes.
- **No commercial use without permission.** Selling NeoClip, offering it as a paid service, or using it in a for-profit business requires a separate commercial license from the maintainer. Open an [issue](https://github.com/TE99SJM/Neoclip/issues) to ask.
- Keep the `LICENSE` file and its `Required Notice:` line with any copy you share.

NeoClip is source-available, not OSI open source, because it restricts commercial use.
