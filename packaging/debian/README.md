# Debian / Ubuntu packaging

Single-file binary for `linux-amd64` and `linux-arm64`.

```bash
sudo apt update && sudo apt install -y ffmpeg libopus0
tar -xzf lofi-1.0.0-linux-amd64.tar.gz
sudo install -m 0755 lofi-linux-amd64 /usr/local/bin/lofi
LOFI_TOKEN=... lofi --check   # then lofi
```

System deps: `ffmpeg` decodes audio, `libopus0` encodes for voice. Both are
found via the same search `paths.opus_library()` uses locally — multiarch dirs,
`LD_LIBRARY_PATH`, `LOFI_OPUS` override.

*arm64*: same commands — the binary is built on `ubuntu-24.04-arm` (Raspberry Pi
OS is Debian, Graviton is Debian/Ubuntu). `python:3.11-slim` is multi-arch.
