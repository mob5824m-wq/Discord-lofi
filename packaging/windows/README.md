# Windows packaging — .exe installer (NSIS)

Installers for `windows-amd64` (`windows-2022` runner) and `windows-arm64`
(`windows-11-arm` runner), built with NSIS `makensis`.

```powershell
winget install Gyan.FFmpeg   # or choco install ffmpeg
# Run the installer (PE, built with NSIS)
.\lofi-1.0.0-windows-amd64-setup.exe   # → C:\Program Files\Lofi\lofi.exe
lofi --check

# Portable: raw binary also works without installer
.\lofi-windows-amd64.exe --check
```

Installer contents (NSIS):
```
C:\Program Files\Lofi\
├── lofi.exe              (PyInstaller onefile)
├── README.md
├── config.example.json
└── uninstall.exe
+ registry Uninstall key + Desktop shortcut
```

`libopus` is bundled via `discord.py[voice]` on Windows. If `--check` reports
missing, drop `libopus-0.dll` next to the `.exe` — `paths.opus_library()` checks
the binary's directory first. Override with `LOFI_OPUS=C:\path\to\libopus-0.dll`.

Built with `scripts/build.py` → `make_windows_installer()` via `makensis` on
Windows; on Linux/macOS a placeholder `.exe` (zip with .exe extension +
EMULATED.txt) is emitted for inspection; CI on Windows produces the real PE
installer. For service install use NSSM or WSL2.

NSIS script is generated at `build/installer-<arch>.nsi` — extend it to add
Start Menu entries, PATH, or signing (`signtool`).
