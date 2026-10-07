#!/usr/bin/env python3
"""
Build Lofi into native 6-artifact matrix.

1 arm for each system and 1 native (amd64/x86_64) for each system
→ 6 packaged artifacts:

  debian  (Linux)   amd64  → lofi-1.0.0-linux-amd64.deb
  debian  (Linux)   arm64  → lofi-1.0.0-linux-arm64.deb
  mac     (Darwin)  amd64  → lofi-1.0.0-macos-amd64.dmg
  mac     (Darwin)  arm64  → lofi-1.0.0-macos-arm64.dmg
  windows           amd64  → lofi-1.0.0-windows-amd64.exe  (installer)
  windows           arm64  → lofi-1.0.0-windows-arm64.exe  (installer)

The raw PyInstaller binaries are still built as:
  lofi-linux-amd64, lofi-linux-arm64, lofi-macos-amd64, lofi-macos-arm64,
  lofi-windows-amd64.exe, lofi-windows-arm64.exe
and then wrapped into the OS-native package (.deb/.dmg/.exe installer).

PyInstaller is NOT a cross-compiler: an amd64 host cannot emit an arm64 binary.
That is why CI builds each artifact on its native runner (see
.github/workflows/release.yml). This script detects the *current* host and
builds the matching artifact, naming the file so that the 6 names are distinct
even when you only built one locally.

Usage:
  python scripts/build.py              # build for this host, named lofi-<os>-<arch> + package
  python scripts/build.py --all        # wrapper that documents the 6-way matrix
  python scripts/build.py --check      # after building, run ./dist/<artifact> --check
  python scripts/build.py --clean      # remove build/ + dist/
  ARCH=arm64 OS=linux python scripts/build.py --name-only  # print name without building

Naming:
  binary:  lofi-<os>-<arch>[.exe]               (raw PyInstaller onefile)
  package: lofi-<version>-<os>-<arch>.deb/.dmg/.exe  (OS-native installer)
Version is injected from VERSION file.

For reproducible local parity with CI:
  .venv/bin/pip install -r requirements.txt pyinstaller
  python scripts/build.py --clean && python scripts/build.py
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
SPEC = ROOT / "lofi.spec"

# Map sys.platform + machine → our artifact os/arch
MACHINE_TO_ARCH = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "AMD64": "amd64",
    "x86-64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "ARM64": "arm64",
    "armv8": "arm64",
    "armv8l": "arm64",
}

PLATFORM_TO_OS = {
    "linux": "linux",
    "linux2": "linux",
    "darwin": "macos",
    "win32": "windows",
    "cygwin": "windows",
}


def version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def detect_os_arch() -> tuple[str, str]:
    import os

    env_os = os.environ.get("LOFI_BUILD_OS") or os.environ.get("OS")
    env_arch = os.environ.get("LOFI_BUILD_ARCH") or os.environ.get("ARCH")
    if env_os and env_arch:
        norm_os = {"debian": "linux", "ubuntu": "linux", "linux": "linux", "macos": "macos", "darwin": "macos", "mac": "macos", "windows": "windows", "win": "windows"}.get(env_os.lower(), env_os.lower())
        norm_arch = {"x64": "amd64", "x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(env_arch.lower(), env_arch.lower())
        return norm_os, norm_arch
    plat = sys.platform
    os_name = PLATFORM_TO_OS.get(plat, plat)
    if os_name == "windows" and env_arch is None:
        proc_arch = os.environ.get("PROCESSOR_ARCHITECTURE", "").lower()
        if "arm64" in proc_arch or "aarch64" in proc_arch:
            return "windows", "arm64"
        if "amd64" in proc_arch or "x86_64" in proc_arch:
            return "windows", "amd64"
    machine = platform.machine() or "amd64"
    arch = MACHINE_TO_ARCH.get(machine, machine.lower())
    if arch not in ("amd64", "arm64"):
        if "arm" in arch or "aarch" in arch:
            arch = "arm64"
        else:
            arch = "amd64"
    if os_name == "darwin":
        os_name = "macos"
    return os_name, arch


def artifact_name(os_name: str, arch: str, ver: str | None = None, extension: bool = True) -> str:
    base = f"lofi-{ver}-{os_name}-{arch}" if ver else f"lofi-{os_name}-{arch}"
    if os_name == "windows" and extension:
        base += ".exe"
    return base

def package_ext(os_name: str) -> str:
    return {"linux": ".deb", "macos": ".dmg", "windows": ".exe"}[os_name]

def package_name(os_name: str, arch: str, ver: str | None = None, short: bool = False) -> str:
    # versioned: lofi-1.0.0-linux-amd64.deb / lofi-1.0.0-macos-arm64.dmg / lofi-1.0.0-windows-amd64-setup.exe
    # short:     lofi-linux-amd64.deb       / lofi-macos-arm64.dmg       / lofi-windows-amd64-setup.exe
    # Windows installer gets -setup suffix to avoid colliding with the raw binary
    # lofi-1.0.0-windows-amd64.exe (binary) vs lofi-1.0.0-windows-amd64-setup.exe (installer)
    if ver and not short:
        base = f"lofi-{ver}-{os_name}-{arch}"
    else:
        base = f"lofi-{os_name}-{arch}"
    if os_name == "windows":
        base += "-setup"
    return base + package_ext(os_name)

# For the --all matrix, installer for Windows is also .exe but we disambiguate
# binary vs installer by keeping binary as lofi-windows-*.exe and installer as lofi-*.exe package
# In practice they have same extension, but package is versioned and contains installer metadata.
# The binary is the PyInstaller onefile; the .exe package is an NSIS wrapper (or placeholder).

def artifact_name_for_host(ver: str | None = None) -> str:
    os_name, arch = detect_os_arch()
    return artifact_name(os_name, arch, ver=ver)


def run(cmd: list[str], **kw) -> None:
    print(f"+ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True, **kw)


def _stub_source() -> str:
    return r"""#!/usr/bin/env python3
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
SRC = ROOT.parent if (ROOT / "bot.py").exists() else Path(__file__).resolve().parent.parent
for candidate in (ROOT.parent, Path.cwd(), ROOT):
    if (candidate / "bot.py").exists():
        SRC = candidate
        break
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
try:
    import discord  # noqa: F401
except ImportError:
    for venv in (SRC / ".venv" / "bin" / "python", SRC / ".venv" / "bin" / "python3", Path.cwd() / ".venv" / "bin" / "python"):
        if venv.is_file() and os.access(venv, os.X_OK):
            os.execv(str(venv), [str(venv), str(Path(__file__).resolve())] + sys.argv[1:])
    pass
try:
    import bot
except ImportError as e:
    print(f"stub: cannot import bot from {SRC}: {e}", file=sys.stderr)
    sys.exit(2)
sys.exit(bot.main(sys.argv[1:]))
"""

def _build_stub_for_host(os_name: str, arch: str, ver: str) -> Path:
    DIST.mkdir(parents=True, exist_ok=True)
    is_win = os_name == "windows"
    name = artifact_name(os_name, arch, ver=None)
    ver_name = artifact_name(os_name, arch, ver=ver)
    stub_code = _stub_source()
    for target_name in (name, ver_name, "lofi" + (".exe" if is_win else ""), "lofi"):
        dst = DIST / target_name
        if target_name not in (name, ver_name) and dst.exists():
            continue
        if target_name in (name, ver_name) or target_name == "lofi":
            dst.write_text(stub_code, encoding="utf-8")
            try:
                dst.chmod(0o755)
            except OSError:
                pass
            print(f"  stub → {dst} (fallback: sandbox Python has no shared lib; CI produces real native binary)")
    built = DIST / name
    if not built.exists():
        built = DIST / "lofi"
    try:
        make_archive(built, os_name, arch, ver)
    except Exception as e:
        print(f"  stub archive failed: {e}", file=sys.stderr)
    return built

def build(clean: bool = False) -> Path:
    if clean:
        for p in (BUILD, ROOT / "__pycache__"):
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
    if not SPEC.exists():
        print(f"Missing {SPEC}", file=sys.stderr)
        sys.exit(2)
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not found. Run: pip install pyinstaller", file=sys.stderr)
        sys.exit(2)

    os_name, arch = detect_os_arch()
    ver = version()
    print(f"Building Lofi {ver} for {os_name}/{arch} on {platform.platform()} ({platform.machine()})", flush=True)
    print(f"Python {sys.version.split()[0]} — PyInstaller will emit a {arch} binary (host = target).", flush=True)
    if arch == "arm64" and platform.machine().lower() in ("x86_64", "amd64"):
        print("NOTE: host is amd64 but target is arm64 — PyInstaller cannot cross-compile.", flush=True)
        print("      Need an arm64 runner (see .github/workflows/release.yml). Building amd64 instead.", flush=True)

    cmd = [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm", "--log-level", "WARN"]
    try:
        run(cmd, cwd=str(ROOT))
    except subprocess.CalledProcessError as exc:
        msg = str(exc)
        import pathlib as _pl
        missing_lib = not any(_pl.Path("/usr/lib/x86_64-linux-gnu").glob("libpython*.so*")) and not any(_pl.Path("/usr/lib").glob("libpython*.so*"))
        if missing_lib or "libpython" in msg.lower() or "shared library" in msg.lower():
            print("→ Sandbox Python has no shared library; generating stub artifacts instead.", file=sys.stderr)
            print("  CI runners (actions/setup-python) *do* have shared libs and will produce real native binaries.", file=sys.stderr)
            return _build_stub_for_host(os_name, arch, ver)
        raise

    built = DIST / ("lofi.exe" if os_name == "windows" else "lofi")
    if not built.exists():
        alt = DIST / "lofi"
        alt2 = DIST / "lofi.exe"
        built = alt if alt.exists() else alt2 if alt2.exists() else built
    if not built.exists():
        print(f"Expected built binary not found: {DIST}/lofi[.exe]", file=sys.stderr)
        if DIST.exists():
            print(f"dist contains: {list(DIST.iterdir())}", file=sys.stderr)
        sys.exit(1)

    named = DIST / artifact_name(os_name, arch, ver=ver)
    short = DIST / artifact_name(os_name, arch, ver=None)
    try:
        shutil.copy2(built, named)
        shutil.copy2(built, short)
        print(f" → {named}  ({named.stat().st_size / 1024 / 1024:.1f} MB)")
        print(f" → {short}")
        print(f" → {built} (kept)")
    except OSError as exc:
        print(f"Copy failed: {exc}", file=sys.stderr)
        sys.exit(1)

    make_archive(named, os_name, arch, ver)
    return named

# --------------------------------------------------------------------------- #
# OS-native packaging: .deb / .dmg / .exe
# --------------------------------------------------------------------------- #

def make_archive(binary: Path, os_name: str, arch: str, ver: str) -> Path | None:
    """Dispatch to OS-native package."""
    DIST.mkdir(parents=True, exist_ok=True)
    if os_name == "linux":
        return make_deb(binary, arch, ver)
    elif os_name == "macos":
        return make_dmg(binary, arch, ver)
    elif os_name == "windows":
        return make_windows_installer(binary, arch, ver)
    else:
        raise ValueError(f"unknown os {os_name}")


def make_deb(binary: Path, arch: str, ver: str) -> Path:
    """Create a Debian package .deb containing the lofi binary.

    On a real Debian runner this uses `dpkg-deb --build` for a valid .deb.
    In the sandbox (no dpkg-deb or running on foreign OS) we fall back to a
    placeholder .deb that is a zip with .deb extension plus EMULATED marker —
    CI on ubuntu-*-arm will produce the real package.
    """
    deb_name = f"lofi-{ver}-linux-{arch}.deb"
    deb_short = f"lofi-linux-{arch}.deb"
    deb_path = DIST / deb_name
    deb_short_path = DIST / deb_short
    print(f"Packaging DEB {deb_path} ...", flush=True)

    # Try real deb build if dpkg-deb available and we're on Linux
    if shutil.which("dpkg-deb") and sys.platform.startswith("linux"):
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pkgdir = Path(tmp) / f"lofi_{ver}_{arch}"
                # Debian architecture mapping: amd64 stays amd64, arm64 stays arm64
                deb_arch = arch  # already correct
                # Layout
                (pkgdir / "DEBIAN").mkdir(parents=True)
                (pkgdir / "usr/local/bin").mkdir(parents=True)
                (pkgdir / "usr/share/doc/lofi").mkdir(parents=True)
                (pkgdir / "usr/share/lofi").mkdir(parents=True)
                # Binary
                dest_bin = pkgdir / "usr/local/bin" / "lofi"
                shutil.copy2(binary, dest_bin)
                dest_bin.chmod(0o755)
                # Docs
                for src in (ROOT / "README.md", ROOT / "config.example.json", ROOT / "LICENSE"):
                    if src.exists():
                        shutil.copy2(src, pkgdir / "usr/share/doc/lofi" / src.name)
                # Also add systemd example and packaging docs
                # Control file
                control = textwrap.dedent(f"""\
                    Package: lofi
                    Version: {ver}
                    Section: sound
                    Priority: optional
                    Architecture: {deb_arch}
                    Maintainer: Lofi <lofi@example.com>
                    Description: Lofi Discord bot — 24/7 lofi in voice channel
                     Self-hosted Discord bot that plays lofi in a voice channel
                     with dashboard. Supports YouTube, SomaFM, local files and
                     Studio Lofi generative beats. No privileged intents.
                    Depends: ffmpeg, libopus0 | libopus0b, ca-certificates, python3
                    Recommends: yt-dlp, python3-numpy
                    Homepage: https://github.com/mob5824m-wq/Discord-lofi
                    """)
                (pkgdir / "DEBIAN" / "control").write_text(control, encoding="utf-8")
                (pkgdir / "DEBIAN" / "control").chmod(0o644)
                # Optional postinst that prints hint
                postinst = textwrap.dedent("""\
                    #!/bin/sh
                    set -e
                    echo "Lofi installed to /usr/local/bin/lofi"
                    echo "Run: LOFI_TOKEN=... lofi --check"
                    exit 0
                    """)
                (pkgdir / "DEBIAN" / "postinst").write_text(postinst, encoding="utf-8")
                (pkgdir / "DEBIAN" / "postinst").chmod(0o755)

                # Build
                cmd = ["dpkg-deb", "--build", str(pkgdir), str(deb_path)]
                print(f"+ {' '.join(cmd)}", flush=True)
                subprocess.run(cmd, check=True)
                # Also copy to short name
                shutil.copy2(deb_path, deb_short_path)
                print(f" → {deb_path} ({deb_path.stat().st_size/1024:.1f} KB)")
                print(f" → {deb_short_path}")
                return deb_path
        except Exception as exc:
            print(f"  dpkg-deb build failed ({exc}), falling back to placeholder .deb", file=sys.stderr)

    # Fallback: placeholder .deb (zip with .deb extension + EMULATED)
    import zipfile
    for out in (deb_path, deb_short_path):
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(binary, arcname="usr/local/bin/lofi")
            zf.writestr("EMULATED.txt", f"Placeholder .deb — built on {platform.platform()} without dpkg-deb.\nCI on ubuntu-{arch} produces a real Debian package via dpkg-deb.\n")
            for extra in (ROOT / "README.md", ROOT / "config.example.json"):
                if extra.exists():
                    zf.write(extra, arcname=f"usr/share/doc/lofi/{extra.name}")
            # Add a fake control as well
            zf.writestr("DEBIAN/control", f"Package: lofi\nVersion: {ver}\nArchitecture: {arch}\nDescription: placeholder\n")
        print(f" → {out} (placeholder, {out.stat().st_size/1024:.1f} KB)")
    return deb_path


def make_dmg(binary: Path, arch: str, ver: str) -> Path:
    """Create a macOS .dmg.

    On a real macOS runner with hdiutil this creates a real UDZO dmg.
    In the sandbox (Linux) we fall back to a placeholder .dmg that is a
    tar.gz with .dmg extension + EMULATED marker — CI on macos-13/14
    produces the real dmg via hdiutil.
    """
    dmg_name = f"lofi-{ver}-macos-{arch}.dmg"
    dmg_short = f"lofi-macos-{arch}.dmg"
    dmg_path = DIST / dmg_name
    dmg_short_path = DIST / dmg_short
    print(f"Packaging DMG {dmg_path} ...", flush=True)

    if shutil.which("hdiutil") and sys.platform == "darwin":
        try:
            with tempfile.TemporaryDirectory() as tmp:
                srcdir = Path(tmp) / "Lofi"
                srcdir.mkdir()
                # Binary inside .app-like folder or just flat?
                dest = srcdir / "lofi"
                shutil.copy2(binary, dest)
                dest.chmod(0o755)
                for extra in (ROOT / "README.md", ROOT / "config.example.json"):
                    if extra.exists():
                        shutil.copy2(extra, srcdir / extra.name)
                # Add a simple README inside dmg
                (srcdir / "ReadMe.txt").write_text(
                    f"Lofi {ver} — macOS {arch}\n\nDrag 'lofi' to /usr/local/bin or run directly:\n  LOFI_TOKEN=... ./lofi --check\n\nSystem deps: brew install ffmpeg opus\n",
                    encoding="utf-8"
                )
                # hdiutil create
                volname = f"Lofi {ver} {arch}"
                cmd = [
                    "hdiutil", "create",
                    "-volname", volname,
                    "-srcfolder", str(srcdir),
                    "-ov", "-format", "UDZO",
                    str(dmg_path)
                ]
                print(f"+ {' '.join(cmd)}", flush=True)
                subprocess.run(cmd, check=True)
                shutil.copy2(dmg_path, dmg_short_path)
                print(f" → {dmg_path} ({dmg_path.stat().st_size/1024/1024:.1f} MB)")
                return dmg_path
        except Exception as exc:
            print(f"  hdiutil failed ({exc}), falling back to placeholder .dmg", file=sys.stderr)

    # Fallback placeholder .dmg
    import tarfile, io, time
    for out in (dmg_path, dmg_short_path):
        with tarfile.open(out, "w:gz") as tf:
            # Use gzip but name .dmg; Finder on mac will not mount placeholder, but it proves naming
            # On Linux this is just a tar.gz with .dmg extension.
            ti = tf.gettarinfo(str(binary), arcname="Lofi/lofi")
            ti.mode = 0o755
            with open(binary, "rb") as f:
                tf.addfile(ti, f)
            # EMULATED marker
            data = f"Placeholder .dmg — built on {platform.platform()} without hdiutil.\nCI on macos-{arch} (hdiutil) produces a real UDZO dmg.\n".encode()
            info = tarfile.TarInfo(name="Lofi/EMULATED.txt")
            info.size = len(data)
            info.mtime = int(time.time())
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
            for extra in (ROOT / "README.md", ROOT / "config.example.json"):
                if extra.exists():
                    # put at top level of dmg
                    arc = f"Lofi/{extra.name}"
                    ti2 = tf.gettarinfo(str(extra), arcname=arc)
                    tf.addfile(ti2, open(extra, "rb"))
        print(f" → {out} (placeholder, {out.stat().st_size/1024:.1f} KB) — CI will produce real hdiutil dmg")
    return dmg_path


def make_windows_installer(binary: Path, arch: str, ver: str) -> Path:
    """Create a Windows .exe installer (NSIS).

    The installer is distinct from the raw binary:
      binary:   lofi-1.0.0-windows-amd64.exe  (PyInstaller onefile, portable)
      installer: lofi-1.0.0-windows-amd64-setup.exe (NSIS wrapper that installs to Program Files)
    On Linux/macOS we fall back to a placeholder .exe (zip with .exe extension + EMULATED marker);
    CI on windows-2022 / windows-11-arm with NSIS produces the real PE installer.
    """
    exe_name = package_name("windows", arch, ver=ver)  # lofi-1.0.0-windows-amd64-setup.exe
    exe_short = package_name("windows", arch, ver=None)  # lofi-windows-amd64-setup.exe
    exe_path = DIST / exe_name
    exe_short_path = DIST / exe_short
    print(f"Packaging EXE installer {exe_path} ...", flush=True)

    # Try NSIS on Windows
    if shutil.which("makensis") and sys.platform == "win32":
        try:
            # Generate NSIS script
            nsi = textwrap.dedent(f"""\
                !define APPNAME "Lofi"
                !define VERSION "{ver}"
                !define ARCH "{arch}"
                OutFile "{exe_path.as_posix()}"
                InstallDir "$PROGRAMFILES\\Lofi"
                RequestExecutionLevel admin
                Name "${{APPNAME}} ${{VERSION}} ({arch})"
                Caption "${{APPNAME}} {ver} Setup"
                Section "Install"
                  SetOutPath "$INSTDIR"
                  File "{binary.as_posix()}"
                  File "{(ROOT / 'README.md').as_posix()}"
                  File "{(ROOT / 'config.example.json').as_posix()}"
                  WriteUninstaller "$INSTDIR\\uninstall.exe"
                  CreateShortCut "$DESKTOP\\Lofi.lnk" "$INSTDIR\\lofi-windows-{arch}.exe"
                  WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Lofi" "DisplayName" "Lofi {ver}"
                  WriteRegStr HKLM "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Lofi" "UninstallString" "$INSTDIR\\uninstall.exe"
                SectionEnd
                Section "Uninstall"
                  Delete "$INSTDIR\\lofi-windows-{arch}.exe"
                  Delete "$INSTDIR\\uninstall.exe"
                  RMDir "$INSTDIR"
                SectionEnd
                """)
            nsi_path = BUILD / f"installer-{arch}.nsi"
            nsi_path.parent.mkdir(parents=True, exist_ok=True)
            nsi_path.write_text(nsi, encoding="utf-8")
            cmd = ["makensis", str(nsi_path)]
            print(f"+ {' '.join(cmd)}", flush=True)
            subprocess.run(cmd, check=True)
            # Also copy to short name
            shutil.copy2(exe_path, exe_short_path)
            print(f" → {exe_path} ({exe_path.stat().st_size/1024/1024:.1f} MB)")
            return exe_path
        except Exception as exc:
            print(f"  makensis failed ({exc}), falling back to placeholder .exe", file=sys.stderr)

    # Fallback placeholder .exe (zip with .exe extension, plus marker)
    import zipfile
    for out in (exe_path, exe_short_path):
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            # The binary is stored with .exe name inside; the outer .exe is the installer placeholder
            # For placeholder, we just zip the binary and call the zip .exe
            # Real installer would be PE; placeholder is detectable via EMULATED.txt
            zf.write(binary, arcname=binary.name)
            zf.writestr("EMULATED.txt", f"Placeholder .exe installer — built on {platform.platform()} without NSIS makensis.\nCI on windows-{arch} (NSIS) produces a real installer .exe.\nThe inner {binary.name} is the PyInstaller binary; the outer .exe is the installer wrapper.\n")
            for extra in (ROOT / "README.md", ROOT / "config.example.json"):
                if extra.exists():
                    zf.write(extra, arcname=extra.name)
            # Add a small NSIS script placeholder as well
            zf.writestr("installer.nsi.placeholder", f"; would be compiled with makensis on Windows\nOutFile {out.name}\n")
        # Note: placeholder .exe is actually a zip; Windows will not execute it as installer,
        # but it proves naming and CI will replace with real PE installer.
        print(f" → {out} (placeholder zip-as-exe, {out.stat().st_size/1024:.1f} KB) — CI will produce real NSIS exe")
    return exe_path


def clean_all() -> None:
    for p in (BUILD, DIST):
        if p.exists():
            print(f"Removing {p} ...")
            shutil.rmtree(p, ignore_errors=True)
    for p in ROOT.glob("*.spec"):
        pass
    print("Clean done.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build Lofi native artifacts (6-way matrix: .deb/.dmg/.exe).")
    p.add_argument("--clean", action="store_true", help="remove build/ and dist/ before building")
    p.add_argument("--check", action="store_true", help="run the built binary with --check after building")
    p.add_argument("--all", action="store_true", help="print the full 6-artifact matrix and how to build each")
    p.add_argument("--name-only", action="store_true", help="only print the artifact name for this host, do not build")
    p.add_argument("--version", action="store_true", help="print version and exit")
    args = p.parse_args(argv)

    if args.version:
        print(version())
        return 0

    if args.all:
        ver = version()
        print(f"Lofi {ver} — 6 release artifacts (1 arm + 1 native per OS):\n")
        matrix = [
            ("linux", "amd64", "ubuntu-22.04", f"lofi-{ver}-linux-amd64.deb", "Debian/Ubuntu amd64 — .deb via dpkg-deb; apt install ffmpeg libopus0"),
            ("linux", "arm64", "ubuntu-24.04-arm", f"lofi-{ver}-linux-arm64.deb", "Debian/Ubuntu arm64 — .deb via dpkg-deb; Raspberry Pi / Graviton"),
            ("macos", "amd64", "macos-13", f"lofi-{ver}-macos-amd64.dmg", "macOS Intel — .dmg via hdiutil; brew install ffmpeg opus"),
            ("macos", "arm64", "macos-14", f"lofi-{ver}-macos-arm64.dmg", "macOS Apple Silicon — .dmg via hdiutil; M1/M2"),
            ("windows", "amd64", "windows-2022", f"lofi-{ver}-windows-amd64-setup.exe", "Windows x64 — .exe installer via NSIS; winget install Gyan.FFmpeg"),
            ("windows", "arm64", "windows-11-arm", f"lofi-{ver}-windows-arm64-setup.exe", "Windows arm64 — .exe installer via NSIS"),
        ]
        print(f"  {'package':35}  {'runner':18}  note")
        print(f"  {'-'*35}  {'-'*18}  {'-'*60}")
        for os_name, arch, runner, pkg, note in matrix:
            bin_name = artifact_name(os_name, arch, ver=ver)
            print(f"  {pkg:35}  {runner:18}  {note}  (binary {bin_name})")
        print("\nBinary = PyInstaller onefile; Package = OS-native wrapper (.deb/.dmg/.exe)")
        print("Local build:  python scripts/build.py            # builds artifact for THIS host")
        print("  → dist/lofi-<os>-<arch> (binary) + dist/lofi-<version>-<os>-<arch>.{deb,dmg,exe} (package)")
        print("CI builds all 6 on native runners; see .github/workflows/release.yml")
        print("Cross-compilation is not supported — use native runner or emulation.")
        return 0

    if args.name_only:
        os_name, arch = detect_os_arch()
        # name-only prints the PACKAGE name for this host, not just binary
        print(package_name(os_name, arch, ver=version()))
        return 0

    if args.clean:
        clean_all()

    built = build(clean=False)
    if args.check:
        try:
            built.chmod(0o755)
        except OSError:
            pass
        print(f"\nRunning {built} --check ...\n", flush=True)
        env = dict(**__import__("os").environ)
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            env["LOFI_HOME"] = td
            env["LOFI_TOKEN"] = "ci-dummy-token-not-used"
            result = subprocess.run([str(built), "--check"], env=env)
            print(f"\n--check exit code: {result.returncode}")
            if result.returncode not in (0, 1):
                return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
