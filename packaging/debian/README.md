# Debian / Ubuntu packaging — .deb

Debian packages for `linux-amd64` and `linux-arm64`, built with `dpkg-deb`.

```bash
sudo dpkg -i lofi-1.0.0-linux-amd64.deb
sudo apt-get install -f -y   # pulls ffmpeg, libopus0 if missing
lofi --check                  # binary at /usr/local/bin/lofi
LOFI_TOKEN=... lofi           # then lofi

# arm64 (Raspberry Pi, Graviton)
sudo dpkg -i lofi-1.0.0-linux-arm64.deb && sudo apt-get install -f -y
```

Package layout (`dpkg-deb -c`):
```
./usr/local/bin/lofi
./usr/share/doc/lofi/README.md
./usr/share/doc/lofi/config.example.json
./DEBIAN/control  (Package: lofi, Architecture: amd64/arm64, Depends: ffmpeg, libopus0)
```

System deps: `ffmpeg` decodes audio, `libopus0` encodes for voice. Both are
found via `paths.opus_library()` — multiarch dirs, `LD_LIBRARY_PATH`, `LOFI_OPUS`.

Built on `ubuntu-22.04` (amd64) and `ubuntu-24.04-arm` (arm64) via
`scripts/build.py` → `make_deb()` using `dpkg-deb --build`; on other OSes a
placeholder `.deb` (zip with .deb extension + EMULATED.txt) is produced for naming inspection.

Docker:
```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libopus0 dpkg-dev \
 && pip install -r requirements.txt pyinstaller \
 && python scripts/build.py && dpkg-deb -I dist/lofi-*.deb
```
