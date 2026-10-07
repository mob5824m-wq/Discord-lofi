# Packaging — 6 native OS packages

Lofi ships as a single-file native binary — no Python needed on the target machine.
There are **6 packages: 1 arm + 1 native for each OS** — wrapped as OS-native installers:

| OS | arch | binary (inside) | package | runner | tool |
|---|---|---|---|---|---|
| Debian / Ubuntu (`linux`) | **amd64** (native) | `lofi-linux-amd64` | `lofi-1.0.0-linux-amd64.deb` | `ubuntu-22.04` | `dpkg-deb` |
| Debian / Ubuntu (`linux`) | **arm64** | `lofi-linux-arm64` | `lofi-1.0.0-linux-arm64.deb` | `ubuntu-24.04-arm` | `dpkg-deb` |
| macOS | **amd64** (Intel) | `lofi-macos-amd64` | `lofi-1.0.0-macos-amd64.dmg` | `macos-13` | `hdiutil` |
| macOS | **arm64** (Apple Silicon) | `lofi-macos-arm64` | `lofi-1.0.0-macos-arm64.dmg` | `macos-14` | `hdiutil` |
| Windows | **amd64** (native) | `lofi-windows-amd64.exe` | `lofi-1.0.0-windows-amd64-setup.exe` | `windows-2022` | `NSIS makensis` |
| Windows | **arm64** | `lofi-windows-arm64.exe` | `lofi-1.0.0-windows-arm64-setup.exe` | `windows-11-arm` | `NSIS makensis` |

Short names without version are also emitted (`lofi-linux-amd64.deb`, `lofi-macos-arm64.dmg`, `lofi-windows-amd64-setup.exe`) for `latest` downloads.

Each binary is a PyInstaller `onefile` build from `lofi.spec`. System deps
(`ffmpeg` + `libopus`) remain external — they are `apt`/`brew`/`winget` packages
so the binary stays ~40–90 MB and avoids LGPL bundling questions. The app probes
them at startup; `lofi --check` reports what to install.

```
# Debian
sudo dpkg -i lofi-1.0.0-linux-amd64.deb && LOFI_TOKEN=... lofi --check
# macOS
open lofi-1.0.0-macos-arm64.dmg  # drag lofi to /usr/local/bin
LOFI_TOKEN=... /usr/local/bin/lofi --check
# Windows (installer)
lofi-1.0.0-windows-amd64-setup.exe  # NSIS installer → C:\Program Files\Lofi\lofi.exe
lofi --check  # or portable: lofi-windows-amd64.exe --check
```

Inside each package is the same single-file binary you could also run directly:
```
LOFI_TOKEN=... ./lofi-linux-amd64 --check   # raw binary, same flags as bot.py
LOFI_TOKEN=... ./lofi-macos-arm64
.\lofi-windows-amd64.exe --check
```

---

## Why 6, and why native runners?

PyInstaller **and** OS packagers are **not cross-compilers**. An amd64 Linux host cannot emit an arm64 `.deb`, and a Linux host cannot emit a macOS `.dmg` via `hdiutil` or a Windows `.exe` via `NSIS`. The only correct way is to build + package each on its native CPU/OS. That is why `.github/workflows/release.yml` has a 6-entry matrix, each on its native runner. A script that tries to cross-compile would silently ship the wrong architecture.

For local development you only build the artifact matching *this* host:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pyinstaller
python scripts/build.py              # → dist/lofi-<os>-<arch> + dist/lofi-*.deb/.dmg/.exe
./dist/lofi --check                  # or ./dist/lofi-linux-amd64 --check
```

To inspect the naming / packaging logic without owning all 6 machines:

```bash
make dist-all   # or: python scripts/make_dist_all.py
# copies the one real binary into all 6 names + 6 packages (.deb/.dmg/.exe) + SHA256SUMS.txt
# placeholder packages contain EMULATED.txt so you don't ship them by accident
ls -lh dist/*.deb dist/*.dmg dist/*.exe
```

---

## Repository layout

```
lofi.spec                    PyInstaller spec (datas, hiddenimports, onefile)
scripts/build.py             host-aware builder: detects os/arch, names artifact,
                             calls PyInstaller, wraps into .deb/.dmg/.exe
scripts/make_dist_all.py     emulates 6 packages from 1 binary (inspection)
.github/workflows/release.yml  6-job matrix + GitHub Release publish
Makefile                     make build / make dist / make dist-all / make check
docs/PACKAGING.md            this file
packaging/                   per-OS notes and templates (Debian, macOS, Windows)
  packaging/debian/            control template, Dockerfile, README
  packaging/macos/             dmg layout + README
  packaging/windows/           NSIS installer.nsi + README
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
- `lofi-<os>-<arch>[.exe]` and `lofi-<version>-<os>-<arch>[.exe]` — raw arch-specific binaries
- `lofi-<version>-<os>-<arch>.deb` / `.dmg` / `.exe` — OS-native packages (binary + `config.example.json` + `README.md` + `EMULATED.txt` when placeholder)
- short packages without version (`lofi-linux-amd64.deb`, …) for `latest` URL
- `SHA256SUMS.txt`
- naming via `python scripts/build.py --name-only` and `--all` to see the matrix

### Reproducing CI locally (Docker / VM)

The closest to CI without owning hardware is to build inside the target OS's
container/VM:

```bash
# Linux amd64 — same as CI's ubuntu-22.04 (real .deb via dpkg-deb)
docker run --rm -v "$PWD:/app" -w /app python:3.11-slim bash -c "
  apt-get update && apt-get install -y --no-install-recommends ffmpeg libopus0 dpkg-dev \
  && pip install -r requirements.txt pyinstaller \
  && python scripts/build.py --clean && ls -lh dist/*.deb"

# Linux arm64 — on an amd64 host this still emits amd64 unless you use an arm64 host or QEMU.
# Use docker buildx with --platform linux/arm64 on an arm64 machine or with QEMU enabled:
docker buildx build --platform linux/arm64 -f packaging/debian/Dockerfile .

# macOS / Windows: must be on that OS. No reliable cross.
# On those runners `hdiutil` (mac) and `makensis` (Windows NSIS) are used to produce
# real .dmg (UDZO) and .exe (PE installer). On Linux they fall back to placeholder
# archives with correct extensions and EMULATED.txt; CI replaces them with real ones.
```

---

## CI / Release — auto +1 on every merge to main

Every push to `main` (i.e. each merged PR) **auto-bumps `VERSION` patch +1, tags, and publishes a Release** with the 6 OS-native packages. Tag pushes and manual dispatches still work.

```bash
# normal flow — merge a PR to main → 1.0.0 → 1.0.1 → Release v1.0.1 with 6 packages
# manual tag (if you need to cut a specific version):
git tag v1.0.1 && git push origin v1.0.1
# manual dispatch with override:
gh workflow run release --ref main -f version=1.2.0 -f bump=minor
```

What the workflow does:

1. `prepare` job (only on `push` to `main` by a human): reads `VERSION`, runs `python scripts/bump_version.py --patch` (or `--minor`/`--major` if commit message contains `bump: minor/major` or `inputs.bump` is set), writes back `VERSION`, commits `chore: bump version to X.Y.Z [skip ci]`, pushes commit + tag `vX.Y.Z` (the `[skip ci]` + `github.actor != 'github-actions[bot]'` guard prevents a loop).
2. `build` job: 6 parallel native runners, each installs system deps, `pip install pyinstaller`, `pyinstaller lofi.spec`, then `python scripts/build.py` which wraps the binary into the OS-native package:
   - Linux: `dpkg-deb --build` → `lofi-*.deb` (`DEBIAN/control` + `usr/local/bin/lofi`)
   - macOS: `hdiutil create -format UDZO` → `lofi-*.dmg` (volume `Lofi <ver>`)
   - Windows: `makensis` (NSIS) → `lofi-*-setup.exe` installer (installs to `Program Files\Lofi`)
   Resolves version as `prepare`'s bump > `inputs.version` > tag > `VERSION` file.
3. Each `build` uploads its binary + package with `actions/upload-artifact`.
4. `release` job (on `main` after bump *or* on tag) downloads all 6, verifies every package exists (`lofi-*.deb`, `lofi-*.dmg`, `*-setup.exe`), generates `SHA256SUMS.txt`, and publishes a GitHub Release via `softprops/action-gh-release` at `vX.Y.Z` (uses `needs.prepare.outputs.sha` as `target_commitish` so the tag points to the bump commit).

Artifacts are also available as workflow artifacts on PRs and non-tag pushes for testing. The `prepare` bump is skipped for pushes by `github-actions[bot]` and for tag pushes, so human tags and manual dispatches don't double-bump.

### Runner image retirement

If GitHub retires e.g. `macos-13`, replace it with the nearest equivalent
(`macos-13-large`, `macos-14`, etc.) — artifact names stay `lofi-macos-amd64.dmg` etc.
The version is the only stable identifier downstream should parse.

---

## Per-OS details

### Debian / Ubuntu — .deb

- System deps: `sudo apt install ffmpeg libopus0` (or `imageio-ffmpeg` pip fallback).
- Binary is dynamically linked against `libopus.so.0` — the usual multiarch path.
  `paths.opus_library()` probes `LD_LIBRARY_PATH`, multiarch dirs, and `LOFI_OPUS`.
- Package: `dpkg-deb` with layout:

  ```
  lofi_1.0.0_amd64.deb
  ├── DEBIAN/control  (Package, Version, Architecture, Depends: ffmpeg, libopus0)
  ├── DEBIAN/postinst (hint: LOFI_TOKEN=... lofi --check)
  └── usr/local/bin/lofi
  └── usr/share/doc/lofi/{README.md,config.example.json}
  ```

  Install:

  ```bash
  sudo dpkg -i lofi-1.0.0-linux-amd64.deb
  sudo apt-get install -f -y   # pull missing Depends if any
  lofi --check
  # arm64 same: lofi-1.0.0-linux-arm64.deb on Raspberry Pi OS / Graviton
  ```

  `dpkg-deb -I lofi-*.deb` and `dpkg -L lofi` show contents. Uninstall: `sudo apt remove lofi`.

See `packaging/debian/` (Dockerfile, control template, README).

### macOS — .dmg

- System deps: `brew install ffmpeg opus`.
- Binary is inside an UDZO-compressed dmg created with `hdiutil`:

  ```
  Lofi 1.0.0.dmg  (volname "Lofi 1.0.0 amd64/arm64")
  └── Lofi/
      ├── lofi  (755)
      ├── README.md
      ├── config.example.json
      └── ReadMe.txt
  ```

  Open in Finder, drag `lofi` to `/usr/local/bin` or `~/bin`, then `LOFI_TOKEN=... lofi --check`.

- Binaries are not code-signed in CI — Gatekeeper will warn on first open.
  Ad-hoc sign locally: `codesign -s - dist/lofi-macos-arm64`.
  For distribution signing, set `CODESIGN_IDENTITY` and extend macOS build with `codesign`.
- Universal2 (`amd64+arm64` lipo) is not produced — two separate dmgs are smaller.
  Pick the one matching `uname -m`.

See `packaging/macos/`.

### Windows — .exe installer (NSIS)

- System deps: `winget install Gyan.FFmpeg` (opus is bundled via `discord.py[voice]`).
  If `--check` reports libopus missing, drop `libopus-0.dll` next to the `.exe`.
- Package: NSIS installer `.exe` (PE) built with `makensis`:

  ```
  lofi-1.0.0-windows-amd64-setup.exe  (installer)
  → C:\Program Files\Lofi\lofi.exe
  + uninstall.exe + README.md + config.example.json
  + registry Uninstall key
  ```

  Run the installer, then `lofi --check` from Start Menu or `C:\Program Files\Lofi`.

- No-zip fallback: unzip is not needed — the `.exe` is the installer. For portable
  use, the raw `lofi-windows-amd64.exe` binary (inside `dist/`) also works as a standalone.

- On Linux, placeholder `.exe` is a zip with `.exe` extension + `EMULATED.txt`; CI on
  `windows-2022` / `windows-11-arm` replaces it with a real NSIS PE.

See `packaging/windows/` (NSIS `installer.nsi` + README).

---

## Versioning and naming

- Source of truth: `VERSION` file.
- Local builds embed it in package names: `lofi-<version>-<os>-<arch>.deb/.dmg/.exe`.
- Plain names without version (`lofi-linux-amd64.deb`, …) are also emitted for
  `curl -LO .../latest/download/lofi-linux-amd64.deb` style installs.
- The binary's `--version` prints the same string (`paths.app_version()` reads
  `VERSION` via `resource_path` — bundled in PyInstaller datas).
- Raw binaries (`lofi-linux-amd64`) and packages (`lofi-1.0.0-linux-amd64.deb`) share the same `VERSION`; `SHA256SUMS.txt` covers both.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `PYINSTALLER not found` | not installed in this venv | `pip install pyinstaller` |
| Binary is wrong arch | cross-compile attempted | build on native runner; see matrix |
| `dpkg-deb: not found` / `hdiutil` missing / `makensis` missing | OS mismatch or tool not installed | On Linux install `dpkg-dev`; macOS has `hdiutil`; Windows install NSIS via `choco install nsis`; CI installs them |
| `ffmpeg was not found` from installed package | system ffmpeg missing | install per-OS deps above, or `pip install imageio-ffmpeg` |
| `Could not find opus library` | libopus missing | `apt install libopus0` / `brew install opus` / drop `libopus-0.dll` |
| macOS Gatekeeper blocks | unsigned dmg/binary | `xattr -dr com.apple.quarantine dist/*.dmg` or `codesign -s -` |
| `EMULATED.txt` in package | package came from `make dist-all` on wrong OS | rebuild that OS/arch natively; emulated packages are not release-grade |
| `dpkg -i` complains about Depends | ffmpeg/libopus not installed | `sudo apt-get install -f` after `dpkg -i` |

---

## Security notes

- Binaries and packages contain no token — pass `LOFI_TOKEN` at runtime.
- `config.json` is written to `LOFI_HOME` / platform state dir with `0600`, never bundled.
- The dashboard bind defaults to `127.0.0.1` even in the installed package; exposing it is explicit.

