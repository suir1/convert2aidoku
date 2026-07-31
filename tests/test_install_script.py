from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


def _dry_run(
    os_name: str,
    *,
    package_manager: str | None = None,
    install_ref: str | None = None,
) -> str:
    environment = {
        **os.environ,
        "C2A_INSTALL_DRY_RUN": "1",
        "C2A_INSTALL_FORCE_MISSING": "1",
        "C2A_INSTALL_OS": os_name,
        "C2A_INSTALL_USE_LOCAL": "0",
        "HOME": "/tmp/c2a-installer-home",
    }
    if package_manager is not None:
        environment["C2A_INSTALL_PACKAGE_MANAGER"] = package_manager
    if install_ref is not None:
        environment["C2A_INSTALL_REF"] = install_ref
    result = subprocess.run(
        ["sh", str(INSTALLER)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout + result.stderr


def test_installer_has_valid_posix_shell_syntax() -> None:
    subprocess.run(["sh", "-n", str(INSTALLER)], check=True)


def test_macos_dry_run_bootstraps_every_layer() -> None:
    output = _dry_run("Darwin")

    assert "Homebrew is missing" in output
    assert "brew install git" in output
    assert "brew install jadx" in output
    assert "uv/install.sh" in output
    assert "tool install --python 3.13 --force" in output
    assert "convert2aidoku/archive/main.zip" in output
    assert "c2a setup --yes" in output
    assert "c2a doctor" in output


def test_linux_dry_run_installs_packages_and_checksum_pinned_jadx() -> None:
    output = _dry_run("Linux", package_manager="apt-get")

    assert "sudo apt-get update" in output
    assert "openjdk-17-jre-headless" in output
    assert "jadx-1.5.3.zip" in output
    assert "8280f3799c0273fe797a2bcd90258c943e451fd195f13d05400de5e6451d15ec" in output
    assert "tool install --python 3.13 --force" in output
    assert "c2a setup --yes" in output
    assert "c2a doctor" in output


def test_installer_rejects_unsupported_operating_system() -> None:
    result = subprocess.run(
        ["sh", str(INSTALLER)],
        capture_output=True,
        text=True,
        env={**os.environ, "C2A_INSTALL_OS": "Windows_NT"},
    )

    assert result.returncode == 1
    assert "Unsupported operating system" in result.stderr


def test_installer_can_pin_a_release_tag() -> None:
    output = _dry_run("Darwin", install_ref="v0.1.0-beta.1")

    assert "convert2aidoku/archive/v0.1.0-beta.1.zip" in output
