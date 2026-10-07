#!/usr/bin/env python3
"""
Build Lofi into OS-native installers: .deb (Linux), .dmg (macOS), .exe (Windows).

Every push to main runs this on 6 native GitHub runners (see
.github/workflows/release.yml) and publishes the result, so the download a user
gets is a real installer for their machine:

  debian  (Linux)   amd64  -> lofi-<ver>-linux-amd64.deb
  debian  (Linux)   arm64  -> lofi-<ver>-linux-arm64.deb
  macOS            amd64  -> lofi-<ver>-macos-amd64.dmg     (contains a .pkg)
  macOS            arm64  -> lofi-<ver>-macos-arm64.dmg     (contains a .pkg)
  windows          amd64  -> lofi-<ver>-windows-amd64-setup.exe   (NSIS)
  windows          arm64  -> lofi-<ver>-windows-arm64-setup.exe   (NSIS)

The raw PyInstaller one-file binary is also published, for people who want a
portable executable instead of an install:

  lofi-linux-<arch>, lofi-macos-<arch>, lofi-windows-<arch>.exe

PyInstaller is NOT a cross-compiler and neither are the OS packagers: an amd64
Linux host cannot emit an arm64 .deb, and no non-Apple host can run hdiutil.
So this script builds the artifact for the *current* host and names it so the
six names stay distinct. CI builds each on its native runner.

There are no "best effort" packages here on purpose. A zip renamed to .deb, or
a tar.gz renamed to .dmg, installs nothing and looks like a working release
until a user tries it. If this host cannot build a real package it fails loudly
with the command to fix it; `--allow-placeholder` (used only by
scripts/make_dist_all.py, for inspecting names locally) opts into the fake.

Usage:
  python scripts/build.py                # PyInstaller + package for THIS host
  python scripts/build.py --check        # ...then run the binary with --check
  python scripts/build.py --all          # print the 6-artifact matrix
  python scripts/build.py --name-only    # print this host's package name
  python scripts/build.py --clean        # remove build/ and dist/ first
  python scripts/build.py --version      # print VERSION

Templates live in packaging/ and are rendered with @VERSION@ / @ARCH@:
  packaging/debian/   control, postinst, prerm, postrm, lofi.service,
                      lofi.env.example, lofi.1, copyright
  packaging/macos/    ReadMe.txt.in, postinstall (the .pkg's only script)
  packaging/windows/  installer.nsi
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
SPEC = ROOT / "lofi.spec"
PACKAGING = ROOT / "packaging"

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

# Reverse identifiers used by the OS installers themselves, not the file names.
DEB_ARCH = {"amd64": "amd64", "arm64": "arm64"}
MACOS_ARCH_LABEL = {"amd64": "x86_64 (Intel)", "arm64": "arm64 (Apple Silicon)"}
WINDOWS_ARCH_LABEL = {"amd64": "64-bit (x64)", "arm64": "ARM64"}
MACOS_PKG_ID = "io.github.mob5824m-wq.lofi"


class PackagingError(RuntimeError):
    """Raised when this host cannot produce a real, installable package."""


# --------------------------------------------------------------------------- #
# naming
# --------------------------------------------------------------------------- #

def version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def detect_os_arch() -> tuple[str, str]:
    env_os = os.environ.get("LOFI_BUILD_OS") or os.environ.get("OS")
    env_arch = os.environ.get("LOFI_BUILD_ARCH") or os.environ.get("ARCH")
    if env_os and env_arch:
        norm_os = {
            "debian": "linux", "ubuntu": "linux", "linux": "linux",
            "macos": "macos", "darwin": "macos", "mac": "macos",
            "windows": "windows", "win": "windows",
        }.get(env_os.lower(), env_os.lower())
        norm_arch = {
            "x64": "amd64", "x86_64": "amd64", "amd64": "amd64",
            "aarch64": "arm64", "arm64": "arm64",
        }.get(env_arch.lower(), env_arch.lower())
        return norm_os, norm_arch

    os_name = PLATFORM_TO_OS.get(sys.platform, sys.platform)

    if os_name == "windows":
        proc_arch = os.environ.get("PROCESSOR_ARCHITECTURE", "").lower()
        if "arm64" in proc_arch or "aarch64" in proc_arch:
            return "windows", "arm64"
        if "amd64" in proc_arch or "x86_64" in proc_arch:
            return "windows", "amd64"

    machine = platform.machine() or "amd64"
    arch = MACHINE_TO_ARCH.get(machine, machine.lower())
    if arch not in ("amd64", "arm64"):
        arch = "arm64" if ("arm" in arch or "aarch" in arch) else "amd64"
    return os_name, arch


def artifact_name(os_name: str, arch: str, ver: str | None = None) -> str:
    """Raw binary name, e.g. lofi-1.0.2-linux-amd64 / lofi-1.0.2-windows-arm64.exe."""
    base = f"lofi-{ver}-{os_name}-{arch}" if ver else f"lofi-{os_name}-{arch}"
    return base + ".exe" if os_name == "windows" else base


def package_ext(os_name: str) -> str:
    return {"linux": ".deb", "macos": ".dmg", "windows": ".exe"}[os_name]


def package_name(os_name: str, arch: str, ver: str | None = None, short: bool = False) -> str:
    """Installer name, e.g. lofi-1.0.2-linux-amd64.deb / lofi-windows-amd64-setup.exe."""
    base = f"lofi-{ver}-{os_name}-{arch}" if (ver and not short) else f"lofi-{os_name}-{arch}"
    if os_name == "windows":
        base += "-setup"  # keeps the installer distinct from the portable binary
    return base + package_ext(os_name)


def run(cmd: list[str], **kw) -> None:
    print(f"+ {' '.join(str(c) for c in cmd)}", flush=True)
    subprocess.run(cmd, check=True, **kw)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def render(template: Path, dest: Path, **vars: str) -> Path:
    text = template.read_text(encoding="utf-8")
    for key, val in vars.items():
        text = text.replace(f"@{key}@", val)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return dest


def _chmod_exec(path: Path, mode: int = 0o755) -> None:
    path.chmod(mode)


def _dir_size_kb(path: Path) -> int:
    total = 0
    for parent, _dirs, files in os.walk(path):
        if Path(parent).name == "DEBIAN":
            continue
        for name in files:
            total += (Path(parent) / name).stat().st_size
    return max(1, total // 1024)


# --------------------------------------------------------------------------- #
# PyInstaller
# --------------------------------------------------------------------------- #

def build(clean: bool = False, allow_placeholder: bool = False) -> Path:
    if clean:
        for p in (BUILD, ROOT / "__pycache__"):
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)

    if not SPEC.exists():
        raise SystemExit(f"Missing {SPEC}")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit("PyInstaller not found. Run: pip install pyinstaller")

    os_name, arch = detect_os_arch()
    ver = version()
    print(
        f"Building Lofi {ver} for {os_name}/{arch} on {platform.platform()} "
        f"({platform.machine()}), Python {sys.version.split()[0]}",
        flush=True,
    )
    if arch == "arm64" and platform.machine().lower() in ("x86_64", "amd64"):
        print(
            "NOTE: host is amd64 but target is arm64. PyInstaller cannot "
            "cross-compile; use an arm64 runner (see .github/workflows/release.yml).",
            flush=True,
        )

    run([sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm", "--log-level", "WARN"],
        cwd=str(ROOT))

    built = DIST / ("lofi.exe" if os_name == "windows" else "lofi")
    if not built.exists():
        alt = DIST / ("lofi" if os_name == "windows" else "lofi.exe")
        if alt.exists():
            built = alt
    if not built.exists():
        raise SystemExit(
            f"Expected binary not found: {DIST}/lofi[.exe] "
            f"(dist contains: {[p.name for p in DIST.iterdir()] if DIST.exists() else 'nothing'})"
        )
    _chmod_exec(built)

    named = DIST / artifact_name(os_name, arch, ver=ver)
    short = DIST / artifact_name(os_name, arch)
    shutil.copy2(built, named)
    shutil.copy2(built, short)
    _chmod_exec(named)
    _chmod_exec(short)
    print(f" -> {named}  ({named.stat().st_size / 1024 / 1024:.1f} MB)", flush=True)
    print(f" -> {short}", flush=True)

    make_archive(named, os_name, arch, ver, allow_placeholder=allow_placeholder)
    return named


def make_archive(binary: Path, os_name: str, arch: str, ver: str,
                 allow_placeholder: bool = False) -> Path:
    DIST.mkdir(parents=True, exist_ok=True)
    if os_name == "linux":
        return make_deb(binary, arch, ver, allow_placeholder=allow_placeholder)
    if os_name == "macos":
        return make_dmg(binary, arch, ver, allow_placeholder=allow_placeholder)
    if os_name == "windows":
        return make_windows_installer(binary, arch, ver, allow_placeholder=allow_placeholder)
    raise PackagingError(f"unknown os {os_name}")


# --------------------------------------------------------------------------- #
# Debian / Ubuntu — .deb
# --------------------------------------------------------------------------- #

def make_deb(binary: Path, arch: str, ver: str, allow_placeholder: bool = False) -> Path:
    """Build a real Debian package with dpkg-deb (control + maintainer scripts).

    Layout:
      /usr/local/bin/lofi
      /usr/share/doc/lofi/{README.md,config.example.json,LICENSE,copyright}
      /usr/share/doc/lofi/examples/{lofi.env,lofi.service}
      /usr/share/man/man1/lofi.1.gz
      /lib/systemd/system/lofi.service          (installed, not enabled)
      DEBIAN/{control,postinst,prerm,postrm}
    """
    deb_path = DIST / package_name("linux", arch, ver=ver)
    deb_short = DIST / package_name("linux", arch, ver=None)
    print(f"Packaging DEB {deb_path.name} ...", flush=True)

    DIST.mkdir(parents=True, exist_ok=True)

    dpkg = shutil.which("dpkg-deb")
    if not (dpkg and sys.platform.startswith("linux")):
        if not allow_placeholder:
            raise PackagingError(
                "dpkg-deb not available, so a real .deb cannot be built here.\n"
                "  Debian/Ubuntu: sudo apt-get install -y dpkg-dev\n"
                "  Everywhere else: let GitHub Actions build it (release.yml, "
                "ubuntu-22.04 / ubuntu-24.04-arm runners)."
            )
        return _placeholder(binary, [deb_path, deb_short], "deb", arch, ver)

    with tempfile.TemporaryDirectory() as tmp:
        pkgdir = Path(tmp) / f"lofi_{ver}_{DEB_ARCH[arch]}"
        bindir = pkgdir / "usr/local/bin"
        docdir = pkgdir / "usr/share/doc/lofi"
        exampledir = docdir / "examples"
        mandir = pkgdir / "usr/share/man/man1"
        units = pkgdir / "lib/systemd/system"
        debiandir = pkgdir / "DEBIAN"
        for d in (bindir, docdir, exampledir, mandir, units, debiandir):
            d.mkdir(parents=True)

        dest_bin = bindir / "lofi"
        shutil.copy2(binary, dest_bin)
        _chmod_exec(dest_bin, 0o755)

        for name in ("README.md", "config.example.json", "LICENSE"):
            src = ROOT / name
            if src.exists():
                shutil.copy2(src, docdir / name)
                (docdir / name).chmod(0o644)

        shutil.copy2(PACKAGING / "debian" / "copyright", docdir / "copyright")
        (docdir / "copyright").chmod(0o644)

        # systemd unit: shipped active-but-disabled, plus a copy in examples so
        # a user can read it without hunting through /lib.
        unit = render(PACKAGING / "debian" / "lofi.service", units / "lofi.service")
        unit.chmod(0o644)
        shutil.copy2(unit, exampledir / "lofi.service")
        shutil.copy2(PACKAGING / "debian" / "lofi.env.example", exampledir / "lofi.env")
        (exampledir / "lofi.env").chmod(0o644)

        # The same two files in /usr/share/lofi: slim Debian/Ubuntu images tell
        # dpkg to skip /usr/share/doc/*, and the postinst needs the env example
        # to be there so `systemctl enable lofi` has somewhere to read a token.
        sharedir = pkgdir / "usr/share/lofi"
        sharedir.mkdir(parents=True)
        shutil.copy2(PACKAGING / "debian" / "lofi.env.example", sharedir / "lofi.env")
        (sharedir / "lofi.env").chmod(0o644)
        shutil.copy2(unit, sharedir / "lofi.service")
        (sharedir / "lofi.service").chmod(0o644)
        if (ROOT / "config.example.json").exists():
            shutil.copy2(ROOT / "config.example.json", sharedir / "config.example.json")

        # man page (gzipped, as Debian expects)
        man_src = render(
            PACKAGING / "debian" / "lofi.1",
            Path(tmp) / "lofi.1",
            VERSION=ver,
            DATE=time.strftime("%Y-%m-%d"),
        )
        with open(man_src, "rb") as fin, gzip.GzipFile(
            mandir / "lofi.1.gz", "wb", compresslevel=9, mtime=0
        ) as fout:
            shutil.copyfileobj(fin, fout)
        (mandir / "lofi.1.gz").chmod(0o644)

        # control is written last so Installed-Size reflects the real payload
        render(
            PACKAGING / "debian" / "control",
            debiandir / "control",
            VERSION=ver,
            ARCH=DEB_ARCH[arch],
            INSTALLED_SIZE=str(_dir_size_kb(pkgdir)),
        )
        (debiandir / "control").chmod(0o644)
        for script in ("postinst", "prerm", "postrm"):
            src = PACKAGING / "debian" / script
            dst = debiandir / script
            shutil.copy2(src, dst)
            dst.chmod(0o755)

        # --root-owner-group (dpkg >= 1.19.2) makes the package's files owned by
        # root:root instead of whoever ran the build. Older dpkg can still
        # build; anything else is a real failure and must not be swallowed.
        cmd = [dpkg, "--build", "--root-owner-group", "-Zxz", str(pkgdir), str(deb_path)]
        print(f"+ {' '.join(cmd)}", flush=True)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            combined = (result.stdout or "") + (result.stderr or "")
            retryable = any(
                s in combined.lower()
                for s in ("root-owner-group", "unrecognized", "unknown option", "invalid option")
            )
            if not retryable:
                print(combined, file=sys.stderr)
                last = (combined.strip().splitlines() or ["unknown error"])[-1]
                raise PackagingError(f"dpkg-deb refused the package: {last}")
            print("  (--root-owner-group unsupported by this dpkg, retrying without it)",
                  flush=True)
            run([dpkg, "--build", "-Zxz", str(pkgdir), str(deb_path)])

    shutil.copy2(deb_path, deb_short)
    print(f" -> {deb_path.name} ({deb_path.stat().st_size / 1024 / 1024:.1f} MB)", flush=True)
    print(f" -> {deb_short.name}", flush=True)
    return deb_path


# --------------------------------------------------------------------------- #
# macOS — .dmg containing a .pkg installer
# --------------------------------------------------------------------------- #

def make_dmg(binary: Path, arch: str, ver: str, allow_placeholder: bool = False) -> Path:
    """Build a real UDZO .dmg holding a real .pkg (built with pkgbuild).

    Layout inside the mounted image:
      Lofi-<ver>.pkg    double-click installer -> /usr/local/bin/lofi
      portable/lofi     the same binary for people who prefer to copy it
      README.md, LICENSE, ReadMe.txt
    """
    dmg_path = DIST / package_name("macos", arch, ver=ver)
    dmg_short = DIST / package_name("macos", arch, ver=None)
    print(f"Packaging DMG {dmg_path.name} ...", flush=True)

    DIST.mkdir(parents=True, exist_ok=True)

    if not (shutil.which("hdiutil") and sys.platform == "darwin"):
        if not allow_placeholder:
            raise PackagingError(
                "hdiutil not available, so a real .dmg cannot be built here.\n"
                "  macOS: hdiutil ships with the OS (Xcode command line tools).\n"
                "  Everywhere else: let GitHub Actions build it (release.yml, "
                "macos-15-intel / macos-14 runners)."
            )
        return _placeholder(binary, [dmg_path, dmg_short], "dmg", arch, ver)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        payload = tmp / "payload"
        (payload / "bin").mkdir(parents=True)
        (payload / "share/doc/lofi").mkdir(parents=True)

        dest = payload / "bin" / "lofi"
        shutil.copy2(binary, dest)
        _chmod_exec(dest, 0o755)
        for name in ("README.md", "LICENSE", "config.example.json"):
            src = ROOT / name
            if src.exists():
                shutil.copy2(src, payload / "share/doc/lofi" / name)
        shutil.copy2(PACKAGING / "macos" / "ReadMe.txt.in",
                     payload / "share/doc/lofi" / "ReadMe.txt")

        # postinstall: make sure the binary is executable, drop the quarantine
        # attribute an unsigned download arrives with, and ask once - on a first
        # install - for the bot token. It lives in packaging/macos as a real
        # file rather than a string here, so it can be read, linted and tested
        # the way the Debian maintainer scripts are.
        scripts = tmp / "scripts"
        scripts.mkdir()
        postinstall = scripts / "postinstall"
        shutil.copy2(PACKAGING / "macos" / "postinstall", postinstall)
        postinstall.chmod(0o755)

        pkg = tmp / f"Lofi-{ver}.pkg"
        if shutil.which("pkgbuild"):
            run([
                "pkgbuild",
                "--root", str(payload),
                "--identifier", MACOS_PKG_ID,
                "--version", ver,
                "--install-location", "/usr/local",
                "--scripts", str(scripts),
                str(pkg),
            ])
        else:  # pkgbuild ships with macOS; this is a belt-and-braces branch
            print("  pkgbuild missing — shipping the binary inside the dmg instead",
                  file=sys.stderr)

        # image contents
        dmgdir = tmp / "dmg"
        dmgdir.mkdir()
        if pkg.exists():
            shutil.copy2(pkg, dmgdir / pkg.name)
        portable = dmgdir / "portable"
        portable.mkdir()
        shutil.copy2(binary, portable / "lofi")
        _chmod_exec(portable / "lofi", 0o755)
        for name in ("README.md", "LICENSE"):
            if (ROOT / name).exists():
                shutil.copy2(ROOT / name, dmgdir / name)
        render(
            PACKAGING / "macos" / "ReadMe.txt.in",
            dmgdir / "ReadMe.txt",
            VERSION=ver,
            ARCH=MACOS_ARCH_LABEL.get(arch, arch),
        )

        run([
            "hdiutil", "create",
            "-volname", f"Lofi {ver} ({arch})",
            "-srcfolder", str(dmgdir),
            "-ov", "-format", "UDZO", "-imagekey", "zlib-level=9",
            str(dmg_path),
        ])
        run(["hdiutil", "verify", str(dmg_path)])

    shutil.copy2(dmg_path, dmg_short)
    print(f" -> {dmg_path.name} ({dmg_path.stat().st_size / 1024 / 1024:.1f} MB)", flush=True)
    print(f" -> {dmg_short.name}", flush=True)
    return dmg_path


# --------------------------------------------------------------------------- #
# Windows — NSIS .exe installer
# --------------------------------------------------------------------------- #

def _find_makensis() -> str | None:
    found = shutil.which("makensis")
    if found:
        return found
    # choco's NSIS package does not always land on PATH inside the same job.
    for candidate in (
        Path(r"C:\Program Files (x86)\NSIS\makensis.exe"),
        Path(r"C:\Program Files\NSIS\makensis.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def make_windows_installer(binary: Path, arch: str, ver: str,
                           allow_placeholder: bool = False) -> Path:
    """Build a real NSIS installer (.exe, PE) from packaging/windows/installer.nsi."""
    exe_path = DIST / package_name("windows", arch, ver=ver)
    exe_short = DIST / package_name("windows", arch, ver=None)
    print(f"Packaging EXE installer {exe_path.name} ...", flush=True)

    DIST.mkdir(parents=True, exist_ok=True)

    nsi = PACKAGING / "windows" / "installer.nsi"
    if not nsi.exists():
        raise PackagingError(f"missing NSIS script: {nsi}")

    makensis = _find_makensis()
    if not makensis:
        if not allow_placeholder:
            raise PackagingError(
                "makensis not found, so a real Windows installer cannot be built here.\n"
                "  Windows: choco install nsis   (or download from nsis.sourceforge.io)\n"
                "  Everywhere else: let GitHub Actions build it (release.yml, "
                "windows-2022 / windows-11-arm runners)."
            )
        return _placeholder(binary, [exe_path, exe_short], "exe", arch, ver)

    license_file = ROOT / "LICENSE"
    readme = ROOT / "README.md"
    example = ROOT / "config.example.json"
    for required in (license_file, readme, example, binary):
        if not Path(required).exists():
            raise PackagingError(f"missing input for installer: {required}")

    run([
        makensis,
        f"/DAPPNAME=Lofi",
        f"/DVERSION={ver}",
        f"/DARCH={arch}",
        f"/DARCHLABEL={WINDOWS_ARCH_LABEL.get(arch, arch)}",
        f"/DOUTFILE={exe_path}",
        f"/DBINARY={binary.resolve()}",
        f"/DLICENSE={license_file.resolve()}",
        f"/DREADME={readme.resolve()}",
        f"/DEXAMPLE={example.resolve()}",
        "/V2",
        str(nsi.resolve()),
    ])

    shutil.copy2(exe_path, exe_short)
    print(f" -> {exe_path.name} ({exe_path.stat().st_size / 1024 / 1024:.1f} MB)", flush=True)
    print(f" -> {exe_short.name}", flush=True)
    return exe_path


# --------------------------------------------------------------------------- #
# placeholder packages — local name inspection only, never a release
# --------------------------------------------------------------------------- #

def _placeholder(binary: Path, outputs: list[Path], kind: str, arch: str, ver: str) -> Path:
    print(
        f"  WARNING: creating placeholder .{kind} packages — these are NOT installable.",
        file=sys.stderr,
    )
    import zipfile

    for out in outputs:
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(binary, arcname=binary.name)
            zf.writestr(
                "EMULATED.txt",
                f"Placeholder .{kind} built on {platform.platform()} — NOT INSTALLABLE.\n"
                f"Build {arch} natively, or let CI do it: .github/workflows/release.yml\n",
            )
        print(f" -> {out.name} (placeholder)", file=sys.stderr)
    return outputs[0]


# --------------------------------------------------------------------------- #

def clean_all() -> None:
    for p in (BUILD, DIST):
        if p.exists():
            print(f"Removing {p} ...")
            shutil.rmtree(p, ignore_errors=True)
    print("Clean done.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Build Lofi and wrap it in the OS-native installer (.deb/.dmg/.exe)."
    )
    p.add_argument("--clean", action="store_true", help="remove build/ and dist/ first")
    p.add_argument("--check", action="store_true", help="run the built binary with --check")
    p.add_argument("--all", action="store_true", help="print the 6-artifact matrix")
    p.add_argument("--name-only", action="store_true", help="print this host's package name")
    p.add_argument("--version", action="store_true", help="print VERSION and exit")
    p.add_argument(
        "--allow-placeholder",
        action="store_true",
        help="when this host cannot build a real package, emit a clearly-marked "
             "placeholder instead of failing (local inspection only)",
    )
    args = p.parse_args(argv)

    if args.version:
        print(version())
        return 0

    if args.all:
        ver = version()
        matrix = [
            ("linux", "amd64", "ubuntu-22.04", "dpkg-deb", "Debian/Ubuntu amd64"),
            ("linux", "arm64", "ubuntu-24.04-arm", "dpkg-deb", "Debian/Ubuntu arm64 — Pi / Graviton"),
            ("macos", "amd64", "macos-15-intel", "pkgbuild + hdiutil", "macOS Intel"),
            ("macos", "arm64", "macos-14", "pkgbuild + hdiutil", "macOS Apple Silicon"),
            ("windows", "amd64", "windows-2022", "makensis (NSIS)", "Windows x64"),
            ("windows", "arm64", "windows-11-arm", "makensis (NSIS)", "Windows ARM64"),
        ]
        print(f"Lofi {ver} — 6 release artifacts (1 native + 1 arm per OS):\n")
        print(f"  {'package':38} {'runner':19} tool")
        print(f"  {'-' * 38} {'-' * 19} {'-' * 22}")
        for os_name, arch, runner, tool, note in matrix:
            print(f"  {package_name(os_name, arch, ver=ver):38} {runner:19} {tool}  ({note})")
        print("\n  binary = PyInstaller one-file; package = OS-native installer")
        print("  Cross-compiling is impossible: build each on its native runner.")
        print("  Verify any package with: python scripts/verify_packages.py dist/")
        return 0

    if args.name_only:
        os_name, arch = detect_os_arch()
        print(package_name(os_name, arch, ver=version()))
        return 0

    if args.clean:
        clean_all()

    try:
        built = build(clean=False, allow_placeholder=args.allow_placeholder)
    except PackagingError as exc:
        print(f"\nPackaging failed: {exc}", file=sys.stderr)
        if args.allow_placeholder:
            print("  (--allow-placeholder: continuing)", file=sys.stderr)
            return 0
        return 1

    if args.check:
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ, LOFI_HOME=td, LOFI_TOKEN="ci-dummy-token-not-used")
            print(f"\nRunning {built} --check ...\n", flush=True)
            rc = subprocess.run([str(built), "--check"], env=env).returncode
            print(f"\n--check exit code: {rc}")
            return 0 if rc in (0, 1) else rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
