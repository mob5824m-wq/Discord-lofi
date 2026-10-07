"""The 6 OS-native installers (.deb / .dmg / .exe) — naming, templates, and the
check that a package is what its file name claims.

These tests exist because packaging fails in a particularly quiet way: a host
without dpkg-deb / hdiutil / makensis can still write a file *called*
lofi-1.0.0-linux-amd64.deb, and nothing about it looks wrong until a user tries
to install it. The real build happens on native runners in CI, so what is
testable here is the naming, the templates, the refusal to fake a package, and
the byte-level verifier that gates the release.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build as buildscript  # noqa: E402
import verify_packages as verifier  # noqa: E402

MATRIX = [
    ("linux", "amd64", "lofi-1.2.3-linux-amd64.deb"),
    ("linux", "arm64", "lofi-1.2.3-linux-arm64.deb"),
    ("macos", "amd64", "lofi-1.2.3-macos-amd64.dmg"),
    ("macos", "arm64", "lofi-1.2.3-macos-arm64.dmg"),
    ("windows", "amd64", "lofi-1.2.3-windows-amd64-setup.exe"),
    ("windows", "arm64", "lofi-1.2.3-windows-arm64-setup.exe"),
]


# --------------------------------------------------------------------------- #
# naming
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("os_name,arch,expected", MATRIX)
def test_package_names_are_stable(os_name, arch, expected):
    """The release workflow matches artifact names by string; they must not drift."""
    assert buildscript.package_name(os_name, arch, ver="1.2.3") == expected


def test_short_names_have_no_version_for_latest_downloads():
    assert buildscript.package_name("linux", "amd64", ver=None) == "lofi-linux-amd64.deb"
    assert buildscript.package_name("windows", "arm64", ver=None) == "lofi-windows-arm64-setup.exe"


def test_installer_name_never_collides_with_the_portable_binary():
    """On Windows both are .exe, so the installer carries -setup."""
    binary = buildscript.artifact_name("windows", "amd64", ver="1.2.3")
    installer = buildscript.package_name("windows", "amd64", ver="1.2.3")
    assert binary == "lofi-1.2.3-windows-amd64.exe"
    assert installer != binary


@pytest.mark.parametrize("os_name,arch,_", MATRIX)
def test_binary_name(os_name, arch, _):
    name = buildscript.artifact_name(os_name, arch, ver="1.2.3")
    assert name.startswith(f"lofi-1.2.3-{os_name}-{arch}")
    assert name.endswith(".exe") == (os_name == "windows")


def test_all_matrix_matches_the_release_workflow():
    """`build.py --all` and the naming helpers must agree, at the real version."""
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build.py"), "--all"],
        capture_output=True, text=True, check=True,
    )
    ver = buildscript.version()
    for os_name, arch, _ in MATRIX:
        expected = buildscript.package_name(os_name, arch, ver=ver)
        assert expected in out.stdout, f"{expected} missing from build.py --all"
        assert ver in out.stdout


def test_detect_os_arch_never_returns_surprises(monkeypatch):
    monkeypatch.setenv("LOFI_BUILD_OS", "debian")
    monkeypatch.setenv("LOFI_BUILD_ARCH", "aarch64")
    assert buildscript.detect_os_arch() == ("linux", "arm64")
    monkeypatch.setenv("LOFI_BUILD_OS", "mac")
    monkeypatch.setenv("LOFI_BUILD_ARCH", "x64")
    assert buildscript.detect_os_arch() == ("macos", "amd64")


# --------------------------------------------------------------------------- #
# templates
# --------------------------------------------------------------------------- #

def _rendered(pkg_file: str, **vars) -> str:
    src = ROOT / "packaging" / pkg_file
    return buildscript.render(src, Path("/tmp") / Path(pkg_file).name, **vars).read_text()


def test_debian_control_renders_and_has_no_comments():
    """dpkg-deb rejects '#' comment lines in control; the notes live in README."""
    text = _rendered("debian/control", VERSION="1.2.3", ARCH="arm64", INSTALLED_SIZE="42")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert not any(ln.lstrip().startswith("#") for ln in lines)
    fields = {}
    for line in lines:
        if line.startswith((" ", "\t")):
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    assert fields["Package"] == "lofi"
    assert fields["Version"] == "1.2.3"
    assert fields["Architecture"] == "arm64"
    assert fields["Installed-Size"] == "42"
    # the binary is self-contained: requiring python3 here would pull in an
    # interpreter the PyInstaller build never uses
    assert "python3" not in fields["Depends"]
    assert "ffmpeg" in fields["Depends"] and "libopus0" in fields["Depends"]


def test_maintainer_scripts_are_executable_shell_and_safe_to_rerun():
    for script in ("postinst", "prerm", "postrm"):
        text = (ROOT / "packaging" / "debian" / script).read_text()
        assert text.startswith("#!/bin/sh")
        assert "exit 0" in text
        # an upgrade must never delete the user's token
        if script == "postrm":
            assert "purge" in text


def test_postinst_reads_the_example_from_share_not_doc():
    """slim Debian images configure dpkg to skip /usr/share/doc/*."""
    text = (ROOT / "packaging" / "debian" / "postinst").read_text()
    assert "/usr/share/lofi/lofi.env" in text
    assert "/usr/share/doc/lofi" not in text.split("EXAMPLE=")[1].split("\n")[0]


def test_systemd_unit_is_present_and_not_hardened_into_failure():
    unit = (ROOT / "packaging" / "debian" / "lofi.service").read_text()
    assert "ExecStart=/usr/local/bin/lofi" in unit
    assert "EnvironmentFile=-/etc/lofi/lofi.env" in unit
    assert "LOFI_HOME" in unit


def test_macos_readme_renders_placeholders():
    text = _rendered("macos/ReadMe.txt.in", VERSION="1.2.3", ARCH="arm64 (Apple Silicon)")
    assert "@VERSION@" not in text and "@ARCH@" not in text
    assert "Lofi-1.2.3.pkg" in text
    assert "brew install ffmpeg opus" in text


def test_nsis_script_declares_the_pieces_the_docs_promise():
    nsi = (ROOT / "packaging" / "windows" / "installer.nsi").read_text()
    for needed in (
        "RequestExecutionLevel admin",
        "WriteUninstaller",
        "CurrentVersion\\Uninstall\\Lofi",
        "AddToMachinePath",
        "RemoveFromMachinePath",
        "SetEnvironmentVariable('Path'",
    ):
        assert needed in nsi, f"installer.nsi is missing {needed}"


def test_nsis_powershell_strings_have_no_unescaped_dollars():
    """'$' is NSIS's escape character: a bare $_ in an inline command breaks it."""
    nsi = (ROOT / "packaging" / "windows" / "installer.nsi").read_text()
    for line in nsi.splitlines():
        if "powershell" not in line:
            continue
        line = line.replace("$$", "").replace("${Dir}", "")
        for i, ch in enumerate(line):
            if ch != "$":
                continue
            rest = line[i + 1:]
            assert rest.startswith(("INSTDIR", "SYSDIR", "WINDIR", "{")), (
                f"unescaped $ in: {line.strip()[:90]}"
            )


# --------------------------------------------------------------------------- #
# a fake package must be refused, not shipped
# --------------------------------------------------------------------------- #

def test_packaging_refuses_to_emit_a_placeholder_by_default(tmp_path):
    """No dpkg-deb/hdiutil/makensis here -> error with the fix, never a fake."""
    binary = tmp_path / "lofi"
    binary.write_bytes(b"\x7fELF fake")
    binary.chmod(0o755)

    for fn, os_name, tool in (
        (buildscript.make_dmg, "macos", "hdiutil"),
        (buildscript.make_windows_installer, "windows", "makensis"),
    ):
        if sys.platform == "darwin" and tool == "hdiutil":
            continue
        if sys.platform == "win32" and tool == "makensis":
            continue
        with pytest.raises(buildscript.PackagingError):
            fn(binary, "amd64", "1.2.3")
        # and the opt-in still exists for local name inspection
        out = fn(binary, "amd64", "1.2.3", allow_placeholder=True)
        assert out.exists()
        with zipfile.ZipFile(out) as zf:
            assert "EMULATED.txt" in zf.namelist()


def test_verifier_rejects_a_zip_renamed_as_a_package(tmp_path):
    for name in ("lofi-1.2.3-linux-amd64.deb", "lofi-1.2.3-macos-arm64.dmg",
                 "lofi-1.2.3-windows-amd64-setup.exe"):
        fake = tmp_path / name
        with zipfile.ZipFile(fake, "w") as zf:
            zf.writestr("EMULATED.txt", "placeholder")
        result = verifier.check_deb(fake) if fake.suffix == ".deb" else (
            verifier.check_dmg(fake) if fake.suffix == ".dmg" else verifier.check_exe(fake)
        )
        assert not result.ok
        assert "EMULATED.txt" in result.detail


def test_verifier_reports_missing_expected_artifacts(tmp_path):
    results = verifier.verify_dir(tmp_path, expected=["lofi-1.2.3-linux-amd64.deb"])
    assert results and not results[0].ok and "MISSING" in results[0].detail


# --------------------------------------------------------------------------- #
# a real .deb, when this host can build one
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(
    not (shutil.which("dpkg-deb") and sys.platform.startswith("linux")),
    reason="dpkg-deb is only available on Debian/Ubuntu",
)
def test_deb_is_a_real_installable_package(tmp_path, monkeypatch):
    monkeypatch.setattr(buildscript, "DIST", tmp_path)
    binary = tmp_path / "lofi-fake"
    shutil.copy2(shutil.which("true") or "/bin/true", binary)
    binary.chmod(0o755)

    deb = buildscript.make_deb(binary, "amd64", "1.2.3")

    assert deb.read_bytes()[:8] == b"!<arch>\n"
    info = subprocess.run(["dpkg-deb", "-I", str(deb)], capture_output=True, text=True, check=True)
    assert "Package: lofi" in info.stdout
    assert "Version: 1.2.3" in info.stdout
    contents = subprocess.run(["dpkg-deb", "-c", str(deb)], capture_output=True, text=True, check=True)
    for path in ("./usr/local/bin/lofi", "./lib/systemd/system/lofi.service",
                 "./usr/share/lofi/lofi.env"):
        assert path in contents.stdout, f"{path} missing from the package"

    result = verifier.check_deb(deb)
    assert result.ok, result.detail
