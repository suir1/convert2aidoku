from __future__ import annotations

import ipaddress
import platform
import shutil
import threading
import webbrowser
from pathlib import Path

import uvicorn

from .command_execution import execute_command
from .errors import ConfigurationError
from .web_app import create_web_app


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_wsl() -> bool:
    return "microsoft" in platform.release().casefold()


def open_web_ui(url: str) -> bool:
    powershell = shutil.which("powershell.exe") if _is_wsl() else None
    if powershell is not None:
        result = execute_command(
            [powershell, "-NoProfile", "-Command", "Start-Process", url],
            timeout=15,
        )
        return result.ok
    return webbrowser.open(url)


def start_web_ui(
    *,
    host: str = "127.0.0.1",
    port: int = 51_821,
    open_browser: bool = True,
    allow_network: bool = False,
    working_directory: Path | None = None,
) -> None:
    if not 1 <= port <= 65_535:
        raise ConfigurationError("Web UI port must be between 1 and 65535")
    if not _is_loopback_host(host) and not allow_network:
        raise ConfigurationError("refusing a non-loopback Web UI host without --allow-network")
    app = create_web_app(
        working_directory=working_directory,
        allow_network=allow_network,
    )
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{display_host}:{port}"
    if open_browser:
        timer = threading.Timer(0.8, open_web_ui, args=(url,))
        timer.daemon = True
        timer.start()
    uvicorn.run(app, host=host, port=port, log_level="info")
