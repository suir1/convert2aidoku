from __future__ import annotations

import json
import os
import subprocess
import time
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .constants import BLOCKED_OUTPUT_MARKERS
from .errors import InputError, SecurityError
from .models import StageKind, ValidationResult, ValidationStage
from .scaffold import read_generated_files, validate_generated_content
from .toolchain import find_tool, tool_environment

_BLOCKED_HTTP_STATUSES = {403, 429, 503, 521, 522, 523, 524}
_CHALLENGE_BODY_MARKERS = (
    "/cdn-cgi/challenge-platform/",
    "cf-chl-",
    "cf-turnstile",
    "checking your browser",
    "just a moment...",
    "attention required! | cloudflare",
    "verify you are human",
    "g-recaptcha",
    "hcaptcha",
)
_BROWSER_PROBE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
    "Sec-CH-UA": '"Chromium";v="138", "Not=A?Brand";v="24"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"macOS"',
}
_RUNNER_NETWORK_FAILURE_MARKERS = (
    "requesterror",
    "networkerror",
    "request failed",
    "connection error",
    "connection refused",
    "connection reset",
    "dns error",
    "search/list returned no manga",
    "popular listing returned no manga",
    "latest listing returned no manga",
    "timed out",
    "timeout",
)


def _trim_output(value: str, limit: int = 40_000) -> str:
    if len(value) <= limit:
        return value
    return value[: limit // 2] + "\n... output truncated ...\n" + value[-limit // 2 :]


def _is_blocked(output: str) -> bool:
    lowered = output.lower()
    return any(marker in lowered for marker in BLOCKED_OUTPUT_MARKERS)


def _is_browser_challenge(body: str) -> bool:
    lowered = body.lower()
    return any(marker in lowered for marker in _CHALLENGE_BODY_MARKERS)


def _is_runner_network_failure(output: str) -> bool:
    lowered = output.lower()
    return _is_blocked(output) or any(
        marker in lowered for marker in _RUNNER_NETWORK_FAILURE_MARKERS
    )


def _resolve_proxy(proxy: str | None) -> str | None:
    value = proxy or os.getenv("C2A_PROXY")
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise InputError(
            "validation proxy must be an http(s) URL, for example http://127.0.0.1:7890"
        )
    return value


def _network_environment(proxy: str | None) -> dict[str, str]:
    env = tool_environment()
    if proxy:
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            env[name] = proxy
    return env


def _run_stage(
    *,
    name: str,
    kind: StageKind,
    command: list[str],
    cwd: Path,
    timeout: int,
    environment: dict[str, str] | None = None,
) -> ValidationStage:
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment if environment is not None else tool_environment(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = _trim_output((result.stdout + "\n" + result.stderr).strip())
        ok = result.returncode == 0
        return ValidationStage(
            name=name,
            kind=kind,
            ok=ok,
            command=command,
            output=output,
            duration_seconds=time.monotonic() - started,
            blocked=not ok and name == "core-live-smoke" and _is_blocked(output),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        output = str(exc)
        return ValidationStage(
            name=name,
            kind=kind,
            ok=False,
            command=command,
            output=output,
            duration_seconds=time.monotonic() - started,
            blocked=name == "core-live-smoke" and _is_blocked(output),
        )


def _missing_tool_stage(name: str) -> ValidationStage:
    return ValidationStage(
        name="toolchain",
        kind=StageKind.TOOLCHAIN,
        ok=False,
        output=f"required tool is not installed: {name}",
    )


def _generated_safety_stage(project: Path) -> ValidationStage:
    started = time.monotonic()
    try:
        for generated in read_generated_files(project):
            path = generated["path"]
            if not path.endswith(".rs"):
                continue
            validate_generated_content(path, generated["content"])
    except (OSError, SecurityError) as exc:
        return ValidationStage(
            name="generated-safety-after-clippy-fix",
            kind=StageKind.CHECK,
            ok=False,
            output=str(exc),
            duration_seconds=time.monotonic() - started,
        )
    return ValidationStage(
        name="generated-safety-after-clippy-fix",
        kind=StageKind.CHECK,
        ok=True,
        duration_seconds=time.monotonic() - started,
    )


def _test_wasm(project: Path) -> Path | None:
    """Return the wasm unit-test artifact built by `cargo test --no-run`."""
    try:
        package = tomllib.loads((project / "Cargo.toml").read_text(encoding="utf-8"))["package"][
            "name"
        ]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return None
    crate_name = str(package).replace("-", "_")
    artifacts = list(
        (project / "target" / "wasm32-unknown-unknown" / "debug" / "deps").glob(
            f"{crate_name}-*.wasm"
        )
    )
    if not artifacts:
        return None
    return max(artifacts, key=lambda path: path.stat().st_mtime)


def _blocked_site_probe(
    project: Path,
    *,
    proxy: str | None = None,
    runner_output: str = "",
) -> str | None:
    """Check whether the non-browser validator network is blocked by the site."""
    try:
        source = json.loads((project / "res" / "source.json").read_text(encoding="utf-8"))
        url = source["info"]["url"]
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return None
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None
    try:
        response = httpx.get(
            url,
            headers={
                "User-Agent": _BROWSER_PROBE_HEADERS["User-Agent"],
                "Accept": _BROWSER_PROBE_HEADERS["Accept"],
            },
            follow_redirects=True,
            timeout=20,
            trust_env=False,
            proxy=proxy,
        )
    except httpx.HTTPError as exc:
        detail = str(exc).lower()
        if any(
            marker in detail for marker in ("connection refused", "dns", "timed out", "timeout")
        ):
            return (
                "runner-network probe failed: "
                f"{type(exc).__name__}: {exc}; this does not prove the site is unavailable "
                "in a normal browser"
            )
        return None
    body = response.text[:20_000]
    route = " via configured proxy" if proxy else ""
    if response.status_code in _BLOCKED_HTTP_STATUSES:
        try:
            browser_response = httpx.get(
                url,
                headers=_BROWSER_PROBE_HEADERS,
                follow_redirects=True,
                timeout=20,
                trust_env=False,
                proxy=proxy,
            )
        except httpx.HTTPError:
            browser_response = None
        if (
            browser_response is not None
            and 200 <= browser_response.status_code < 400
            and not _is_browser_challenge(browser_response.text[:20_000])
        ):
            return (
                f"runner-network probe{route} returned HTTP {response.status_code} for {url}, "
                f"but browser-like HTTPX preflight returned HTTP {browser_response.status_code}; "
                "this points to the Aidoku runner's TLS/HTTP fingerprint being challenged, not "
                "the source parser or site-wide unavailability"
            )
        return (
            f"runner-network probe{route} returned HTTP {response.status_code} for {url}; "
            "this validation process does not share a browser session, so the result does not "
            "prove the site is unavailable in a normal browser"
        )
    if _is_browser_challenge(body):
        return (
            f"runner-network probe{route} received browser-challenge content for {url} "
            f"with HTTP {response.status_code}; this validation process does not share a browser "
            "session, so the result does not prove the site is unavailable in a normal browser"
        )
    if "RequestError(RequestError)" in runner_output:
        return (
            "Aidoku runner returned a network RequestError while the independent HTTPX probe"
            f"{route} reached {url} with HTTP {response.status_code}; this points to a runner "
            "proxy/TLS compatibility problem rather than a source parser failure"
        )
    return None


def validate_project(
    project: Path,
    *,
    live: bool = True,
    proxy: str | None = None,
) -> ValidationResult:
    proxy = _resolve_proxy(proxy)
    result = ValidationResult()
    cargo = find_tool("cargo")
    if cargo is None:
        result.stages.append(_missing_tool_stage("cargo"))
        return result

    commands: list[tuple[str, StageKind, list[str], int]] = [
        ("format", StageKind.FORMAT, [cargo, "fmt", "--all"], 120),
        (
            "cargo-check",
            StageKind.CHECK,
            [cargo, "check", "--locked", "--target", "wasm32-unknown-unknown"],
            600,
        ),
    ]
    if not (project / "Cargo.lock").is_file():
        lock_stage = _run_stage(
            name="cargo-lock",
            kind=StageKind.CHECK,
            command=[cargo, "generate-lockfile"],
            cwd=project,
            timeout=600,
        )
        result.stages.append(lock_stage)
        if not lock_stage.ok:
            return result

    for name, kind, command, timeout in commands:
        stage = _run_stage(name=name, kind=kind, command=command, cwd=project, timeout=timeout)
        result.stages.append(stage)
        if not stage.ok:
            return result

    clippy_command = [
        cargo,
        "clippy",
        "--locked",
        "--target",
        "wasm32-unknown-unknown",
        "--",
        "-D",
        "warnings",
    ]
    clippy_stage = _run_stage(
        name="clippy",
        kind=StageKind.CLIPPY,
        command=clippy_command,
        cwd=project,
        timeout=600,
    )
    if not clippy_stage.ok:
        fix_stage = _run_stage(
            name="clippy-fix",
            kind=StageKind.CLIPPY,
            command=[
                cargo,
                "clippy",
                "--fix",
                "--allow-dirty",
                "--allow-no-vcs",
                "--locked",
                "--target",
                "wasm32-unknown-unknown",
                "--",
                "-D",
                "warnings",
            ],
            cwd=project,
            timeout=600,
        )
        if not fix_stage.ok:
            result.stages.extend((clippy_stage, fix_stage))
            return result
        result.stages.append(fix_stage)
        format_stage = _run_stage(
            name="format-after-clippy-fix",
            kind=StageKind.FORMAT,
            command=[cargo, "fmt", "--all"],
            cwd=project,
            timeout=120,
        )
        result.stages.append(format_stage)
        if not format_stage.ok:
            return result
        safety_stage = _generated_safety_stage(project)
        result.stages.append(safety_stage)
        if not safety_stage.ok:
            return result
        clippy_stage = _run_stage(
            name="clippy",
            kind=StageKind.CLIPPY,
            command=clippy_command,
            cwd=project,
            timeout=600,
        )
    result.stages.append(clippy_stage)
    if not clippy_stage.ok:
        return result
    result.build_ok = True

    aidoku = find_tool("aidoku")
    if aidoku is None:
        result.stages.append(_missing_tool_stage("aidoku"))
        return result
    package_stage = _run_stage(
        name="aidoku-package",
        kind=StageKind.PACKAGE,
        command=[aidoku, "package", "."],
        cwd=project,
        timeout=900,
    )
    result.stages.append(package_stage)
    if not package_stage.ok:
        return result
    result.package_ok = (project / "package.aix").is_file()

    verify_stage = _run_stage(
        name="aidoku-verify",
        kind=StageKind.VERIFY,
        command=[aidoku, "verify", "package.aix"],
        cwd=project,
        timeout=180,
    )
    result.stages.append(verify_stage)
    if not verify_stage.ok:
        result.package_ok = False
        return result

    if live:
        runner = find_tool("aidoku-test-runner")
        if runner is None:
            result.stages.append(_missing_tool_stage("aidoku-test-runner"))
            return result
        test_build = _run_stage(
            name="aidoku-test-build",
            kind=StageKind.LIVE_TEST,
            command=[
                cargo,
                "test",
                "--locked",
                "--target",
                "wasm32-unknown-unknown",
                "--no-run",
            ],
            cwd=project,
            timeout=600,
        )
        result.stages.append(test_build)
        result.blocked = test_build.blocked
        if not test_build.ok:
            return result
        test_wasm = _test_wasm(project)
        if test_wasm is None:
            result.stages.append(
                ValidationStage(
                    name="core-live-smoke",
                    kind=StageKind.LIVE_TEST,
                    ok=False,
                    output="cargo built no wasm test artifact",
                )
            )
            return result
        live_stage = _run_stage(
            name="core-live-smoke",
            kind=StageKind.LIVE_TEST,
            command=[runner, str(test_wasm), "--nocapture"],
            cwd=project,
            timeout=900,
            environment=_network_environment(proxy),
        )
        result.stages.append(live_stage)
        if (
            not live_stage.ok
            and not live_stage.blocked
            and _is_runner_network_failure(live_stage.output)
        ):
            probe = _blocked_site_probe(
                project,
                proxy=proxy,
                runner_output=live_stage.output,
            )
            if probe:
                live_stage.blocked = True
                live_stage.output = _trim_output(f"{live_stage.output}\n\n{probe}".strip())
        result.blocked = live_stage.blocked
        if not live_stage.ok:
            return result
        result.live_ok = True
    return result
