from __future__ import annotations

import pytest

from convert2aidoku import toolchain
from convert2aidoku.command_execution import CommandFailure, CommandResult
from convert2aidoku.errors import ToolchainError


def _result(
    command: list[str],
    *,
    returncode: int | None = 0,
    stdout: str = "",
    stderr: str = "",
    failure: CommandFailure | None = None,
    error: str = "",
) -> CommandResult:
    return CommandResult(
        command=tuple(command),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.1,
        failure=failure,
        error=error,
    )


def test_doctor_command_uses_normalized_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        toolchain,
        "execute_command",
        lambda command, **_kwargs: _result(command, stdout="git version 2.50\n"),
    )

    assert toolchain._command_output(["git", "--version"]) == (True, "git version 2.50")


def test_doctor_command_preserves_execution_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        toolchain,
        "execute_command",
        lambda command, **_kwargs: _result(
            command,
            returncode=None,
            failure="timeout",
            error="Command timed out after 30 seconds",
        ),
    )

    assert toolchain._command_output(["git", "--version"]) == (
        False,
        "Command timed out after 30 seconds",
    )


def test_setup_inherits_terminal_output(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_execute(command: list[str], **kwargs: object) -> CommandResult:
        captured.update(kwargs)
        return _result(command)

    monkeypatch.setattr(toolchain, "execute_command", fake_execute)

    toolchain._run_setup(["rustup", "default", "stable"])

    assert captured["capture_output"] is False
    assert captured["timeout"] == 1_200


def test_setup_error_text_remains_domain_specific(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        toolchain,
        "execute_command",
        lambda command, **_kwargs: _result(command, returncode=9),
    )

    with pytest.raises(
        ToolchainError,
        match=r"setup command failed \(9\): cargo install package",
    ):
        toolchain._run_setup(["cargo", "install", "package"])
