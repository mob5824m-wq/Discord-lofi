#!/usr/bin/env python3
"""
Emulate the 6 release artifacts locally from the binary we can actually build.

PyInstaller cannot cross-compile: an amd64 Linux host cannot emit an arm64
macOS binary. CI solves that with 6 native runners. For local inspection (and
for reviewers who want to see that the packaging logic yields exactly 6 names)
this script copies the one binary we *did* build into all 6 expected places
and re-archives each. The copies are byte-identical to the real build and are
marked as *emulated* so you don't ship them by accident.

Produces (from version in VERSION):
  dist/lofi-linux-amd64,            dist/lofi-1.0.0-linux-amd64.tar.gz
  dist/lofi-linux-arm64,            dist/lofi-1.0.0-linux-arm64.tar.gz
  dist/lofi-macos-amd64,            dist/lofi-1.0.0-macos-amd64.tar.gz
  dist/lofi-macos-arm64,            dist/lofi-1.0.0-macos-arm64.tar.gz
  dist/lofi-windows-amd64.exe,      dist/lofi-1.0.0-windows-amd64.zip
  dist/lofi-windows-arm64.exe,      dist/lofi-1.0.0-windows-arm64.zip
plus a SHA256SUMS.txt

Usage:
  python scripts/make_dist_all.py
  # after a real build:
  .venv/bin/python scripts/build.py && .venv/bin/python scripts/make_dist_all.py && ls -lh dist/
"""

from __future__ import annotations

import hashlib
import shutil
import tarfile
import zipfile
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
    # fallback: largest file in dist (allow stub fallback ~1KB)
    if DIST.exists():
        files = sorted(DIST.iterdir(), key=lambda p: p.stat().st_size if p.is_file() else 0, reverse=True)
        for f in files:
            if f.is_file() and f.stat().st_size > 512 and f.suffix not in (".tar.gz", ".zip", ".txt") and not f.name.startswith("SHA"):
                return f
    raise FileNotFoundError("No built binary found in dist/. Run: python scripts/build.py or make build")

def main() -> int:
    if not DIST.exists():
        print("dist/ not found — run: python scripts/build.py")
        return 2
    src = find_source()
    print(f"Source binary: {src} ({src.stat().st_size/1024/1024:.1f} MB)")
    print(f"Version: {VERSION}")
    print("Emulating 6 artifacts (byte-identical copies, for naming/inspection only):")
    print("  NOTE: only the artifact matching this host is a true native build;")
    print("        the other 5 are emulated copies — CI builds each natively.\n")

    # Ensure source is correctly named variant also exists
    artifacts: list[Path] = []
    archives: list[Path] = []

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

        # Archive
        if is_win:
            archive = DIST / f"lofi-{VERSION}-{os_name}-{arch}.zip"
            short_archive = DIST / f"lofi-{os_name}-{arch}.zip"
            for arch_path in (archive, short_archive):
                with zipfile.ZipFile(arch_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    zf.write(dst, arcname=bin_name)
                    # Add marker file so it's obvious this is emulated when built on wrong host
                    zf.writestr("EMULATED.txt", f"This archive was emulated on {src} host — not a native {os_name}/{arch} build.\nCI builds each artifact natively; see .github/workflows/release.yml\n")
                    for extra in (ROOT / "config.example.json", ROOT / "README.md"):
                        if extra.exists():
                            zf.write(extra, arcname=extra.name)
                print(f"  {arch_path.name:40} {arch_path.stat().st_size/1024/1024:5.1f} MB  ← {bin_name}")
                archives.append(arch_path)
        else:
            archive = DIST / f"lofi-{VERSION}-{os_name}-{arch}.tar.gz"
            short_archive = DIST / f"lofi-{os_name}-{arch}.tar.gz"
            for arch_path in (archive, short_archive):
                with tarfile.open(arch_path, "w:gz") as tf:
                    ti = tf.gettarinfo(str(dst), arcname=bin_name)
                    ti.mode = 0o755
                    with open(dst, "rb") as f:
                        tf.addfile(ti, f)
                    # Emulated marker
                    import io, time
                    data = f"This archive was emulated on {src} host — not a native {os_name}/{arch} build.\n".encode()
                    info = tarfile.TarInfo(name="EMULATED.txt")
                    info.size = len(data)
                    info.mtime = int(time.time())
                    info.mode = 0o644
                    tf.addfile(info, io.BytesIO(data))
                    for extra in (ROOT / "config.example.json", ROOT / "README.md"):
                        if extra.exists():
                            tf.add(str(extra), arcname=extra.name)
                print(f"  {arch_path.name:40} {arch_path.stat().st_size/1024/1024:5.1f} MB  ← {bin_name}")
                archives.append(arch_path)

    # SHA256SUMS
    sums_path = DIST / "SHA256SUMS.txt"
    with open(sums_path, "w", encoding="utf-8") as out:
        for p in sorted(set(artifacts + archives)):
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            out.write(f"{h}  {p.name}\n")
    print(f"\n  SHA256SUMS.txt")
    print(open(sums_path).read())

    # Summary table
    print("Summary — 6 binaries + 6 versioned binaries + 12 archives:")
    for os_name, arch in MATRIX:
        bin_name = f"lofi-{os_name}-{arch}{'.exe' if os_name=='windows' else ''}"
        print(f"  ✓ {bin_name}")

    total = sum(p.stat().st_size for p in set(artifacts + archives) if p.exists()) / 1024 / 1024
    print(f"\nTotal in dist/: {total:.1f} MB across {len(set(artifacts+archives))+1} files")
    print("\nInspect: ls -lh dist/")
    print("Real native builds: use .github/workflows/release.yml (6 runners) or `make build` on each OS/arch.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
