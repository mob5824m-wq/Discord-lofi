# macOS packaging — .dmg

Disk images for `macos-amd64` (Intel, `macos-13` runner) and `macos-arm64`
(Apple Silicon, `macos-14` runner), built with `hdiutil` (UDZO).

```bash
brew install ffmpeg opus
open lofi-1.0.0-macos-arm64.dmg        # or lofi-1.0.0-macos-amd64.dmg on Intel
# drag `lofi` to /usr/local/bin (or ~/bin)
LOFI_TOKEN=... lofi --check

# alternative: raw binary inside dmg
# hdiutil attach lofi-*.dmg && cp /Volumes/Lofi/lofi /usr/local/bin/
```

Contents inside dmg (`hdiutil attach` → `/Volumes/Lofi 1.0.0`):
```
Lofi/
├── lofi                 (755, PyInstaller onefile)
├── README.md
├── config.example.json
└── ReadMe.txt
```

Gatekeeper: CI binaries/dmg are unsigned. On first open:
```bash
xattr -dr com.apple.quarantine lofi-macos-arm64
xattr -dr com.apple.quarantine lofi-1.0.0-macos-arm64.dmg
# or ad-hoc sign after local build:
codesign -s - dist/lofi-macos-arm64
hdiutil create uses UDZO; for signed DMG add --sign
```

No universal2 — two dmgs are smaller and cache better. Pick matching `uname -m`.

Built with `scripts/build.py` → `make_dmg()` via `hdiutil create -format UDZO`;
on Linux a placeholder `.dmg` (tar.gz with .dmg extension + EMULATED.txt) is
emitted for inspection; CI on macOS produces the real dmg.
