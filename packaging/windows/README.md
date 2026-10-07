# Windows packaging

Binaries for `windows-amd64` (`windows-2022` runner) and `windows-arm64`
(`windows-11-arm` runner).

```powershell
winget install Gyan.FFmpeg   # or choco install ffmpeg
Expand-Archive lofi-1.0.0-windows-amd64.zip -DestinationPath .
.\lofi-windows-amd64.exe --check
```

`libopus` is usually bundled via `discord.py[voice]` on Windows. If `--check`
reports it missing, download `libopus-0.dll` and place it next to the `.exe` —
`paths.opus_library()` checks the binary's directory first. Override with
`LOFI_OPUS=C:\path\to\libopus-0.dll`.

No installer is provided — unzip and run. For service install use
NSSM or run in WSL2 as a Linux binary.
