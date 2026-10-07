# macOS packaging — `.dmg` containing a `.pkg`

Two disk images are published, built with `pkgbuild` + `hdiutil` (both ship with
macOS — no Homebrew formula needed):

```bash
brew install ffmpeg opus                    # runtime deps, not bundled
open lofi-macos-arm64.dmg                   # Apple Silicon
open lofi-macos-amd64.dmg                   # Intel
# → run Lofi.pkg inside the image → installs /usr/local/bin/lofi
LOFI_TOKEN=... lofi --check
```

## Image contents

```
Lofi <ver> (arm64).dmg
├── Lofi-<ver>.pkg     pkgbuild component package → /usr/local/bin/lofi
├── portable/lofi      the same binary, to copy anywhere
├── README.md, LICENSE
└── ReadMe.txt         first steps + uninstall notes
```

The `.pkg` payload is `bin/lofi` + `share/doc/lofi/*`, installed under
`/usr/local`, identifier `io.github.mob5824m-wq.lofi`. Its `postinstall`
chmods the binary, clears `com.apple.quarantine` (what an unsigned download
arrives with) and asks once - on a first install - for the bot token, which it
stores in the console user's `~/Library/Application Support/lofi/config.json`,
mode 0600.

## Signing

CI does not sign anything: there is no Apple Developer ID in this repo, so
Gatekeeper asks for confirmation on first open (Control-click → Open rather
than disabling Gatekeeper). To sign a local build:

```bash
codesign -s "Developer ID Application: ..." dist/lofi-macos-arm64
pkgbuild --sign "Developer ID Installer: ..." ...
hdiutil create -format UDZO -sign "Developer ID Application: ..." ...
```

Notarization (`xcrun notarytool`) is the next step for a build you distribute
widely; the `.pkg`-inside-`.dmg` layout is the one notarization expects.

## Uninstall

```bash
sudo rm -f /usr/local/bin/lofi
sudo rm -rf /usr/local/share/doc/lofi
sudo pkgutil --forget io.github.mob5824m-wq.lofi
```

Your data directory (`~/Library/Application Support/lofi`, or `LOFI_HOME`) is
left in place on purpose.

## No universal2

Two images instead of one `lipo`d binary: smaller downloads, better caching, and
each matches `uname -m`.

## Building one

Only on macOS — `hdiutil` does not exist anywhere else, and `scripts/build.py`
fails with that explanation rather than emitting a look-alike file:

```bash
python scripts/build.py
hdiutil verify dist/lofi-*.dmg
python scripts/verify_packages.py dist/
```
