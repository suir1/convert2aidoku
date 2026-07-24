from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_CREDENTIAL_ENV_MARKERS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD")
_CREDENTIAL_ENV_NAMES = frozenset(
    {
        "AUTHORIZATION",
        "HTTP_AUTHORIZATION",
        "PROXY_AUTHORIZATION",
    }
)
type CommandFailure = Literal["timeout", "os_error"]


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    cwd: Path | None = None
    timeout_seconds: float | None = None
    failure: CommandFailure | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.failure is None and self.returncode == 0


def cargo_home_bin() -> Path:
    return Path(os.getenv("CARGO_HOME", Path.home() / ".cargo")) / "bin"


def command_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the environment shared by untrusted external commands.

    Proxy variables and ordinary process configuration are retained. Provider
    credentials are removed, and Cargo's conventional bin directory is made
    discoverable for commands installed by rustup.
    """
    env = dict(os.environ if base is None else base)
    for name in tuple(env):
        upper = name.upper()
        if upper in _CREDENTIAL_ENV_NAMES or any(
            marker in upper for marker in _CREDENTIAL_ENV_MARKERS
        ):
            env.pop(name, None)
    cargo_bin = str(cargo_home_bin())
    path_parts = env.get("PATH", "").split(os.pathsep)
    if cargo_bin not in path_parts:
        env["PATH"] = cargo_bin + os.pathsep + env.get("PATH", "")
    return env


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def execute_command(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
    environment: Mapping[str, str] | None = None,
    capture_output: bool = True,
) -> CommandResult:
    """Execute one command without a shell and return normalized execution facts."""
    normalized = tuple(command)
    if not normalized:
        raise ValueError("command must contain an executable")
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(normalized),
            cwd=cwd,
            env=command_environment(environment),
            capture_output=capture_output,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            command=normalized,
            returncode=None,
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr),
            duration_seconds=time.monotonic() - started,
            cwd=cwd,
            timeout_seconds=timeout,
            failure="timeout",
            error=str(exc),
        )
    except OSError as exc:
        return CommandResult(
            command=normalized,
            returncode=None,
            stdout="",
            stderr="",
            duration_seconds=time.monotonic() - started,
            cwd=cwd,
            timeout_seconds=timeout,
            failure="os_error",
            error=str(exc),
        )
    return CommandResult(
        command=normalized,
        returncode=completed.returncode,
        stdout=_text(completed.stdout),
        stderr=_text(completed.stderr),
        duration_seconds=time.monotonic() - started,
        cwd=cwd,
        timeout_seconds=timeout,
    )
