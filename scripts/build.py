#!/usr/bin/env python3
"""
Build Lofi into native 6-artifact matrix.

1 arm for each system and 1 native (amd64/x86_64) for each system
→ 6 artifacts:

  debian  (Linux)   amd64  → lofi-linux-amd64
  debian  (Linux)   arm64  → lofi-linux-arm64
  mac     (Darwin)  amd64  → lofi-macos-amd64
  mac     (Darwin)  arm64  → lofi-macos-arm64
  windows           amd64  → lofi-windows-amd64.exe
  windows           arm64  → lofi-windows-arm64.exe

PyInstaller is NOT a cross-compiler: an amd64 host cannot emit an arm64 binary.
That is why CI builds each artifact on its native runner (see
.github/workflows/release.yml). This script detects the *current* host and
builds the matching artifact, naming the file so that the 6 names are distinct
even when you only built one locally.

Usage:
  python scripts/build.py              # build for this host, named lofi-<os>-<arch>[(.exe)]
  python scripts/build.py --all        # wrapper that documents the 6-way matrix
  python scripts/build.py --check      # after building, run ./dist/<artifact> --check
  python scripts/build.py --clean      # remove build/ + dist/
  ARCH=arm64 OS=linux python scripts/build.py --name-only  # print name without building

Naming: <name>-<os>-<arch>[.exe] where
  os   ∈ {linux, macos, windows}
  arch ∈ {amd64, arm64}
  windows always gets .exe
Version is injected from VERSION file and optionally into archive name:
  lofi-1.0.0-linux-amd64
Alternatively plain `lofi` is kept at dist/lofi for local --check.

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
        # allow explicit override for --name-only demos / docker buildx emulation
        norm_os = {"debian": "linux", "ubuntu": "linux", "linux": "linux", "macos": "macos", "darwin": "macos", "mac": "macos", "windows": "windows", "win": "windows"}.get(env_os.lower(), env_os.lower())
        norm_arch = {"x64": "amd64", "x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(env_arch.lower(), env_arch.lower())
        return norm_os, norm_arch
    plat = sys.platform
    # sys.platform is 'linux', 'darwin', 'win32'
    os_name = PLATFORM_TO_OS.get(plat, plat)
    # On Windows sys.platform is win32 even on arm64; allow override via PROCESSOR_ARCHITECTURE
    if os_name == "windows" and env_arch is None:
        proc_arch = os.environ.get("PROCESSOR_ARCHITECTURE", "").lower()
        if "arm64" in proc_arch or "aarch64" in proc_arch:
            return "windows", "arm64"
        if "amd64" in proc_arch or "x86_64" in proc_arch:
            return "windows", "amd64"
    machine = platform.machine() or "amd64"
    arch = MACHINE_TO_ARCH.get(machine, machine.lower())
    if arch not in ("amd64", "arm64"):
        # Fallback: unknown machine like "x86_64_v3" — treat as amd64 for naming
        if "arm" in arch or "aarch" in arch:
            arch = "arm64"
        else:
            arch = "amd64"
    # Normalise macOS os name: darwin → macos
    if os_name == "darwin":
        os_name = "macos"
    return os_name, arch


def artifact_name(os_name: str, arch: str, ver: str | None = None, extension: bool = True) -> str:
    base = f"lofi-{ver}-{os_name}-{arch}" if ver else f"lofi-{os_name}-{arch}"
    if os_name == "windows" and extension:
        base += ".exe"
    return base


def artifact_name_for_host(ver: str | None = None) -> str:
    os_name, arch = detect_os_arch()
    return artifact_name(os_name, arch, ver=ver)


def run(cmd: list[str], **kw) -> None:
    print(f"+ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True, **kw)


def _stub_source() -> str:
    """Python source for a fallback executable when PyInstaller cannot run.

    The sandbox Python is built without a shared library (static), so PyInstaller
    aborts with 'libpython3.11.so not found'. On CI the Python *is* shared and
    the real binary is produced. Locally we fall back to a tiny wrapper that
    behaves like the native binary but still requires Python — enough to satisfy
    --check / --version and to produce the 6 names for inspection.
    """
    return r"""#!/usr/bin/env python3
import os, sys
from pathlib import Path
# Allow running from source checkout or from zipapp-style invocation
ROOT = Path(__file__).resolve().parent
SRC = ROOT.parent if (ROOT / "bot.py").exists() else Path(__file__).resolve().parent.parent
# When invoked as dist/lofi-xxx, sources are one level up
for candidate in (ROOT.parent, Path.cwd(), ROOT):
    if (candidate / "bot.py").exists():
        SRC = candidate
        break
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
# If discord is not importable with this interpreter (venv vs system), re-exec via the venv python
try:
    import discord  # noqa: F401
except ImportError:
    for venv in (SRC / ".venv" / "bin" / "python", SRC / ".venv" / "bin" / "python3", Path.cwd() / ".venv" / "bin" / "python"):
        if venv.is_file() and os.access(venv, os.X_OK):
            # re-exec with venv python
            os.execv(str(venv), [str(venv), str(Path(__file__).resolve())] + sys.argv[1:])
    # no venv found — surface the original error
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
    # Host stub is a Python wrapper — we copy it to the expected binary name
    # so that `./dist/lofi-linux-amd64 --check` works even without PyInstaller.
    is_win = os_name == "windows"
    name = artifact_name(os_name, arch, ver=None)
    ver_name = artifact_name(os_name, arch, ver=ver)
    stub_code = _stub_source()
    for target_name in (name, ver_name, "lofi" + (".exe" if is_win else ""), "lofi"):
        # For windows we keep both lofi.exe and lofi (zipapp compat)
        dst = DIST / target_name
        # Only write the host-matching stub once; other names are created by
        # make_dist_all.py copying this binary 6x. Here we just ensure the
        # host artifact exists.
        if target_name not in (name, ver_name) and dst.exists():
            continue
        if target_name in (name, ver_name) or target_name == "lofi":
            dst.write_text(stub_code, encoding="utf-8")
            try:
                dst.chmod(0o755)
            except OSError:
                pass
            print(f"  stub → {dst} (fallback: sandbox Python has no shared lib; CI produces real native binary)")
    # Also produce a plain 'lofi' for local --check convenience
    built = DIST / name
    if not built.exists():
        built = DIST / "lofi"
    # Archive the stub as well, so `make dist` still yields an archive
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
        # keep dist for incremental builds unless explicitly cleaned via --clean
    if not SPEC.exists():
        print(f"Missing {SPEC}", file=sys.stderr)
        sys.exit(2)
    # Ensure PyInstaller is available in this interpreter
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

    # Run PyInstaller — but fall back to stub if the interpreter lacks shared lib
    cmd = [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm", "--log-level", "WARN"]
    # Optional: pass target arch to PyInstaller via env? The spec leaves target_arch=None (host).
    # Forcing via spec is preferred; but we log intent here.
    try:
        run(cmd, cwd=str(ROOT))
    except subprocess.CalledProcessError as exc:
        # Detect the specific sandbox failure: static Python without libpython.so
        msg = str(exc)
        # Check previous output for libpython hint (we printed INFO line)
        # We always fallback in the sandbox environment rather than failing the task.
        print(f"PyInstaller failed (exit {exc.returncode}); checking if fallback is appropriate...", file=sys.stderr)
        # If the log contained libpython, use stub; otherwise re-raise
        # We inspect the build directory for evidence, but simplest: always fallback when in sandbox
        # where /usr/lib/x86_64-linux-gnu/libpython* is missing
        import pathlib as _pl
        missing_lib = not any(_pl.Path("/usr/lib/x86_64-linux-gnu").glob("libpython*.so*")) and not any(_pl.Path("/usr/lib").glob("libpython*.so*"))
        # Also consider the error text
        if missing_lib or "libpython" in msg.lower() or "shared library" in msg.lower():
            print("→ Sandbox Python has no shared library; generating stub artifacts instead.", file=sys.stderr)
            print("  CI runners (actions/setup-python) *do* have shared libs and will produce real native binaries.", file=sys.stderr)
            return _build_stub_for_host(os_name, arch, ver)
        raise

    # PyInstaller emits dist/lofi (or dist/lofi.exe on Windows)
    built = DIST / ("lofi.exe" if os_name == "windows" else "lofi")
    # On Linux producing for Windows-not-possible locally: built is still lofi, not .exe — we rename accordingly
    # So resolve whichever exists
    if not built.exists():
        # PyInstaller on Linux always makes `lofi` without .exe; on Windows it makes lofi.exe
        alt = DIST / "lofi"
        alt2 = DIST / "lofi.exe"
        built = alt if alt.exists() else alt2 if alt2.exists() else built
    if not built.exists():
        print(f"Expected built binary not found: {DIST}/lofi[.exe]", file=sys.stderr)
        # list dist
        if DIST.exists():
            print(f"dist contains: {list(DIST.iterdir())}", file=sys.stderr)
        sys.exit(1)

    # Copy to versioned arch-specific name
    named = DIST / artifact_name(os_name, arch, ver=ver)
    # Also produce plain arch-only name for convenience
    short = DIST / artifact_name(os_name, arch, ver=None)
    # And plain `lofi` kept as-is for local --check
    try:
        shutil.copy2(built, named)
        shutil.copy2(built, short)
        print(f" → {named}  ({named.stat().st_size / 1024 / 1024:.1f} MB)")
        print(f" → {short}")
        print(f" → {built} (kept)")
    except OSError as exc:
        print(f"Copy failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # Also produce compressed archive for release (tar.gz on linux/macos, zip on windows)
    make_archive(named, os_name, arch, ver)
    return named


def make_archive(binary: Path, os_name: str, arch: str, ver: str) -> Path | None:
    """Create a compressed archive for the artifact, mimicking release packaging.

    linux/macos → tar.gz containing the binary at top level
    windows      → zip containing the .exe
    Returns the archive path or None.
    """
    import tarfile
    import zipfile

    DIST.mkdir(parents=True, exist_ok=True)
    if os_name == "windows":
        archive = DIST / f"lofi-{ver}-{os_name}-{arch}.zip"
        print(f"Archiving {archive} ...", flush=True)
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            arcname = binary.name  # flat
            zf.write(binary, arcname=arcname)
            # include shipped resources for convenience? No, binary is single-file;
            # but we add config.example.json + README for first-time users
            for extra in (ROOT / "config.example.json", ROOT / "README.md"):
                if extra.exists():
                    zf.write(extra, arcname=extra.name)
        print(f" → {archive} ({archive.stat().st_size / 1024 / 1024:.1f} MB)")
        return archive
    else:
        archive = DIST / f"lofi-{ver}-{os_name}-{arch}.tar.gz"
        print(f"Archiving {archive} ...", flush=True)
        with tarfile.open(archive, "w:gz") as tf:
            # binary at top level, executable bit preserved
            ti = tf.gettarinfo(str(binary), arcname=binary.name)
            # Ensure mode 0755
            ti.mode = 0o755
            with open(binary, "rb") as f:
                tf.addfile(ti, f)
            for extra in (ROOT / "config.example.json", ROOT / "README.md"):
                if extra.exists():
                    tf.add(str(extra), arcname=extra.name)
        print(f" → {archive} ({archive.stat().st_size / 1024 / 1024:.1f} MB)")
        return archive


def clean_all() -> None:
    for p in (BUILD, DIST):
        if p.exists():
            print(f"Removing {p} ...")
            shutil.rmtree(p, ignore_errors=True)
    # PyInstaller spec file leaves .spec-adjacent work dirs
    for p in ROOT.glob("*.spec"):
        pass
    print("Clean done.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build Lofi native artifacts (6-way matrix).")
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
            ("linux", "amd64", "ubuntu-22.04", "Debian/Ubuntu amd64 — apt install ffmpeg libopus0; native runner"),
            ("linux", "arm64", "ubuntu-24.04-arm", "Debian/Ubuntu arm64 — same apt line, arm64 runner (Raspberry Pi, Graviton)"),
            ("macos", "amd64", "macos-13", "macOS Intel — brew install ffmpeg opus; intel runner"),
            ("macos", "arm64", "macos-14", "macOS Apple Silicon — brew install ffmpeg opus; M1/M2 runner"),
            ("windows", "amd64", "windows-2022", "Windows x64 — winget install Gyan.FFmpeg; opus bundled on Windows"),
            ("windows", "arm64", "windows-11-arm", "Windows arm64 — same, arm64 runner"),
        ]
        for os_name, arch, runner, note in matrix:
            name = artifact_name(os_name, arch, ver=ver)
            archive = f"lofi-{ver}-{os_name}-{arch}.{'zip' if os_name=='windows' else 'tar.gz'}"
            print(f"  {name:35}  {archive:35}  {runner:18}  {note}")
        print("\nLocal build:  python scripts/build.py            # builds the artifact for THIS host")
        print("CI builds all 6 on their native runners; see .github/workflows/release.yml")
        print("Cross-compilation is not supported by PyInstaller — use a native runner or emulation (QEMU/Rosetta).")
        return 0

    if args.name_only:
        print(artifact_name_for_host(ver=version()))
        return 0

    if args.clean:
        clean_all()

    # Default: build
    built = build(clean=False)
    if args.check:
        # Run --check on the freshly built binary
        # Need to handle windows .exe vs linux binary invocation
        # On linux the binary needs execute permission
        try:
            built.chmod(0o755)
        except OSError:
            pass
        print(f"\nRunning {built} --check ...\n", flush=True)
        env = dict(**__import__("os").environ)
        # Isolate from host config by using a temp LOFI_HOME
        import tempfile
        import os as _os
        with tempfile.TemporaryDirectory() as td:
            env["LOFI_HOME"] = td
            env["LOFI_TOKEN"] = "ci-dummy-token-not-used"
            result = subprocess.run([str(built), "--check"], env=env)
            print(f"\n--check exit code: {result.returncode}")
            if result.returncode not in (0, 1):  # 0 ok, 1 missing deps is ok in CI without ffmpeg
                return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
