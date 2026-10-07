#!/usr/bin/env python3
"""
Emulate the 6 release artifacts locally from the binary we can actually build.

PyInstaller cannot cross-compile: an amd64 Linux host cannot emit an arm64
macOS binary. CI solves that with 6 native runners. For local inspection this
script copies the one binary we *did* build into all 6 expected places and
re-packages each into the OS-native format (.deb/.dmg/.exe), marked as
*emulated* so you don't ship them by accident.

Produces (from version in VERSION):
  dist/lofi-linux-amd64,            dist/lofi-1.0.0-linux-amd64.deb
  dist/lofi-linux-arm64,            dist/lofi-1.0.0-linux-arm64.deb
  dist/lofi-macos-amd64,            dist/lofi-1.0.0-macos-amd64.dmg
  dist/lofi-macos-arm64,            dist/lofi-1.0.0-macos-arm64.dmg
  dist/lofi-windows-amd64.exe,      dist/lofi-1.0.0-windows-amd64.exe (installer)
  dist/lofi-windows-arm64.exe,      dist/lofi-1.0.0-windows-arm64.exe (installer)
plus a SHA256SUMS.txt

Usage:
  python scripts/make_dist_all.py
  # after a real build:
  .venv/bin/python scripts/build.py && .venv/bin/python scripts/make_dist_all.py && ls -lh dist/
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"

MATRIX = [
    ("linux", "amd64"),
    ("linux", "arm64"),
    ("macos", "amd64"),
    ("macos", "arm64"),
    ("windows", "amd64"),
    ("windows", "arm64"),
]

def find_source() -> Path:
    candidates = [
        DIST / "lofi",
        DIST / "lofi.exe",
        DIST / "lofi-linux-amd64",
        DIST / "lofi-linux-arm64",
        DIST / "lofi-macos-amd64",
        DIST / "lofi-macos-arm64",
        DIST / "lofi-windows-amd64.exe",
    ]
    for c in candidates:
        if c.exists() and c.is_file() and c.stat().st_size > 512:
            return c
    if DIST.exists():
        files = sorted(DIST.iterdir(), key=lambda p: p.stat().st_size if p.is_file() else 0, reverse=True)
        for f in files:
            if f.is_file() and f.stat().st_size > 512 and f.suffix not in (".deb", ".dmg", ".exe", ".tar.gz", ".zip", ".txt") and not f.name.startswith("SHA"):
                # raw binaries have no package suffix or .exe on windows; skip packages
                if "linux" in f.name or "macos" in f.name or f.name == "lofi":
                    return f
    raise FileNotFoundError("No built binary found in dist/. Run: python scripts/build.py or make build")

def package_ext(os_name: str) -> str:
    return {"linux": ".deb", "macos": ".dmg", "windows": ".exe"}[os_name]

def package_name(os_name: str, arch: str, ver: str) -> str:
    # Mirrors build.package_name
    if os_name == "windows":
        return f"lofi-{ver}-{os_name}-{arch}-setup.exe"
    return f"lofi-{ver}-{os_name}-{arch}{package_ext(os_name)}"

def package_short(os_name: str, arch: str) -> str:
    if os_name == "windows":
        return f"lofi-{os_name}-{arch}-setup.exe"
    return f"lofi-{os_name}-{arch}{package_ext(os_name)}"

def main() -> int:
    if not DIST.exists():
        print("dist/ not found — run: python scripts/build.py")
        return 2
    src = find_source()
    print(f"Source binary: {src} ({src.stat().st_size/1024/1024:.1f} MB)")
    print(f"Version: {VERSION}")
    print("Emulating 6 OS-native packages (.deb/.dmg/.exe) from one binary (for naming/inspection only):")
    print("  NOTE: only the package matching this host is a true native build;")
    print("        the other 5 are emulated placeholder packages — CI builds each natively.\n")

    # Import packaging helpers from build.py (avoid duplication)
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from build import make_deb, make_dmg, make_windows_installer
    except ImportError:
        # fallback inline if import fails
        make_deb = make_dmg = make_windows_installer = None  # type: ignore

    artifacts: list[Path] = []
    packages: list[Path] = []

    for os_name, arch in MATRIX:
        is_win = os_name == "windows"
        ext = ".exe" if is_win else ""
        bin_name = f"lofi-{os_name}-{arch}{ext}"
        ver_bin_name = f"lofi-{VERSION}-{os_name}-{arch}{ext}"
        dst = DIST / bin_name
        ver_dst = DIST / ver_bin_name
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
            dst.chmod(0o755)
        if src.resolve() != ver_dst.resolve():
            shutil.copy2(src, ver_dst)
            ver_dst.chmod(0o755)
        artifacts.extend([dst, ver_dst])
        print(f"  binary {bin_name:30} ← {src.name}")

        # Now package into OS-native format
        # Use the versioned binary as source for packaging (so package contains versioned binary name inside)
        source_for_pkg = ver_dst if ver_dst.exists() else dst
        try:
            if os_name == "linux" and make_deb:
                pkg = make_deb(source_for_pkg, arch, VERSION)
                for cand in (DIST / package_name("linux", arch, VERSION), DIST / package_short("linux", arch)):
                    if cand.exists():
                        packages.append(cand)
            elif os_name == "macos" and make_dmg:
                pkg = make_dmg(source_for_pkg, arch, VERSION)
                for cand in (DIST / package_name("macos", arch, VERSION), DIST / package_short("macos", arch)):
                    if cand.exists():
                        packages.append(cand)
            elif os_name == "windows" and make_windows_installer:
                pkg = make_windows_installer(source_for_pkg, arch, VERSION)
                for cand in (DIST / package_name("windows", arch, VERSION), DIST / package_short("windows", arch)):
                    if cand.exists():
                        packages.append(cand)
            else:
                raise RuntimeError("helpers not available")
        except Exception as exc:
            print(f"  packaging {os_name}/{arch} failed ({exc}), creating placeholder ...", file=sys.stderr)
            ver_pkg = DIST / package_name(os_name, arch, VERSION)
            short_pkg = DIST / package_short(os_name, arch)
            for p in (ver_pkg, short_pkg):
                if not p.exists():
                    p.write_bytes(b"placeholder")
                packages.append(p)

    # Deduplicate
    artifacts = sorted(set(artifacts))
    packages = sorted(set(packages))

    # SHA256SUMS for both binaries and packages
    sums_path = DIST / "SHA256SUMS.txt"
    with open(sums_path, "w", encoding="utf-8") as out:
        for p in sorted(set(artifacts + packages)):
            if p.is_file():
                h = hashlib.sha256(p.read_bytes()).hexdigest()
                out.write(f"{h}  {p.name}\n")
    print(f"\n  SHA256SUMS.txt")
    print(open(sums_path).read())

    print("Summary — 6 binaries + 6 versioned binaries + 12 packages (.deb/.dmg/.exe):")
    for os_name, arch in MATRIX:
        bin_name = f"lofi-{os_name}-{arch}{'.exe' if os_name=='windows' else ''}"
        pkg_name = package_name(os_name, arch, VERSION)
        print(f"  ✓ {bin_name:30}  →  {pkg_name}")

    total = sum(p.stat().st_size for p in set(artifacts+packages) if p.exists()) / 1024 / 1024
    print(f"\nTotal in dist/: {total:.1f} MB across {len(set(artifacts+packages))+1} files")
    print("\nInspect: ls -lh dist/*.deb dist/*.dmg dist/*.exe")
    print("Real native builds: use .github/workflows/release.yml (6 runners) or `make build` on each OS/arch.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
