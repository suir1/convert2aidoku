from __future__ import annotations

from pathlib import Path

import pytest

from convert2aidoku import web_runtime
from convert2aidoku.command_execution import CommandResult
from convert2aidoku.errors import ConfigurationError


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_are_accepted(host: str) -> None:
    assert web_runtime._is_loopback_host(host)


def test_network_binding_requires_explicit_permission() -> None:
    with pytest.raises(ConfigurationError, match="--allow-network"):
        web_runtime.start_web_ui(host="0.0.0.0", open_browser=False)


def test_runtime_starts_uvicorn_without_opening_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    captured: dict[str, object] = {}
    monkeypatch.setattr(web_runtime, "create_web_app", lambda **_kwargs: sentinel)
    monkeypatch.setattr(
        web_runtime.uvicorn,
        "run",
        lambda app, **kwargs: captured.update({"app": app, **kwargs}),
    )

    web_runtime.start_web_ui(
        port=51_822,
        open_browser=False,
        working_directory=tmp_path,
    )

    assert captured == {
        "app": sentinel,
        "host": "127.0.0.1",
        "port": 51_822,
        "log_level": "info",
    }


def test_wsl_opens_the_windows_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []
    monkeypatch.setattr(web_runtime, "_is_wsl", lambda: True)
    monkeypatch.setattr(web_runtime.shutil, "which", lambda _name: "/mnt/c/powershell.exe")

    def fake_execute(command: list[str], **_kwargs) -> CommandResult:
        captured.extend(command)
        return CommandResult(
            command=tuple(command),
            returncode=0,
            stdout="",
            stderr="",
            duration_seconds=0.1,
        )

    monkeypatch.setattr(web_runtime, "execute_command", fake_execute)

    assert web_runtime.open_web_ui("http://127.0.0.1:51821")
    assert captured == [
        "/mnt/c/powershell.exe",
        "-NoProfile",
        "-Command",
        "Start-Process",
        "http://127.0.0.1:51821",
    ]
