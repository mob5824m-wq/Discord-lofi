# Packaging — 6 native artifacts

Lofi ships as a single-file native binary — no Python needed on the target machine.
There are **6 artifacts: 1 arm + 1 native for each OS** (Debian/Linux, macOS, Windows).

| OS | arch | binary | archive | runner |
|---|---|---|---|---|
| Debian / Ubuntu (`linux`) | **amd64** (native) | `lofi-linux-amd64` | `lofi-linux-amd64.tar.gz` | `ubuntu-22.04` |
| Debian / Ubuntu (`linux`) | **arm64** | `lofi-linux-arm64` | `lofi-linux-arm64.tar.gz` | `ubuntu-24.04-arm` |
| macOS | **amd64** (Intel) | `lofi-macos-amd64` | `lofi-macos-amd64.tar.gz` | `macos-13` |
| macOS | **arm64** (Apple Silicon) | `lofi-macos-arm64` | `lofi-macos-arm64.tar.gz` | `macos-14` |
| Windows | **amd64** (native) | `lofi-windows-amd64.exe` | `lofi-windows-amd64.zip` | `windows-2022` |
| Windows | **arm64** | `lofi-windows-arm64.exe` | `lofi-windows-arm64.zip` | `windows-11-arm` |

Each artifact is a PyInstaller `onefile` build from `lofi.spec`. System deps
(`ffmpeg` + `libopus`) remain external — they are `apt`/`brew`/`winget` packages
so the binary stays ~40–90 MB and avoids LGPL bundling questions. The app probes
them at startup; `lofi --check` reports what to install.

```
LOFI_TOKEN=... ./lofi-linux-amd64 --check   # same flags as bot.py
LOFI_TOKEN=... ./lofi-macos-arm64
.\lofi-windows-amd64.exe --check
```

---

## Why 6, and why native runners?

PyInstaller is **not a cross-compiler**. An amd64 host cannot emit an arm64
binary, and a Linux host cannot emit a macOS `.exe`. The only correct way to
produce the 6 is to build each on its native CPU/OS. That is why
`.github/workflows/release.yml` has a 6-entry matrix, each on its native
GitHub-hosted runner. A script that tries to cross-compile would silently ship
the wrong architecture.

For local development you only build the artifact matching *this* host:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pyinstaller
python scripts/build.py              # → dist/lofi-<os>-<arch>
./dist/lofi --check                  # or ./dist/lofi-linux-amd64 --check
```

To inspect the naming / archive logic without owning all 6 machines:

```bash
make dist-all   # or: python scripts/make_dist_all.py
# copies the one real binary into all 6 names + 12 archives + SHA256SUMS.txt
# archives contain EMULATED.txt so you don't ship them by accident
ls -lh dist/
```

---

## Repository layout

```
lofi.spec                    PyInstaller spec (datas, hiddenimports, onefile)
scripts/build.py             host-aware builder: detects os/arch, names artifact,
                             calls PyInstaller, archives (tar.gz/zip)
scripts/make_dist_all.py     emulates 6 archives from 1 binary (inspection)
.github/workflows/release.yml  6-job matrix + GitHub Release publish
Makefile                     make build / make dist / make dist-all / make check
docs/PACKAGING.md            this file
packaging/                   per-OS notes (Debian, macOS, Windows)
```

---

## Local builds

### One-liner

```bash
make venv && make build && make check
# or without make:
python scripts/build.py --clean && python scripts/build.py --check
```

Outputs in `dist/`:

- `lofi` — un-versioned single file kept for `lofi --check`
- `lofi-<os>-<arch>[.exe]` and `lofi-<version>-<os>-<arch>[.exe]` — arch-specific copies
- `lofi-<version>-<os>-<arch>.tar.gz` / `.zip` — release archives (binary + `config.example.json` + `README.md`)
- naming via `python scripts/build.py --name-only` and `--all` to see the matrix

### Reproducing CI locally (Docker / VM)

The closest to CI without owning hardware is to build inside the target OS's
container/VM:

```bash
# Linux amd64 — same as CI's ubuntu-22.04
docker run --rm -v "$PWD:/app" -w /app python:3.11-slim bash -c "
  apt-get update && apt-get install -y --no-install-recommends ffmpeg libopus0 \
  && pip install -r requirements.txt pyinstaller \
  && python scripts/build.py --clean && ls -lh dist/"

# Linux arm64 — on an amd64 host this still emits amd64 unless you use an arm64 host or QEMU.
# Use docker buildx with --platform linux/arm64 on an arm64 machine or with QEMU enabled:
docker buildx build --platform linux/arm64 -f packaging/debian/Dockerfile .

# macOS / Windows: must be on that OS. No reliable cross.
```

---

## CI / Release

Tagging `v*.*.*` triggers the `release` workflow:

```bash
git tag v1.0.1 && git push origin v1.0.1
# or manually: gh workflow run release --ref main -f version=1.0.1
```

Flow:

1. `build` job: 6 parallel runs, each installs system deps, `pip install pyinstaller`,
   `pyinstaller lofi.spec`, renames to `lofi-<os>-<arch>` and archives.
2. Each job uploads its binary + archive with `actions/upload-artifact`.
3. `release` job (only on tag) downloads all 6, verifies that every name is present,
   generates `SHA256SUMS.txt`, and publishes a GitHub Release with
   `softprops/action-gh-release`.

Artifacts are also available as workflow artifacts on non-tag pushes for testing.

### Runner image retirement

If GitHub retires e.g. `macos-13`, replace it with the nearest equivalent
(`macos-13-large`, `macos-14`, etc.) — artifact names stay `lofi-macos-amd64` etc.
The version is the only stable identifier downstream should parse.

---

## Per-OS details

### Debian / Ubuntu

- System deps: `sudo apt install ffmpeg libopus0` (or `imageio-ffmpeg` pip fallback).
- Binary is dynamically linked against `libopus.so.0` — the usual multiarch path.
  `paths.opus_library()` probes `LD_LIBRARY_PATH`, multiarch dirs, and `LOFI_OPUS`.
- Archive: `tar.gz` with executable bit `0755`. To install:
  ```bash
  tar -xzf lofi-1.0.0-linux-amd64.tar.gz
  sudo install -m 0755 lofi-linux-amd64 /usr/local/bin/lofi
  lofi --check
  ```
- `.deb` packaging is intentionally not provided — single-file + tarball covers
  servers and `install`-based deploys without needing `dpkg` dependencies.

See `packaging/debian/`.

### macOS

- System deps: `brew install ffmpeg opus`.
- Binaries are not code-signed in CI — Gatekeeper will warn on first open.
  Ad-hoc sign locally: `codesign -s - dist/lofi-macos-arm64`.
  For distribution signing, set `CODESIGN_IDENTITY` and `APPLE_TEAM_ID` secrets
  and extend the macOS build step with `codesign --deep --force`.
- Universal2 (`amd64+arm64` lipo) is not produced — two separate artifacts are
  smaller and cache better. Users pick the one matching `uname -m`.

See `packaging/macos/`.

### Windows

- System deps: `winget install Gyan.FFmpeg` (opus is bundled via `discord.py[voice]`).
  If `--check` reports libopus missing, drop `libopus-0.dll` next to the `.exe`.
- Archive: `.zip` with the `.exe` flat. No installer — unzip and run:
  ```powershell
  Expand-Archive lofi-1.0.0-windows-amd64.zip -DestinationPath .
  .\lofi-windows-amd64.exe --check
  ```
- For an MSI/InnoSetup installer, wrap the `.exe` — not provided here because
  most Windows deployments of this bot run in WSL2/Linux containers.

See `packaging/windows/`.

---

## Versioning and naming

- Source of truth: `VERSION` file.
- Local builds embed it in archive names: `lofi-<version>-<os>-<arch>.tar.gz`.
- Plain names without version (`lofi-linux-amd64`) are also emitted for
  `curl -LO .../latest/download/lofi-linux-amd64` style installs.
- The binary's `--version` prints the same string (`paths.app_version()` reads
  `VERSION` via `resource_path` — bundled in PyInstaller datas).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `PYINSTALLER not found` | not installed in this venv | `pip install pyinstaller` |
| Binary is wrong arch | cross-compile attempted | build on native runner; see matrix |
| `ffmpeg was not found` from built binary | system ffmpeg missing | install per-OS deps above, or `pip install imageio-ffmpeg` |
| `Could not find opus library` | libopus missing | `apt install libopus0` / `brew install opus` / drop `libopus-0.dll` next to `.exe` |
| macOS Gatekeeper blocks | unsigned binary | `xattr -dr com.apple.quarantine dist/lofi-macos-*` or ad-hoc `codesign -s -` |
| `EMULATED.txt` in archive | archive came from `make dist-all` | rebuild that OS/arch natively; emulated archives are not release-grade |

---

## Security notes

- Binaries contain no token — pass `LOFI_TOKEN` at runtime.
- `config.json` is written to `LOFI_HOME` / platform state dir with `0600`, never bundled.
- The dashboard bind defaults to `127.0.0.1` even in the binary; exposing it is
  an explicit config step.
