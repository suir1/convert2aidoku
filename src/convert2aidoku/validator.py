from __future__ import annotations

import json
import os
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .command_execution import command_environment, execute_command
from .constants import BLOCKED_OUTPUT_MARKERS
from .errors import InputError, SecurityError
from .models import StageKind, ValidationResult, ValidationStage
from .scaffold import read_generated_files, validate_generated_content
from .toolchain import find_tool

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
type _StageRunner = Callable[
    [str, StageKind, list[str], Path, int, dict[str, str] | None], ValidationStage
]


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
    env = command_environment()
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
    name: str,
    kind: StageKind,
    command: list[str],
    cwd: Path,
    timeout: int,
    environment: dict[str, str] | None = None,
) -> ValidationStage:
    result = execute_command(command, cwd=cwd, timeout=timeout, environment=environment)
    output = result.error if result.failure is not None else result.stdout + "\n" + result.stderr
    output = _trim_output(output.strip())
    return ValidationStage(
        name=name,
        kind=kind,
        ok=result.ok,
        command=command,
        output=output,
        duration_seconds=result.duration_seconds,
        blocked=not result.ok and name == "core-live-smoke" and _is_blocked(output),
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
    if "requesterror" in runner_output.lower():
        return (
            "Aidoku runner returned a network RequestError while the independent HTTPX probe"
            f"{route} reached {url} with HTTP {response.status_code}; this points to a runner "
            "proxy/TLS compatibility problem rather than a source parser failure"
        )
    return None


@dataclass
class _ValidationPlan:
    project: Path
    run_stage: _StageRunner
    result: ValidationResult = field(default_factory=ValidationResult)

    def command(
        self,
        name: str,
        kind: StageKind,
        command: list[str],
        timeout: int,
        *,
        environment: dict[str, str] | None = None,
        record: bool = True,
    ) -> ValidationStage:
        stage = self.run_stage(name, kind, command, self.project, timeout, environment)
        return self.record(stage) if record else stage

    def record(self, stage: ValidationStage) -> ValidationStage:
        self.result.stages.append(stage)
        return stage

    def clippy(self, cargo: str) -> bool:
        command = [
            cargo,
            "clippy",
            "--locked",
            "--target",
            "wasm32-unknown-unknown",
            "--",
            "-D",
            "warnings",
        ]
        initial = self.command("clippy", StageKind.CLIPPY, command, 600, record=False)
        if initial.ok:
            return self.record(initial).ok
        fix = self.command(
            "clippy-fix",
            StageKind.CLIPPY,
            [
                cargo,
                "clippy",
                "--fix",
                "--allow-dirty",
                "--allow-no-vcs",
                *command[2:],
            ],
            600,
            record=False,
        )
        if not fix.ok:
            self.record(initial)
            self.record(fix)
            return False
        self.record(fix)
        if not self.command(
            "format-after-clippy-fix", StageKind.FORMAT, [cargo, "fmt", "--all"], 120
        ).ok:
            return False
        if not self.record(_generated_safety_stage(self.project)).ok:
            return False
        return self.record(self.command("clippy", StageKind.CLIPPY, command, 600, record=False)).ok

    def missing(self, name: str) -> ValidationResult:
        self.record(
            ValidationStage(
                name="toolchain",
                kind=StageKind.TOOLCHAIN,
                ok=False,
                output=f"required tool is not installed: {name}",
            )
        )
        return self.result


def validate_project(
    project: Path,
    *,
    live: bool = True,
    proxy: str | None = None,
) -> ValidationResult:
    proxy = _resolve_proxy(proxy)
    plan = _ValidationPlan(project, _run_stage)
    result = plan.result
    cargo = find_tool("cargo")
    if cargo is None:
        return plan.missing("cargo")

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
        commands.insert(0, ("cargo-lock", StageKind.CHECK, [cargo, "generate-lockfile"], 600))

    for name, kind, command, timeout in commands:
        if not plan.command(name, kind, command, timeout).ok:
            return result

    if not plan.clippy(cargo):
        return result
    result.build_ok = True

    aidoku = find_tool("aidoku")
    if aidoku is None:
        return plan.missing("aidoku")
    if not plan.command("aidoku-package", StageKind.PACKAGE, [aidoku, "package", "."], 900).ok:
        return result
    result.package_ok = (project / "package.aix").is_file()

    if not plan.command(
        "aidoku-verify", StageKind.VERIFY, [aidoku, "verify", "package.aix"], 180
    ).ok:
        result.package_ok = False
        return result

    if live:
        runner = find_tool("aidoku-test-runner")
        if runner is None:
            return plan.missing("aidoku-test-runner")
        test_build = plan.command(
            "aidoku-test-build",
            StageKind.LIVE_TEST,
            [
                cargo,
                "test",
                "--locked",
                "--target",
                "wasm32-unknown-unknown",
                "--no-run",
            ],
            600,
        )
        result.blocked = test_build.blocked
        if not test_build.ok:
            return result
        test_wasm = _test_wasm(project)
        if test_wasm is None:
            plan.record(
                ValidationStage(
                    name="core-live-smoke",
                    kind=StageKind.LIVE_TEST,
                    ok=False,
                    output="cargo built no wasm test artifact",
                )
            )
            return result
        live_stage = plan.command(
            "core-live-smoke",
            StageKind.LIVE_TEST,
            [runner, str(test_wasm), "--nocapture"],
            900,
            environment=_network_environment(proxy),
        )
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
