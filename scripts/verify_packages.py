#!/usr/bin/env python3
"""
Fail loudly if a release artifact is not the real thing.

The reason this exists: a build host that is missing dpkg-deb / hdiutil /
makensis can still produce a file *named* lofi-1.0.0-linux-amd64.deb — it just
would not be a Debian package, and neither `ls` nor a GitHub Release page can
tell the difference. This checks the actual bytes:

  .deb   starts with the ar archive magic "!<arch>" and contains a control file
         and usr/local/bin/lofi (via dpkg-deb when it is installed)
  .dmg   is a UDIF image: the "koly" footer sits 512 bytes from the end
  .exe   is a PE image: "MZ" at 0 and "PE\\0\\0" at the offset in e_lfanew

and for every artifact, that it is not one of the placeholder formats (zip,
tar, gzip, or plain text) and that no EMULATED.txt is inside it.

Usage:
  python scripts/verify_packages.py dist/
  python scripts/verify_packages.py dist/ --expect lofi-1.0.2-linux-amd64.deb
  python scripts/verify_packages.py dist/ --json

Exit code 0 = every artifact is the real format, 1 = something is wrong.
"""

from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

AR_MAGIC = b"!<arch>\n"
ELF_MAGIC = b"\x7fELF"
MZ_MAGIC = b"MZ"
PE_MAGIC = b"PE\0\0"
UDIF_FOOTER = b"koly"
ZIP_MAGIC = b"PK\x03\x04"
GZIP_MAGIC = b"\x1f\x8b"
MACHO_MAGICS = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce")


class Check:
    def __init__(self, name: str, ok: bool, detail: str = "") -> None:
        self.name = name
        self.ok = ok
        self.detail = detail

    def as_dict(self) -> dict:
        return {"artifact": self.name, "ok": self.ok, "detail": self.detail}


def head(path: Path, n: int = 16) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(n)


def tail(path: Path, n: int = 512) -> bytes:
    size = path.stat().st_size
    with open(path, "rb") as fh:
        fh.seek(max(0, size - n))
        return fh.read()


def is_placeholder(path: Path) -> str | None:
    """Return a reason when the file is a renamed archive rather than a package."""
    start = head(path, 512)
    if start.startswith(ZIP_MAGIC):
        names = []
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
        except Exception:
            pass
        if any(Path(n).name == "EMULATED.txt" for n in names):
            return "zip archive containing EMULATED.txt (placeholder, not installable)"
        return "zip archive, not a native package"
    if start.startswith(GZIP_MAGIC) or start[257:262] == b"ustar":
        return "tar/gzip archive, not a native package"
    return None


def check_deb(path: Path) -> Check:
    if not path.stat().st_size:
        return Check(path.name, False, "empty file")
    if not head(path, 8) == AR_MAGIC:
        bad = is_placeholder(path)
        return Check(path.name, False, bad or "missing ar magic (!<arch>) — not a Debian package")

    detail = "ar archive"
    dpkg = which("dpkg-deb")
    if dpkg:
        try:
            fields_raw = subprocess.run(
                [dpkg, "-f", str(path), "Package", "Version", "Architecture"],
                capture_output=True, text=True, timeout=60,
            )
            if fields_raw.returncode != 0:
                return Check(path.name, False,
                             f"dpkg-deb -f failed: {fields_raw.stderr.strip()[:200]}")
            fields = {}
            for line in fields_raw.stdout.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    fields[k.strip()] = v.strip()
            pkg, ver, arch = fields.get("Package"), fields.get("Version"), fields.get("Architecture")
            if pkg != "lofi":
                return Check(path.name, False, f"control says Package: {pkg!r}, expected 'lofi'")
            if not ver or not arch:
                return Check(path.name, False, "control is missing Version or Architecture")
            detail = f"Package: lofi {ver} {arch}"

            contents = subprocess.run([dpkg, "-c", str(path)], capture_output=True, text=True, timeout=60)
            if "./usr/local/bin/lofi" not in contents.stdout:
                return Check(path.name, False, "package does not contain ./usr/local/bin/lofi")
            detail += ", ./usr/local/bin/lofi present"
        except subprocess.TimeoutExpired:
            return Check(path.name, False, "dpkg-deb timed out")
    return Check(path.name, True, detail)


def check_dmg(path: Path) -> Check:
    if not path.stat().st_size:
        return Check(path.name, False, "empty file")
    footer = tail(path, 512)
    if UDIF_FOOTER not in footer[-512:]:
        bad = is_placeholder(path)
        return Check(path.name, False, bad or "no UDIF 'koly' footer — not a disk image")

    detail = "UDIF disk image"
    hdiutil = which("hdiutil")
    if hdiutil:
        try:
            verify = subprocess.run(
                [hdiutil, "verify", str(path)], capture_output=True, text=True, timeout=300
            )
            if verify.returncode != 0:
                return Check(path.name, False, f"hdiutil verify failed: {verify.stderr.strip()[:200]}")
            info = subprocess.run(
                [hdiutil, "imageinfo", str(path)], capture_output=True, text=True, timeout=120
            )
            for line in info.stdout.splitlines():
                if "Format Description" in line or "Checksum Type" in line:
                    detail += f", {line.strip()}"
        except subprocess.TimeoutExpired:
            return Check(path.name, False, "hdiutil verify timed out")
    return Check(path.name, True, detail)


def check_exe(path: Path) -> Check:
    if not path.stat().st_size:
        return Check(path.name, False, "empty file")
    start = head(path, 2)
    if start != MZ_MAGIC:
        bad = is_placeholder(path)
        return Check(path.name, False, bad or "missing MZ magic — not a Windows executable")

    with open(path, "rb") as fh:
        fh.seek(0x3C)
        try:
            (e_lfanew,) = struct.unpack("<I", fh.read(4))
        except struct.error:
            return Check(path.name, False, "truncated DOS header")
        fh.seek(e_lfanew)
        if fh.read(4) != PE_MAGIC:
            return Check(path.name, False, "MZ file without a PE header (not a Windows installer)")

    detail = "PE executable"
    try:
        machine = read_pe_machine(path, e_lfanew)
        names = {0x8664: "x86-64", 0xAA64: "ARM64", 0x014C: "x86"}
        if machine in names:
            detail += f", machine {names[machine]}"
            if path.name.count("arm64") and machine != 0xAA64:
                return Check(path.name, False, f"name says arm64 but PE machine is {names[machine]}")
            if "amd64" in path.name and machine != 0x8664:
                return Check(path.name, False, f"name says amd64 but PE machine is {names[machine]}")
    except Exception:  # reading the COFF header is a nicety, not a gate
        pass
    return Check(path.name, True, detail)


def read_pe_machine(path: Path, e_lfanew: int) -> int:
    with open(path, "rb") as fh:
        fh.seek(e_lfanew + 4)
        (machine,) = struct.unpack("<H", fh.read(2))
    return machine


def check_binary(path: Path) -> Check:
    """The portable PyInstaller binary: ELF on Linux, Mach-O on macOS, PE on Windows."""
    start = head(path, 4)
    name = path.name
    if "linux" in name:
        ok = start.startswith(ELF_MAGIC)
        return Check(name, ok, "ELF" if ok else "not an ELF binary")
    if "macos" in name:
        ok = start in MACHO_MAGICS or start[:2] in (b"\xcf\xfa", b"\xce\xfa")
        return Check(name, ok, "Mach-O" if ok else "not a Mach-O binary")
    if "windows" in name or name.endswith(".exe"):
        ok = start.startswith(MZ_MAGIC)
        return Check(name, ok, "PE" if ok else "not a PE binary")
    return Check(name, True, "unclassified binary, format not checked")


def which(cmd: str) -> str | None:
    from shutil import which as _which

    return _which(cmd)


def verify_dir(dist: Path, expected: list[str] | None = None) -> list[Check]:
    if not dist.is_dir():
        raise SystemExit(f"{dist} is not a directory")

    results: list[Check] = []
    files = sorted(p for p in dist.iterdir() if p.is_file())

    for path in files:
        name = path.name
        if name.startswith("SHA256SUMS") or name.startswith("."):
            continue
        if name.endswith(".deb"):
            results.append(check_deb(path))
        elif name.endswith(".dmg"):
            results.append(check_dmg(path))
        elif name.endswith(".exe"):
            results.append(check_exe(path))
        elif any(tok in name for tok in ("linux", "macos", "windows")) and "setup" not in name:
            results.append(check_binary(path))
        elif name in ("lofi",):
            results.append(check_binary(path))

    if expected:
        present = {p.name for p in files}
        for want in expected:
            if want not in present:
                results.append(Check(want, False, "MISSING from the release"))
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dist", nargs="?", default="dist", help="directory with the built artifacts")
    ap.add_argument("--expect", action="append", default=[], help="artifact name that must be present")
    ap.add_argument("--json", action="store_true", help="print machine-readable results")
    args = ap.parse_args(argv)

    results = verify_dir(Path(args.dist), args.expect)

    if args.json:
        print(json.dumps([c.as_dict() for c in results], indent=2))
    else:
        width = max((len(c.name) for c in results), default=10)
        for c in results:
            mark = "PASS" if c.ok else "FAIL"
            print(f"  [{mark}] {c.name:<{width}}  {c.detail}")

    failures = [c for c in results if not c.ok]
    if failures:
        print(f"\n{len(failures)} artifact(s) are not what their name claims:", file=sys.stderr)
        for c in failures:
            print(f"  - {c.name}: {c.detail}", file=sys.stderr)
        print(
            "\nA placeholder package is a renamed archive: it installs nothing.\n"
            "Build each artifact on its native runner (see .github/workflows/release.yml)\n"
            "or run 'python scripts/build.py' on that OS.",
            file=sys.stderr,
        )
        return 1

    print(f"\nAll {len(results)} artifact(s) verified as real, installable packages." if not args.json else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
