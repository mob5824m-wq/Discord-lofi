# Debian/Ubuntu native build — linux amd64/arm64 artifact
# PyInstaller cannot cross-compile, so build this Dockerfile on the target arch.
# CI builds it twice: ubuntu-22.04 (amd64) and ubuntu-24.04-arm (arm64).
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt pyinstaller

COPY . .
RUN python scripts/build.py --clean \
 && ls -lh dist/ \
 && LOFI_TOKEN=ci-dummy-token LOFI_HOME=/tmp/lofi-home dist/lofi --check || true

# Result is in dist/lofi-linux-<arch>.tar.gz
