# Packaging — 6 native OS packages

Lofi ships as a single-file native binary — no Python needed on the target
machine. Every push to `main` builds **6 artifacts: 1 arm + 1 native for each
OS** on 6 GitHub-hosted runners and publishes them as OS-native installers.

| OS | arch | installer (inside) | runner | tool |
|---|---|---|---|---|
| Debian / Ubuntu (`linux`) | **amd64** | `lofi-<ver>-linux-amd64.deb` | `ubuntu-22.04` | `dpkg-deb` |
| Debian / Ubuntu (`linux`) | **arm64** | `lofi-<ver>-linux-arm64.deb` | `ubuntu-24.04-arm` | `dpkg-deb` |
| macOS | **amd64** (Intel) | `lofi-<ver>-macos-amd64.dmg` (holds `Lofi-<ver>.pkg`) | `macos-15-intel` | `pkgbuild` + `hdiutil` |
| macOS | **arm64** (Apple Silicon) | `lofi-<ver>-macos-arm64.dmg` | `macos-14` | `pkgbuild` + `hdiutil` |
| Windows | **amd64** | `lofi-<ver>-windows-amd64-setup.exe` | `windows-2022` | NSIS `makensis` |
| Windows | **arm64** | `lofi-<ver>-windows-arm64-setup.exe` | `windows-11-arm` | NSIS `makensis` (best effort) |

Each installer is also published under a **short, unversioned name** —
`lofi-linux-amd64.deb`, `lofi-macos-arm64.dmg`, `lofi-windows-amd64-setup.exe` —
so `/releases/latest/download/<name>` is a stable URL for scripts and docs.
The raw PyInstaller binaries (`lofi-linux-amd64`, `lofi-macos-arm64`,
`lofi-windows-amd64.exe`) are published too, for machines where installing
anything is unwanted.

System dependencies stay outside the package (`ffmpeg` + `libopus`) — they are
`apt`/`brew`/`winget` packages, and keeping them out avoids bundling LGPL code
and keeps the binary near 40–90 MB. The app probes them at startup and
`lofi --check` says which one is missing.

```
# Debian / Ubuntu
sudo dpkg -i lofi-linux-amd64.deb && sudo apt-get install -f -y
lofi --check
# macOS — open the dmg, run Lofi.pkg
open lofi-macos-arm64.dmg && LOFI_TOKEN=... lofi --check
# Windows
.\lofi-windows-amd64-setup.exe        # → C:\Program Files\Lofi\lofi.exe, on PATH
lofi --check
```

---

## Why 6, and why native runners?

PyInstaller **and** the OS packagers are **not cross-compilers**. An amd64 Linux
host cannot emit an arm64 `.deb`; no non-Apple host can run `hdiutil`; no
non-Windows host can produce a PE installer. The only correct approach is to
build and package each artifact on a runner whose CPU and OS match, which is
exactly the 6-entry matrix in `.github/workflows/release.yml`.

---

## The no-fakes rule

A host missing `dpkg-deb` / `hdiutil` / `makensis` can still write a file
*called* `lofi-1.0.0-linux-amd64.deb`. It would install nothing and look
identical to a real package on the release page. So:

- `scripts/build.py` **fails** with the command to fix it instead of emitting a
  placeholder. Opting into a fake is explicit: `--allow-placeholder`, used only
  by `scripts/make_dist_all.py` for inspecting names offline.
- `scripts/verify_packages.py` checks the actual bytes before anything is
  published:

  | artifact | check |
  |---|---|
  | `.deb` | `ar` magic `!<arch>`, `dpkg-deb -f` reports `Package: lofi`, `dpkg-deb -c` contains `./usr/local/bin/lofi` |
  | `.dmg` | UDIF `koly` footer 512 bytes from the end, `hdiutil verify` |
  | `.exe` | `MZ` + `PE\0\0` header, and the PE machine type matches the arch in the file name |
  | any | not a zip/tar/gzip, and no `EMULATED.txt` inside |

  Both the per-OS build job and the release job run it; a failure fails the
  release. Locally: `python scripts/verify_packages.py dist/`.

---

## Repository layout

```
lofi.spec                     PyInstaller spec (datas, hiddenimports, onefile)
scripts/build.py              host-aware build: PyInstaller + OS-native package
scripts/verify_packages.py    byte-level gate: is this artifact what it claims?
scripts/make_dist_all.py      copies one binary into all 6 names, clearly marked
                              as emulated — local name inspection only
.github/workflows/release.yml 6-runner matrix + GitHub Release publish
Makefile                      make build / dist / dist-all / check / verify

packaging/debian/             real .deb templates, rendered by build.py
  control                       @VERSION@ @ARCH@ @INSTALLED_SIZE@
  postinst                      chmod, create /etc/lofi/lofi.env, daemon-reload
  prerm                         stop the service before removal
  postrm                        purge /etc/lofi/lofi.env (purge only)
  lofi.service                  systemd unit (installed, NOT enabled)
  lofi.env.example              where the bot token goes for the service
  lofi.1                        man page
  copyright                     machine-readable copyright
packaging/macos/
  ReadMe.txt.in                 instructions shown inside the mounted image
packaging/windows/
  installer.nsi                 NSIS 3 script: Program Files, PATH, shortcuts,
                                Start Menu, "Apps & features" entry, uninstaller
```

---

## Per-OS details

### Debian / Ubuntu — `.deb`

Built with `dpkg-deb --build --root-owner-group -Zxz`:

```
lofi_1.0.1_amd64.deb
├── DEBIAN/control             Package, Version, Architecture, Depends
├── DEBIAN/postinst|prerm|postrm
├── usr/local/bin/lofi
├── usr/share/lofi/{lofi.env,lofi.service,config.example.json}
├── usr/share/doc/lofi/{README.md,LICENSE,config.example.json,copyright}
│   └── examples/{lofi.env,lofi.service}
├── usr/share/man/man1/lofi.1.gz
└── lib/systemd/system/lofi.service
```

`Depends: ffmpeg, libopus0, ca-certificates` — the binary is self-contained, so
**python3 is deliberately not required**. The unit is installed **disabled**:
the bot needs a token before it can do anything, and starting it from the
package would only produce a crash loop.

```bash
sudo dpkg -i lofi-linux-amd64.deb
sudo apt-get install -f -y          # pulls ffmpeg / libopus0 if missing
sudo editor /etc/lofi/lofi.env     # put your token here
lofi --check
sudo systemctl enable --now lofi    # optional
sudo apt remove lofi                # uninstall (keeps your data directory)
```

The env example is installed to **`/usr/share/lofi/`**, not `/usr/share/doc/`:
slim Debian/Ubuntu images configure dpkg with `path-exclude=/usr/share/doc/*`,
and the postinst needs it so `systemctl enable lofi` has somewhere to read a
token from.

### macOS — `.dmg` containing a `.pkg`

`pkgbuild` builds a real component package (payload `bin/lofi` +
`share/doc/lofi/*`, installed under `/usr/local`), and `hdiutil create -format
UDZO` wraps it in the disk image:

```
Lofi 1.0.1 (arm64).dmg
├── Lofi-1.0.1.pkg     double-click installer → /usr/local/bin/lofi
├── portable/lofi      the same binary, to copy anywhere
├── README.md, LICENSE
└── ReadMe.txt         what to do first (brew install ffmpeg opus)
```

The package's `postinstall` chmods the binary and clears the quarantine
attribute an unsigned download arrives with, so a first run does not need
`xattr -dr com.apple.quarantine` by hand. CI does not sign the binaries — no
Apple Developer ID in this repo — so Gatekeeper will still ask for
confirmation on first open (Control-click → Open).

Uninstall:

```bash
sudo rm -f /usr/local/bin/lofi
sudo rm -rf /usr/local/share/doc/lofi
sudo pkgutil --forget io.github.mob5824m-wq.lofi
```

### Windows — `.exe` (NSIS)

`packaging/windows/installer.nsi` is a real NSIS 3 script, compiled with
`makensis` (installed by `choco install nsis` in CI):

- installs to `$PROGRAMFILES64\Lofi` (native Program Files on both x64 and ARM64)
- adds the install directory to the **machine PATH**, so `lofi --check` works
  from any prompt, and removes it again on uninstall
- Start Menu folder (`Lofi --check`, `Lofi dashboard (demo)`, `Uninstall Lofi`)
  and a desktop shortcut
- registry entry under `Software\Microsoft\Windows\CurrentVersion\Uninstall\Lofi`
  → a proper *Apps & features* entry with version, publisher, icon and size
- refuses to overwrite a running `lofi.exe` with a clear message
- uninstaller removes the files, the PATH entry, the shortcuts and the registry
  key, and **leaves your data directory alone** (it holds `config.json` with
  your token)

`ffmpeg` is not bundled and not downloaded by the installer: `winget install
Gyan.FFmpeg`, and `lofi --check` will say so if it is missing.

---

## CI / release flow

1. **`prepare`** (push to `main` by a human only) — bumps `VERSION` patch +1 via
   `scripts/bump_version.py`, commits `chore: bump version to X.Y.Z [skip ci]`,
   pushes the commit and the tag `vX.Y.Z`. The `[skip ci]` marker plus the
   `github.actor != 'github-actions[bot]'` guard stop the loop.
2. **`build`** — 6 parallel jobs, each installing its OS tools, running
   `python scripts/build.py` (one PyInstaller run + one packaging run) and then
   `scripts/verify_packages.py`. Failures print the build-log tail as `::error::`
   annotations so they are visible without opening the log.
3. **`release`** — downloads everything, re-verifies them with
   `scripts/verify_packages.py`, writes `SHA256SUMS.txt`, and publishes the
   GitHub Release with install instructions. Windows ARM64 is marked
   `continue-on-error` in the matrix: if its toolchain cannot install the Python
   dependencies, the other five still ship and the missing installer is reported
   instead of blocking the release.

```bash
# merge a PR to main  → 1.0.1 → 1.0.2 → Release v1.0.2 with 6 installers
git tag v1.2.0 && git push origin v1.2.0          # cut a specific version
gh workflow run release --ref main -f version=1.3.0 -f bump=minor
```

### Runner image retirement

If GitHub retires an image, replace it with the nearest equivalent. The Intel
macOS entry already had to move once: `macos-13` was retired on 2025-12-04, and
jobs asking for it are never picked up — they sit queued until the run is
cancelled, which looks like runner capacity from the outside. It is now
`macos-15-intel`. Artifact **names** stay stable; the version string is the only
identifier downstream should parse.

---

## Local builds

```bash
make venv && make build && make check     # or:
python scripts/build.py --clean && python scripts/build.py --check
python scripts/verify_packages.py dist/
```

You only ever build the artifact matching this host — that is the point. Outputs
in `dist/`:

- `lofi` — un-versioned binary, kept so `./dist/lofi --check` always works
- `lofi-<os>-<arch>[.exe]` and `lofi-<ver>-<os>-<arch>[.exe]` — raw binaries
- `lofi-<ver>-<os>-<arch>.deb|.dmg|-setup.exe` and their short names — installers

If the tools are missing, `build.py` tells you which command to run:

```
Packaging failed: makensis not found, so a real Windows installer cannot be built here.
  Windows: choco install nsis   (or download from nsis.sourceforge.io)
  Everywhere else: let GitHub Actions build it (release.yml, windows-2022 / windows-11-arm runners).
```

To see all six names without owning six machines:

```bash
python scripts/build.py --all          # the matrix and its runners
make dist-all                          # copies one binary into all 6 names,
                                       # marked EMULATED — never ship these
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `PyInstaller not found` | not installed in the venv | `pip install pyinstaller` |
| `Python shared library (libpython3.11.so.1.0) not found` | Python built without a shared lib | `apt install libpython3.11`, or use `actions/setup-python` (CI is fine) |
| `dpkg-deb not available` | not Debian/Ubuntu | `apt install dpkg-dev`, or build in CI |
| `makensis not found` | NSIS not installed | `choco install nsis`; `build.py` also looks in `C:\Program Files (x86)\NSIS` |
| `no UDIF 'koly' footer` in verification | the file is a renamed archive | build on macOS; do not pass `--allow-placeholder` for a release |
| Binary is the wrong arch | cross-compile attempted | build on a native runner |
| `ffmpeg was not found` after installing | system dep missing | `apt install ffmpeg` / `brew install ffmpeg` / `winget install Gyan.FFmpeg`, or `pip install imageio-ffmpeg` |
| `Could not find opus library` | libopus missing | `apt install libopus0` / `brew install opus` / drop `libopus-0.dll` (or set `LOFI_OPUS=`) |
| macOS Gatekeeper blocks it | unsigned binary | Control-click → Open, or `xattr -dr com.apple.quarantine /usr/local/bin/lofi` |
| `dpkg -i` complains about Depends | ffmpeg/libopus missing | `sudo apt-get install -f` after `dpkg -i` |
| `lofi: command not found` after the Windows installer | PATH picked up in new shells only | open a new terminal; the installer broadcasts `WM_SETTINGCHANGE` |

---

## Security notes

- No token in any artifact: pass `LOFI_TOKEN` at runtime or put it in
  `/etc/lofi/lofi.env` (mode 0600, created by `postinst`).
- `config.json` is written to `LOFI_HOME` / the platform state dir with `0600`,
  never bundled.
- The dashboard binds to `127.0.0.1` even when installed; exposing it is
  explicit.
- CI artifacts are unsigned. macOS Gatekeeper and Windows SmartScreen will warn;
  that is expected for an unsigned open-source build.
