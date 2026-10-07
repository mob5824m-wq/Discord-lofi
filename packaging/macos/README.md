# macOS packaging

Binaries for `macos-amd64` (Intel, `macos-13` runner) and `macos-arm64` (Apple
Silicon, `macos-14` runner). No universal2 — two artifacts are smaller and
cache better.

```bash
brew install ffmpeg opus
tar -xzf lofi-1.0.0-macos-arm64.tar.gz   # or lofi-macos-amd64.tar.gz on Intel
chmod +x lofi-macos-arm64
LOFI_TOKEN=... ./lofi-macos-arm64 --check
```

Gatekeeper: CI binaries are unsigned. On first open:
```bash
xattr -dr com.apple.quarantine lofi-macos-arm64
# or ad-hoc sign after local build:
codesign -s - lofi-macos-arm64
```
For distribution signing, add your Developer ID to CI and extend the macOS build
step with `codesign --deep --force --options runtime --sign "$IDENTITY"`.

Pick the binary matching `uname -m` (x86_64 → amd64, arm64 → arm64).
