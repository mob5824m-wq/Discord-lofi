# Windows packaging — `.exe` installer (NSIS)

Two installers are published, built with NSIS 3 (`makensis`) from
`installer.nsi`:

```powershell
winget install Gyan.FFmpeg                  # runtime dep, not bundled
.\lofi-windows-amd64-setup.exe              # x64
.\lofi-windows-arm64-setup.exe              # ARM64
lofi --check                                # on PATH after install
```

## What the installer does

- installs to `$PROGRAMFILES64\Lofi` (native Program Files on x64 *and* ARM64)
- adds that directory to the machine `PATH`, so `lofi --check` works from any
  prompt, and removes it on uninstall
- Start Menu folder: `Lofi --check`, `Lofi dashboard (demo)`, `Uninstall Lofi`,
  plus a desktop shortcut
- writes `HKLM\Software\Microsoft\Windows\CurrentVersion\Uninstall\Lofi` — a
  real *Apps & features* entry with version, publisher, icon and size
- writes PE version metadata, so Explorer shows `1.0.1` instead of `0.0.0.0`
- refuses to overwrite a running `lofi.exe` with a clear message
- uninstaller deletes the files, the PATH entry, the shortcuts and the registry
  key, and leaves your data directory alone (it holds `config.json` + token)

`ffmpeg` is **not** bundled and **not** downloaded during install: an installer
that fetches things at install time is a supply-chain decision, not a packaging
one. `lofi --check` says what to install instead.

## Build (Windows only)

```powershell
choco install nsis -y
python scripts/build.py
python scripts/verify_packages.py dist/
```

`scripts/build.py` finds `makensis` on `PATH` or in
`C:\Program Files (x86)\NSIS` / `C:\Program Files\NSIS`, and fails with those
instructions instead of shipping something that is not a Windows installer.

## Notes for editing `installer.nsi`

- `$` is NSIS's escape character. The inline PowerShell PATH helpers are written
  without PowerShell variables (`$_`, `$p`) for exactly that reason, inside
  back-quoted strings so the PowerShell single quotes survive. `$$` is how you
  write a literal `$`.
- `Unicode true` requires NSIS 3+; `choco install nsis` provides it.
- Signing: add `!finalize 'signtool sign /tr ... /td sha256 /fd sha256 /a "%1"'`
  when a certificate is available — until then SmartScreen will warn, which is
  normal for unsigned open-source builds.
- Path handling uses `[Environment]::SetEnvironmentVariable('Path', …, 'Machine')`
  rather than `setx`, which truncates PATH at 1024 characters.

## Portable

No installer wanted? The release also ships the raw binary
`lofi-windows-amd64.exe`; it takes the same flags and writes the same data
directory.
