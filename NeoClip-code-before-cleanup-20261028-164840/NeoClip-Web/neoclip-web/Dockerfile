FROM python:3.12-slim-trixie

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# Refuse an FFmpeg build that would silently prevent the full NVIDIA path.
RUN python -c "import subprocess; f=subprocess.check_output(['ffmpeg','-hide_banner','-filters'],stderr=subprocess.STDOUT); e=subprocess.check_output(['ffmpeg','-hide_banner','-encoders'],stderr=subprocess.STDOUT); assert b'scale_cuda' in f and b'h264_nvenc' in e, 'This FFmpeg package lacks scale_cuda or NVENC; use a CUDA-enabled FFmpeg build'"
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/status', timeout=4)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
