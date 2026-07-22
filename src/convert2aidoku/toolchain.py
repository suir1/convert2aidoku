from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from .constants import AIDOKU_RS_REPOSITORY, AIDOKU_RS_REV
from .errors import ToolchainError


@dataclass(frozen=True)
class ToolStatus:
    name: str
    available: bool
    path: str | None = None
    detail: str = ""


def cargo_home_bin() -> Path:
    return Path(os.getenv("CARGO_HOME", Path.home() / ".cargo")) / "bin"


def tool_environment() -> dict[str, str]:
    env = os.environ.copy()
    # Generated Rust and third-party build scripts must never inherit provider
    # credentials.  The AI client reads the key in-process; cargo/rustup/aidoku
    # have no reason to receive it.
    for name in tuple(env):
        upper = name.upper()
        if name == "C2A_API_KEY" or any(
            marker in upper for marker in ("API_KEY", "SECRET", "TOKEN", "PASSWORD")
        ):
            env.pop(name, None)
    cargo_bin = str(cargo_home_bin())
    path_parts = env.get("PATH", "").split(os.pathsep)
    if cargo_bin not in path_parts:
        env["PATH"] = cargo_bin + os.pathsep + env.get("PATH", "")
    return env


def find_tool(name: str) -> str | None:
    return shutil.which(name, path=tool_environment().get("PATH"))


def _command_output(command: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            env=tool_environment(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (result.stdout or result.stderr).strip()
    return result.returncode == 0, output


def doctor() -> list[ToolStatus]:
    statuses = [
        ToolStatus(
            "python",
            sys.version_info >= (3, 12),
            sys.executable,
            platform.python_version(),
        )
    ]
    tools = (
        "git",
        "rustup",
        "cargo",
        "rustfmt",
        "cargo-clippy",
        "jadx",
        "aidoku",
        "aidoku-test-runner",
    )
    for name in tools:
        path = find_tool(name)
        detail = ""
        if path and name in {"git", "rustup", "cargo"}:
            ok, detail = _command_output([path, "--version"])
            if not ok:
                detail = detail or "version check failed"
        elif path and name == "aidoku":
            # aidoku-cli does not expose a conventional --version flag.  Use
            # its help output as a non-mutating availability check instead of
            # reporting a healthy installation as broken.
            ok, detail = _command_output([path, "--help"])
            if not ok:
                detail = detail or "version check failed"
        statuses.append(ToolStatus(name, path is not None, path, detail))

    rustup = find_tool("rustup")
    target_available = False
    detail = "rustup is not installed"
    if rustup:
        ok, output = _command_output([rustup, "target", "list", "--installed"])
        target_available = ok and "wasm32-unknown-unknown" in output.splitlines()
        detail = "installed" if target_available else "missing"
    statuses.append(ToolStatus("wasm32-unknown-unknown", target_available, None, detail))
    return statuses


def _run_setup(command: list[str], *, timeout: int = 1_200) -> None:
    try:
        result = subprocess.run(
            command,
            env=tool_environment(),
            check=False,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolchainError(f"setup command failed to run: {command[0]}: {exc}") from exc
    if result.returncode != 0:
        raise ToolchainError(f"setup command failed ({result.returncode}): {' '.join(command)}")


def _install_rustup() -> None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    triples = {
        ("darwin", "arm64"): "aarch64-apple-darwin",
        ("darwin", "aarch64"): "aarch64-apple-darwin",
        ("darwin", "x86_64"): "x86_64-apple-darwin",
        ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
        ("linux", "arm64"): "aarch64-unknown-linux-gnu",
        ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
        ("windows", "amd64"): "x86_64-pc-windows-msvc",
        ("windows", "x86_64"): "x86_64-pc-windows-msvc",
    }
    target = triples.get((system, machine))
    if target is None:
        raise ToolchainError(f"automatic rustup setup is unsupported on {system}/{machine}")
    executable = "rustup-init.exe" if system == "windows" else "rustup-init"
    url = f"https://static.rust-lang.org/rustup/dist/{target}/{executable}"
    command_tail = ["-y", "--profile", "minimal", "--no-modify-path"]
    try:
        response = httpx.get(url, follow_redirects=True, timeout=120, trust_env=False)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ToolchainError(f"failed to download rustup installer: {exc}") from exc
    with tempfile.TemporaryDirectory(prefix="c2a-rustup-") as temporary:
        installer = Path(temporary) / executable
        installer.write_bytes(response.content)
        if os.name != "nt":
            installer.chmod(0o700)
        _run_setup([str(installer), *command_tail])


def setup_toolchain() -> None:
    if find_tool("rustup") is None:
        _install_rustup()
    rustup = find_tool("rustup")
    if rustup is None:
        raise ToolchainError("rustup installation completed but rustup was not found")
    _run_setup([rustup, "toolchain", "install", "stable", "--profile", "minimal"])
    _run_setup([rustup, "default", "stable"])
    _run_setup([rustup, "component", "add", "rustfmt", "clippy"])
    _run_setup([rustup, "target", "add", "wasm32-unknown-unknown"])

    cargo = find_tool("cargo")
    if cargo is None:
        raise ToolchainError("cargo was not found after rustup setup")
    packages = (
        ("aidoku", "aidoku-cli"),
        ("aidoku-test-runner", "aidoku-test-runner"),
    )
    for _executable, package in packages:
        # Reinstall explicitly after user confirmation so an existing binary
        # from another aidoku-rs revision cannot silently bypass the pin.
        _run_setup(
            [
                cargo,
                "install",
                "--force",
                "--git",
                AIDOKU_RS_REPOSITORY,
                "--rev",
                AIDOKU_RS_REV,
                "--locked",
                package,
            ]
        )
