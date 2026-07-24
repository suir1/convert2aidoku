from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from convert2aidoku import command_execution
from convert2aidoku.command_execution import command_environment, execute_command


def test_execute_command_passes_arguments_and_records_capture_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=7, stdout="standard output", stderr="standard error")

    times = iter((10.0, 10.25))
    monkeypatch.setattr(command_execution.subprocess, "run", fake_run)
    monkeypatch.setattr(command_execution.time, "monotonic", lambda: next(times))

    result = execute_command(
        ["tool", "argument with spaces", "$literal"],
        cwd=tmp_path,
        timeout=12,
        environment={"PATH": "/bin", "KEEP_ME": "yes", "C2A_API_KEY": "secret"},
    )

    assert captured["command"] == ["tool", "argument with spaces", "$literal"]
    assert captured["cwd"] == tmp_path
    assert captured["timeout"] == 12
    assert captured["shell"] is False
    assert captured["check"] is False
    assert captured["capture_output"] is True
    assert captured["text"] is True
    assert captured["env"]["KEEP_ME"] == "yes"  # type: ignore[index]
    assert "C2A_API_KEY" not in captured["env"]  # type: ignore[operator]
    assert result.command == ("tool", "argument with spaces", "$literal")
    assert result.returncode == 7
    assert result.stdout == "standard output"
    assert result.stderr == "standard error"
    assert result.duration_seconds == 0.25
    assert result.cwd == tmp_path
    assert result.timeout_seconds == 12
    assert not result.ok


def test_execute_command_can_inherit_terminal_output(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=None, stderr=None)

    monkeypatch.setattr(command_execution.subprocess, "run", fake_run)

    result = execute_command(["installer", "--yes"], capture_output=False)

    assert captured["capture_output"] is False
    assert result.ok
    assert result.stdout == ""
    assert result.stderr == ""


def test_execute_command_returns_timeout_with_partial_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def time_out(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(
            ["slow"],
            3,
            output=b"partial stdout",
            stderr=b"partial stderr",
        )

    monkeypatch.setattr(command_execution.subprocess, "run", time_out)

    result = execute_command(["slow"], timeout=3)

    assert result.failure == "timeout"
    assert result.returncode is None
    assert result.stdout == "partial stdout"
    assert result.stderr == "partial stderr"
    assert "timed out after 3 seconds" in result.error
    assert not result.ok


def test_execute_command_returns_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError(2, "No such file or directory", "missing-tool")

    monkeypatch.setattr(command_execution.subprocess, "run", missing)

    result = execute_command(["missing-tool"])

    assert result.failure == "os_error"
    assert result.returncode is None
    assert "missing-tool" in result.error
    assert result.duration_seconds >= 0


def test_command_environment_removes_credentials_but_keeps_proxy_and_normal_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("C2A_API_KEY", "provider-key")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-key")
    monkeypatch.setenv("SOME_TOKEN", "provider-token")
    monkeypatch.setenv("AUTHORIZATION", "Bearer provider-key")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("NORMAL_SETTING", "preserved")

    env = command_environment()

    assert "C2A_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "SOME_TOKEN" not in env
    assert "AUTHORIZATION" not in env
    assert env["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert env["NORMAL_SETTING"] == "preserved"


def test_execute_command_rejects_empty_command() -> None:
    with pytest.raises(ValueError, match="executable"):
        execute_command([])
