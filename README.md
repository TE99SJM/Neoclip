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

Copyright (C) 2026 TE99SJM

NeoClip is free, open-source software licensed under the [GNU Affero General Public License v3.0](LICENSE) (AGPLv3).

- **Use it for anything, including paid work.** Individuals, schools, and businesses can use NeoClip, including to make money from the videos they produce.
- **Changes have to stay open.** If you share a modified NeoClip, or run a modified version as a website or service other people use, you must make your full source code available to them under the AGPLv3. You can't turn NeoClip into a closed, private product.
- **Keep the notices.** Keep the `LICENSE` file and copyright notices, mark what you changed, and keep the link to the source code in the app.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the [LICENSE](LICENSE) for details.
