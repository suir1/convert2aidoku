from pathlib import Path

import httpx
import pytest

from convert2aidoku.command_execution import CommandResult, command_environment
from convert2aidoku.errors import InputError
from convert2aidoku.models import StageKind, ValidationBlocker, ValidationStage
from convert2aidoku.validation_policy import RunnerFailureKind, assess_runner_failure
from convert2aidoku.validator import (
    _blocked_site_probe,
    _generated_safety_stage,
    _network_environment,
    _resolve_proxy,
    _run_stage,
    validate_project,
)
from tests.scenarios import source_metadata_project


def test_blocked_site_probe_confirms_http_403(tmp_path: Path, monkeypatch) -> None:
    source_metadata_project(tmp_path)
    monkeypatch.setattr(
        "convert2aidoku.validator.httpx.get",
        lambda *_args, **_kwargs: httpx.Response(403, text="challenge"),
    )

    evidence = _blocked_site_probe(
        tmp_path,
        failure=assess_runner_failure("first image returned HTTP 403"),
    )

    assert evidence is not None
    assert evidence.blocker is ValidationBlocker.SITE_HTTP_BLOCK
    assert evidence.diagnostic == (
        "runner-network probe returned HTTP 403 for https://example.com; this validation process "
        "does not share a browser session, so the result does not prove the site is unavailable "
        "in a normal browser"
    )


def test_site_probe_does_not_block_normal_cloudflare_cdn_reference(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_metadata_project(tmp_path)
    monkeypatch.setattr(
        "convert2aidoku.validator.httpx.get",
        lambda *_args, **_kwargs: httpx.Response(
            200,
            text='<link href="https://cdnjs.cloudflare.com/library.css"><main>Comics</main>',
        ),
    )

    assert (
        _blocked_site_probe(
            tmp_path,
            failure=assess_runner_failure("Cloudflare response"),
        )
        is None
    )


def test_site_probe_detects_explicit_challenge_page(tmp_path: Path, monkeypatch) -> None:
    source_metadata_project(tmp_path)
    monkeypatch.setattr(
        "convert2aidoku.validator.httpx.get",
        lambda *_args, **_kwargs: httpx.Response(
            200,
            text='<script src="/cdn-cgi/challenge-platform/orchestrate.js"></script>',
        ),
    )

    evidence = _blocked_site_probe(
        tmp_path,
        failure=assess_runner_failure("Cloudflare response"),
    )

    assert evidence is not None
    assert evidence.blocker is ValidationBlocker.SITE_BROWSER_CHALLENGE
    assert "browser-challenge content" in evidence.diagnostic


def test_site_probe_distinguishes_runner_fingerprint_from_browser_like_httpx(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_metadata_project(tmp_path)
    responses = iter(
        [
            httpx.Response(403, text="challenge"),
            httpx.Response(200, text="<main>Comics</main>"),
        ]
    )
    monkeypatch.setattr(
        "convert2aidoku.validator.httpx.get",
        lambda *_args, **_kwargs: next(responses),
    )

    evidence = _blocked_site_probe(
        tmp_path,
        failure=assess_runner_failure("first image returned HTTP 403"),
    )

    assert evidence is not None
    assert evidence.blocker is ValidationBlocker.RUNNER_FINGERPRINT
    assert "browser-like HTTPX preflight returned HTTP 200" in evidence.diagnostic
    assert "TLS/HTTP fingerprint" in evidence.diagnostic


@pytest.mark.parametrize(
    "runner_output",
    [
        "request failed: RequestError(RequestError)",
        "cover image request failed after retry: RequestError",
    ],
)
def test_site_probe_classifies_runner_request_error_when_httpx_succeeds(
    tmp_path: Path,
    monkeypatch,
    runner_output: str,
) -> None:
    source_metadata_project(tmp_path)
    monkeypatch.setattr(
        "convert2aidoku.validator.httpx.get",
        lambda *_args, **_kwargs: httpx.Response(200, text="<main>Comics</main>"),
    )

    evidence = _blocked_site_probe(
        tmp_path,
        failure=assess_runner_failure(runner_output),
    )

    assert evidence is not None
    assert evidence.blocker is ValidationBlocker.RUNNER_TRANSPORT
    assert "network RequestError" in evidence.diagnostic
    assert "proxy/TLS compatibility problem" in evidence.diagnostic


def test_site_probe_classifies_generated_json_api_challenge(tmp_path: Path, monkeypatch) -> None:
    source_metadata_project(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    (source / "source.rs").write_text(
        'const API_URL: &str = "https://api.example.com";\n'
        'fn search() { format!("{}/api/v1/search/items?title={}", API_URL, "x"); }\n',
        encoding="utf-8",
    )
    responses = iter(
        [
            httpx.Response(200, text="<main>Site</main>"),
            httpx.Response(
                567,
                text="<html>edge challenge</html>",
                headers={"content-type": "text/html"},
            ),
        ]
    )
    monkeypatch.setattr(
        "convert2aidoku.validator.httpx.get",
        lambda *_args, **_kwargs: next(responses),
    )

    evidence = _blocked_site_probe(
        tmp_path,
        failure=assess_runner_failure(
            'JsonParseError(Error("expected value", line: 1, column: 1))'
        ),
    )

    assert evidence is not None
    assert evidence.blocker is ValidationBlocker.API_HTTP_BLOCK
    assert "generated JSON API probe returned HTTP 567" in evidence.diagnostic
    assert "api.example.com/api/v1/search/items" in evidence.diagnostic


def test_generated_json_api_browser_challenge_has_explicit_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_metadata_project(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    (source / "source.rs").write_text(
        'const API_URL: &str = "https://api.example.com";\n'
        'fn search() { let _ = "/api/v1/search"; }\n',
        encoding="utf-8",
    )
    responses = iter(
        [
            httpx.Response(200, text="<main>Site</main>"),
            httpx.Response(
                200,
                text='<script src="/cdn-cgi/challenge-platform/run.js"></script>',
                headers={"content-type": "text/html"},
            ),
        ]
    )
    monkeypatch.setattr(
        "convert2aidoku.validator.httpx.get",
        lambda *_args, **_kwargs: next(responses),
    )

    evidence = _blocked_site_probe(
        tmp_path,
        failure=assess_runner_failure(
            'JsonParseError(Error("expected value", line: 1, column: 1))'
        ),
    )

    assert evidence is not None
    assert evidence.blocker is ValidationBlocker.API_BROWSER_CHALLENGE


def test_generated_json_api_404_remains_repairable(tmp_path: Path, monkeypatch) -> None:
    source_metadata_project(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    (source / "source.rs").write_text(
        'const API_URL: &str = "https://api.example.com";\n'
        'fn search() { let _ = "/api/v1/stale"; }\n',
        encoding="utf-8",
    )
    responses = iter(
        [
            httpx.Response(200, text="<main>Site</main>"),
            httpx.Response(404, text="<html>Not found</html>"),
        ]
    )
    monkeypatch.setattr(
        "convert2aidoku.validator.httpx.get",
        lambda *_args, **_kwargs: next(responses),
    )

    evidence = _blocked_site_probe(
        tmp_path,
        failure=assess_runner_failure(
            'JsonParseError(Error("expected value", line: 1, column: 1))'
        ),
    )

    assert evidence is None


def test_site_probe_network_error_has_explicit_evidence(tmp_path: Path, monkeypatch) -> None:
    source_metadata_project(tmp_path)

    def fail_get(*_args: object, **_kwargs: object) -> httpx.Response:
        request = httpx.Request("GET", "https://example.com")
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr("convert2aidoku.validator.httpx.get", fail_get)

    evidence = _blocked_site_probe(
        tmp_path,
        failure=assess_runner_failure("connection refused"),
    )

    assert evidence is not None
    assert evidence.blocker is ValidationBlocker.SITE_NETWORK_ERROR


@pytest.mark.parametrize(
    ("output", "kind"),
    [
        ("chapter date is missing", RunnerFailureKind.UNKNOWN),
        ("request failed: RequestError(RequestError)", RunnerFailureKind.TRANSPORT),
        ("first image returned HTTP 403", RunnerFailureKind.REMOTE_RESPONSE),
        ("popular listing returned no manga", RunnerFailureKind.EMPTY_LISTING),
        (
            'JsonParseError(Error("expected value", line: 1, column: 1))',
            RunnerFailureKind.JSON_RESPONSE,
        ),
        ('errorResponse: {"message":"初始化失败"}', RunnerFailureKind.ANONYMOUS_INITIALIZATION),
        ("connection reset", RunnerFailureKind.NETWORK_CANDIDATE),
    ],
)
def test_runner_failure_policy_classifies_probe_candidates(
    output: str,
    kind: RunnerFailureKind,
) -> None:
    assert assess_runner_failure(output).kind is kind


def test_anonymous_initialization_is_the_only_direct_text_blocker() -> None:
    direct = assess_runner_failure('errorResponse: {"message":"初始化失败"}')
    http_status = assess_runner_failure("first image returned HTTP 403")

    assert direct.direct_blocker is ValidationBlocker.ANONYMOUS_INITIALIZATION
    assert not direct.requires_probe
    assert http_status.direct_blocker is None
    assert http_status.requires_probe


def _validate_live_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: str,
):
    source_metadata_project(tmp_path)
    (tmp_path / "Cargo.lock").write_text("locked", encoding="utf-8")
    test_wasm = tmp_path / "target" / "test.wasm"

    def fake_run_stage(name: str, kind: StageKind, *_args: object) -> ValidationStage:
        if name == "aidoku-package":
            (tmp_path / "package.aix").write_bytes(b"package")
        if name == "core-live-smoke":
            return ValidationStage(name=name, kind=kind, ok=False, output=output)
        return ValidationStage(name=name, kind=kind, ok=True)

    monkeypatch.setattr("convert2aidoku.validator.find_tool", lambda name: f"/{name}")
    monkeypatch.setattr("convert2aidoku.validator._run_stage", fake_run_stage)
    monkeypatch.setattr("convert2aidoku.validator._test_wasm", lambda _project: test_wasm)
    return validate_project(tmp_path, live=True)


def test_validation_plan_propagates_anonymous_initialization_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _validate_live_failure(
        tmp_path,
        monkeypatch,
        'errorResponse: {"message":"初始化失败"}',
    )

    assert result.blocked
    assert result.blocker_reason is ValidationBlocker.ANONYMOUS_INITIALIZATION
    assert result.stages[-1].blocker_reason is ValidationBlocker.ANONYMOUS_INITIALIZATION


def test_validation_plan_keeps_endpoint_403_repairable_when_site_is_reachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "convert2aidoku.validator.httpx.get",
        lambda *_args, **_kwargs: httpx.Response(200, text="<main>Comics</main>"),
    )

    result = _validate_live_failure(
        tmp_path,
        monkeypatch,
        "first image returned HTTP 403",
    )

    assert not result.blocked
    assert result.blocker_reason is None
    assert not result.live_ok


def test_generated_endpoint_403_remains_repairable_when_site_probe_succeeds(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_metadata_project(tmp_path)
    monkeypatch.setattr(
        "convert2aidoku.validator.httpx.get",
        lambda *_args, **_kwargs: httpx.Response(200, text="<main>Comics</main>"),
    )

    evidence = _blocked_site_probe(
        tmp_path,
        failure=assess_runner_failure("first image returned HTTP 403"),
    )

    assert evidence is None


def test_proxy_is_passed_to_probe_without_being_reported(tmp_path: Path, monkeypatch) -> None:
    source_metadata_project(tmp_path)
    captured: dict[str, object] = {}

    def fake_get(*_args: object, **kwargs: object) -> httpx.Response:
        captured.update(kwargs)
        return httpx.Response(403, text="blocked")

    monkeypatch.setattr("convert2aidoku.validator.httpx.get", fake_get)
    proxy = "http://user:password@127.0.0.1:7890"

    evidence = _blocked_site_probe(
        tmp_path,
        proxy=proxy,
        failure=assess_runner_failure("first image returned HTTP 403"),
    )

    assert evidence is not None
    diagnostic = evidence.diagnostic
    assert captured["proxy"] == proxy
    assert "via configured proxy" in diagnostic
    assert proxy not in diagnostic
    assert "password" not in diagnostic


def test_proxy_environment_isolated_from_provider_credentials(monkeypatch) -> None:
    monkeypatch.setenv("C2A_API_KEY", "secret")
    proxy = "http://127.0.0.1:7890"

    env = _network_environment(proxy)

    assert env["HTTP_PROXY"] == proxy
    assert env["HTTPS_PROXY"] == proxy
    assert env["ALL_PROXY"] == proxy
    assert "C2A_API_KEY" not in env


def test_proxy_can_come_from_environment_and_rejects_unsupported_scheme(monkeypatch) -> None:
    monkeypatch.setenv("C2A_PROXY", "http://127.0.0.1:7890")
    assert _resolve_proxy(None) == "http://127.0.0.1:7890"

    with pytest.raises(InputError, match=r"http\(s\) URL"):
        _resolve_proxy("socks5://127.0.0.1:7891")


def test_command_environment_drops_credentials(monkeypatch) -> None:
    monkeypatch.setenv("C2A_API_KEY", "secret")
    monkeypatch.setenv("OTHER_TOKEN", "secret")
    env = command_environment()
    assert "C2A_API_KEY" not in env
    assert "OTHER_TOKEN" not in env


def test_validation_stage_consumes_command_execution_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "convert2aidoku.validator.execute_command",
        lambda *_args, **_kwargs: CommandResult(
            command=("cargo", "check"),
            returncode=1,
            stdout="compiler output",
            stderr="compiler error",
            duration_seconds=1.25,
        ),
    )

    stage = _run_stage(
        "cargo-check",
        StageKind.CHECK,
        ["cargo", "check"],
        tmp_path,
        600,
    )

    assert not stage.ok
    assert stage.command == ["cargo", "check"]
    assert stage.output == "compiler output\ncompiler error"
    assert stage.duration_seconds == 1.25


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("cargo-check", ["format", "cargo-check"]),
        ("clippy-fix", ["format", "cargo-check", "clippy", "clippy-fix"]),
    ],
)
def test_validation_plan_stops_and_records_at_first_unrepaired_failure(
    tmp_path: Path,
    monkeypatch,
    failure: str,
    expected: list[str],
) -> None:
    calls: list[str] = []

    def fake_run_stage(name: str, kind: StageKind, *_args: object) -> ValidationStage:
        calls.append(name)
        fails = {failure} | ({"clippy"} if failure == "clippy-fix" else set())
        return ValidationStage(name=name, kind=kind, ok=name not in fails)

    monkeypatch.setattr(
        "convert2aidoku.validator.find_tool",
        lambda name: "/cargo" if name == "cargo" else None,
    )
    monkeypatch.setattr("convert2aidoku.validator._run_stage", fake_run_stage)
    (tmp_path / "Cargo.lock").write_text("locked", encoding="utf-8")

    result = validate_project(tmp_path, live=False)

    assert calls == expected
    assert [stage.name for stage in result.stages] == expected
    assert not result.build_ok


def test_validation_applies_clippy_fixes_before_requesting_ai(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []
    clippy_runs = 0

    def fake_find_tool(name: str) -> str | None:
        return "/cargo" if name == "cargo" else None

    def fake_run_stage(name: str, kind: StageKind, *_args: object) -> ValidationStage:
        nonlocal clippy_runs
        calls.append(name)
        ok = True
        if name == "clippy":
            clippy_runs += 1
            ok = clippy_runs > 1
        return ValidationStage(name=name, kind=kind, ok=ok)

    monkeypatch.setattr("convert2aidoku.validator.find_tool", fake_find_tool)
    monkeypatch.setattr("convert2aidoku.validator._run_stage", fake_run_stage)
    (tmp_path / "Cargo.lock").write_text("locked", encoding="utf-8")

    result = validate_project(tmp_path, live=False)

    assert result.build_ok
    assert calls == [
        "format",
        "cargo-check",
        "clippy",
        "clippy-fix",
        "format-after-clippy-fix",
        "clippy",
    ]
    assert [stage.name for stage in result.stages[:5]] == [
        "format",
        "cargo-check",
        "clippy-fix",
        "format-after-clippy-fix",
        "generated-safety-after-clippy-fix",
    ]
    assert result.stages[5].name == "clippy"


def test_validation_retries_clippy_fix_when_first_fix_exposes_another_lint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []
    clippy_runs = 0

    def fake_find_tool(name: str) -> str | None:
        return "/cargo" if name == "cargo" else None

    def fake_run_stage(name: str, kind: StageKind, *_args: object) -> ValidationStage:
        nonlocal clippy_runs
        calls.append(name)
        ok = True
        if name == "clippy":
            clippy_runs += 1
            ok = clippy_runs > 2
        return ValidationStage(name=name, kind=kind, ok=ok)

    monkeypatch.setattr("convert2aidoku.validator.find_tool", fake_find_tool)
    monkeypatch.setattr("convert2aidoku.validator._run_stage", fake_run_stage)
    (tmp_path / "Cargo.lock").write_text("locked", encoding="utf-8")

    result = validate_project(tmp_path, live=False)

    assert result.build_ok
    assert calls == [
        "format",
        "cargo-check",
        "clippy",
        "clippy-fix",
        "format-after-clippy-fix",
        "clippy",
        "clippy-fix-2",
        "format-after-clippy-fix-2",
        "clippy",
    ]


def test_generated_safety_ignores_tool_owned_smoke_module(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "lib.rs").write_text(
        "#![no_std]\n\n#[cfg(test)]\nmod generated_smoke;\n",
        encoding="utf-8",
    )

    stage = _generated_safety_stage(tmp_path)

    assert stage.ok, stage.output
