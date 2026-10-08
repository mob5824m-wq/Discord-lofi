# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Lofi — builds a single-file native binary.

The spec bundles:
  - bot.py (entry point) and every first-party module
  - dashboard.html, config.example.json, VERSION — resources that paths.resource_path()
    resolves at runtime (it searches bundle_dir() when frozen, so they must be bundled)
  - hiddenimports: discord.py voice chain, aiohttp, yt-dlp, numpy etc. PyInstaller
    misses some dynamic imports (e.g. yt_dlp extractors imported via __import__).

Usage:
  pyinstaller lofi.spec --noconfirm
  ./dist/lofi --check
  ./dist/lofi --version

For the 6 release artifacts (debian/mac/windows × amd64/arm64) see:
  .github/workflows/release.yml  — CI builds each on a native runner
  scripts/build.py               — local wrapper that renames the binary per target
  Makefile                       — `make artifacts` etc.

Single-file (`onefile`) is intentional: Discord bots are often deployed as one binary
with the token passed via env var, mirroring the Docker story. The data directory
(LOFI_HOME / platform state dir) stays external so config/db survive upgrades.

NOTE: ffmpeg and libopus are NOT bundled — they remain system dependencies
(apt/brew/winget) or the pip fallback imageio-ffmpeg. That keeps the binary small
(~80MB) and avoids LGPL/static-linking questions. The app probes them at startup
and --check reports what to install.
"""

from pathlib import Path

block_cipher = None

# ---- datas: shipped resources that must live beside the binary at runtime
# paths.resource_path() searches bundle_dir() / app_dir() / source_dir() and
# well-known share dirs. In a frozen build bundle_dir() == _MEIPASS, so we place
# them at the top level of the bundle.
datas = [
    ("dashboard.html", "."),
    ("config.example.json", "."),
    ("VERSION", "."),
    ("LICENSE", "."),
    ("README.md", "."),
    ("music/README.md", "music"),
]

# Only include dashboard.html etc if they exist (dev checkout vs isolated build env)
datas = [(src, dst) for (src, dst) in datas if Path(src).exists()]

# ---- hidden imports: modules imported dynamically that PyInstaller would miss
hiddenimports = [
    # core runtime
    "discord",
    "discord.ext.commands",
    "discord.opus",
    "aiohttp",
    "aiohttp.web",
    "yarl",
    "multidict",
    "frozenlist",
    # voice crypto. PyNaCl's compiled `_sodium` extension imports CFFI's
    # `_cffi_backend` dynamically; bundle both explicitly or discord.py treats
    # PyNaCl as absent in the frozen binary (voice is then disabled).
    "nacl",
    "nacl.secret",
    "nacl.bindings",
    "nacl._sodium",
    "_cffi_backend",
    # station resolution / generative
    "yt_dlp",
    "yt_dlp.extractor",
    "numpy",
    # packaging / stdlib dynamic shims
    "audioop",
    "audioop_lts",
    "ctypes.util",
    "imageio_ffmpeg",
    # first-party modules (explicit, though Analysis usually finds them)
    "bot",
    "command_tree",
    "dashboard",
    "demo",
    "generative",
    "music",
    "paths",
    "permissions",
    "player",
    "settings",
    "sources",
    "stations",
    "store",
]

# Modules to exclude (shrink)
excludes = [
    "tkinter",
    "tcl",
    "tk",
    "IPython",
    "jupyter",
    "notebook",
    "matplotlib",
]

a = Analysis(
    ["bot.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Remove duplicate data entries
# (PyInstaller warns if the same file is listed twice)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="lofi",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX breaks on macOS signing / older Debian; omit for reproducibility
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # console app: --check / --demo / logging goes to stdout
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,  # let PyInstaller infer from the host; CI matrix pins runner arch
    codesign_identity=None,
    entitlements_file=None,
    # On macOS the .app bundle wrapper is not needed; we ship a single binary
    # so that `lofi` works the same on all three OSes.
    icon=None,
)
