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

import json
import os
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


def test_pyinstaller_spec_bundles_pynacls_cffi_backend():
    """Without the CFFI extension, PyNaCl is present but Discord disables voice."""
    spec = (ROOT / "lofi.spec").read_text(encoding="utf-8")
    assert '"nacl._sodium"' in spec
    assert '"_cffi_backend"' in spec


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


def _write_pe(path: Path, machine: int) -> None:
    """A minimal but structurally valid PE: MZ header, e_lfanew, PE\0\0, COFF."""
    import struct

    e_lfanew = 0x80
    blob = bytearray(e_lfanew)
    blob[0:2] = b"MZ"
    blob[0x3C:0x40] = struct.pack("<I", e_lfanew)
    blob += b"PE\0\0"
    blob += struct.pack("<H", machine)          # Machine
    blob += struct.pack("<H", 1)                # NumberOfSections
    blob += b"\0" * 16                          # TimeDateStamp..SizeOfOptionalHeader
    blob += struct.pack("<H", 0)                # Characteristics
    path.write_bytes(bytes(blob))


def test_verifier_accepts_an_nsis_installer_that_is_a_32bit_pe(tmp_path):
    """NSIS installers are 32-bit on purpose and run on x64 and ARM64 Windows."""
    installer = tmp_path / "lofi-1.2.3-windows-amd64-setup.exe"
    _write_pe(installer, 0x014C)
    result = verifier.check_exe(installer)
    assert result.ok, result.detail
    assert "32-bit" in result.detail


def test_verifier_still_catches_a_portable_binary_of_the_wrong_arch(tmp_path):
    binary = tmp_path / "lofi-1.2.3-windows-arm64.exe"
    _write_pe(binary, 0x8664)  # x86-64 binary named arm64
    result = verifier.check_exe(binary)
    assert not result.ok
    assert "arm64" in result.detail

    good = tmp_path / "lofi-1.2.3-windows-arm64-correct.exe"
    _write_pe(good, 0xAA64)
    assert verifier.check_exe(good).ok


def test_verifier_reports_missing_expected_artifacts(tmp_path):
    results = verifier.verify_dir(tmp_path, expected=["lofi-1.2.3-linux-amd64.deb"])
    assert results and not results[0].ok and "MISSING" in results[0].detail


# --------------------------------------------------------------------------- #
# the installers ask for the token, once
# --------------------------------------------------------------------------- #

def _deb_script(tmp_path: Path) -> Path:
    """The real postinst, with its absolute paths pointed at a throwaway prefix.

    A maintainer script is written for the machine it installs on, so the only
    way to run one here is to rewrite the paths at the top - which is also what
    keeps the test honest: it executes the real file, not a paraphrase of it.
    """
    example = tmp_path / "share" / "lofi.env"
    example.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "packaging" / "debian" / "lofi.env.example", example)
    text = (ROOT / "packaging" / "debian" / "postinst").read_text()
    for old, new in (
        ("BIN=/usr/local/bin/lofi", f"BIN={tmp_path}/bin/lofi"),
        ("ENVFILE=/etc/lofi/lofi.env", f"ENVFILE={tmp_path}/etc/lofi.env"),
        ("EXAMPLE=/usr/share/lofi/lofi.env", f"EXAMPLE={example}"),
        ("mkdir -p /etc/lofi", f"mkdir -p {tmp_path}/etc"),
        ("chmod 0755 /etc/lofi", f"chmod 0755 {tmp_path}/etc"),
        ("-d /etc/lofi", f"-d {tmp_path}/etc"),
        ("systemctl daemon-reload >/dev/null 2>&1 || true", "true"),
    ):
        assert old in text, old
        text = text.replace(old, new)
    script = tmp_path / "postinst"
    script.write_text(text)
    script.chmod(0o755)
    return script


def _macos_script(tmp_path: Path) -> Path:
    """The real postinstall, with the console user's home pointed at tmp_path."""
    text = (ROOT / "packaging" / "macos" / "postinstall").read_text()
    for old, new in (
        ("CONSOLE_USER=\"$(/usr/bin/stat -f%Su /dev/console 2>/dev/null || true)\"",
         'CONSOLE_USER="somebody"'),
        ('CONF_DIR="/Users/$CONSOLE_USER/Library/Application Support/lofi"',
         f'CONF_DIR="{tmp_path}/Library/Application Support/lofi"'),
        ("/usr/bin/osascript", f"{tmp_path}/no-osascript-here"),
        ('chmod 0755 "$BIN"', "true"),   # no payload to make executable here
    ):
        assert old in text, old
        text = text.replace(old, new)
    script = tmp_path / "postinstall"
    script.write_text(text)
    script.chmod(0o755)
    return script


def _installer_env() -> dict:
    """The environment a maintainer script runs in, minus what the shell already set.

    ``LOFI_TOKEN`` in the ambient environment means "the token is already
    configured", which is exactly the case where the script must not ask - so a
    test that wants the prompt has to take it away first.
    """
    return {k: v for k, v in os.environ.items() if k not in ("LOFI_TOKEN", "DEBIAN_FRONTEND")}


def _run_with_a_terminal(script: Path, answer: str, timeout: float = 30.0):
    """Run a script with stdin/stdout on a pty, the way a person would see it.

    Returns ``(output, exit code)``. A maintainer script that blocks waiting for
    an answer is exactly the bug this is here to catch, so the read side is
    bounded and a hang is a failure rather than a slow test.
    """
    import select

    master, slave = os.openpty()
    proc = None
    try:
        proc = subprocess.Popen(
            ["/bin/sh", str(script)], stdin=slave, stdout=slave, stderr=slave,
            env=_installer_env(),
        )
        os.close(slave)
        slave = None
        os.write(master, answer.encode())
        chunks = []
        while True:
            ready, _, _ = select.select([master], [], [], timeout)
            if not ready:
                proc.kill()
                raise AssertionError(f"{script.name} blocked waiting for input")
            try:
                data = os.read(master, 4096)
            except OSError:  # the child closed its end
                break
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks).decode(errors="replace"), proc.wait(timeout=timeout)
    finally:
        os.close(master)
        if slave is not None:
            os.close(slave)
        if proc is not None and proc.poll() is None:  # pragma: no cover - safety net
            proc.kill()
            proc.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell script")
def test_the_debian_postinst_asks_for_the_token_once(tmp_path):
    output, code = _run_with_a_terminal(_deb_script(tmp_path), "MTk4NzY1NDMy.MTEyMjMzNDQ1.NjY3ODg5\n")

    assert code == 0
    env = (tmp_path / "etc" / "lofi.env").read_text()
    assert "LOFI_TOKEN=MTk4NzY1NDMy.MTEyMjMzNDQ1.NjY3ODg5" in env
    assert "#LOFI_TOKEN=your-bot-token-here" not in env   # the placeholder is replaced
    assert "Saved to" in output
    assert oct((tmp_path / "etc" / "lofi.env").stat().st_mode & 0o777) == "0o600"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell script")
def test_the_debian_postinst_never_asks_when_there_is_nobody_to_answer(tmp_path):
    """apt, Docker builds and configuration management: no tty, no hang."""
    script = _deb_script(tmp_path)

    result = subprocess.run(
        ["/bin/sh", str(script)], input="", capture_output=True, text=True,
        timeout=60, env=_installer_env(),
    )

    assert result.returncode == 0
    assert "Discord bot token" not in result.stdout       # never prompts
    assert "sudo editor" in result.stdout                 # says where to put it instead
    # the example's commented placeholder is all that is in there
    assert not any(
        line.startswith("LOFI_TOKEN=") for line in (tmp_path / "etc" / "lofi.env").read_text().splitlines()
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell script")
def test_the_debian_postinst_leaves_an_existing_token_alone(tmp_path):
    """An upgrade must not re-ask, and must not overwrite what is there."""
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "lofi.env").write_text("LOFI_TOKEN=already.here.token\n")

    output, code = _run_with_a_terminal(_deb_script(tmp_path), "a-token-that-must-not-be-read\n")

    assert code == 0
    assert "Discord bot token" not in output
    assert (tmp_path / "etc" / "lofi.env").read_text() == "LOFI_TOKEN=already.here.token\n"


def test_the_debian_postinst_prompt_is_guarded_on_every_side():
    text = (ROOT / "packaging" / "debian" / "postinst").read_text()
    assert "[ -t 0 ]" in text                              # a terminal, or no prompt
    assert "noninteractive" in text                        # debconf's own opt-out
    assert "${LOFI_TOKEN:-}" in text                       # already exported: no prompt
    # and the answer lands where the systemd unit reads it from
    assert "printf 'LOFI_TOKEN=%s\\n'" in text


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell script")
def test_the_macos_postinstall_asks_for_the_token_once(tmp_path):
    output, code = _run_with_a_terminal(_macos_script(tmp_path), "MTk4NzY1NDMy.MTEyMjMzNDQ1.NjY3ODg5\n")

    assert code == 0
    config = tmp_path / "Library" / "Application Support" / "lofi" / "config.json"
    assert json.loads(config.read_text())["bot_token"] == "MTk4NzY1NDMy.MTEyMjMzNDQ1.NjY3ODg5"
    assert "Saved to" in output
    assert oct(config.stat().st_mode & 0o777) == "0o600"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell script")
def test_the_macos_postinstall_never_asks_when_a_token_is_already_stored(tmp_path):
    config_dir = tmp_path / "Library" / "Application Support" / "lofi"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text('{"bot_token": "already.here.token"}\n')

    output, code = _run_with_a_terminal(_macos_script(tmp_path), "a-token-that-must-not-be-read\n")

    assert code == 0
    assert "Discord bot token" not in output
    assert json.loads((config_dir / "config.json").read_text())["bot_token"] == "already.here.token"


def test_the_macos_postinstall_refuses_a_token_that_would_break_the_json():
    """It is interpolated into a JSON file, so quotes and backslashes are refused."""
    save_token = (ROOT / "packaging" / "macos" / "postinstall").read_text()
    save_token = save_token.split("save_token() {", 1)[1].split("\n}\n", 1)[0]
    assert '\\"' in save_token      # a double quote
    assert "\\\\" in save_token    # a backslash
    assert "2>/dev/null || true" in save_token or "return 1" in save_token


def test_the_nsis_installer_asks_for_the_token_on_one_page():
    nsi = (ROOT / "packaging" / "windows" / "installer.nsi").read_text()
    assert "Page custom TokenPage TokenPageLeave" in nsi
    assert "nsDialogs::Create 1018" in nsi                  # MUI2 brings nsDialogs in
    assert "${NSD_CreateText}" in nsi
    # pre-filled from what is stored, so an upgrade keeps its token
    assert 'ReadRegStr $TokenValue HKCU "Environment" "LOFI_TOKEN"' in nsi
    # ...and written to the user environment, which is what LOFI_TOKEN reads
    assert 'WriteRegStr HKCU "Environment" "LOFI_TOKEN" "$TokenValue"' in nsi


def test_the_nsis_installer_never_deletes_a_token_it_did_not_put_there():
    """LOFI_TOKEN is a user setting, not part of the payload: leave it alone."""
    nsi = (ROOT / "packaging" / "windows" / "installer.nsi").read_text()
    uninstaller = nsi.split('Section "Uninstall"', 1)[1]
    assert "DeleteRegValue" not in uninstaller
    assert "LOFI_TOKEN" in uninstaller                     # it says where it is instead


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
