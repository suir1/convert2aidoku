from pathlib import Path

import httpx
import pytest

from convert2aidoku.errors import InputError
from convert2aidoku.models import StageKind, ValidationStage
from convert2aidoku.toolchain import tool_environment
from convert2aidoku.validator import (
    _blocked_site_probe,
    _generated_safety_stage,
    _is_runner_network_failure,
    _network_environment,
    _resolve_proxy,
    validate_project,
)
from tests.scenarios import source_metadata_project


def test_blocked_site_probe_confirms_http_403(tmp_path: Path, monkeypatch) -> None:
    source_metadata_project(tmp_path)
    monkeypatch.setattr(
        "convert2aidoku.validator.httpx.get",
        lambda *_args, **_kwargs: httpx.Response(403, text="challenge"),
    )

    assert _blocked_site_probe(tmp_path) == (
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

    assert _blocked_site_probe(tmp_path) is None


def test_site_probe_detects_explicit_challenge_page(tmp_path: Path, monkeypatch) -> None:
    source_metadata_project(tmp_path)
    monkeypatch.setattr(
        "convert2aidoku.validator.httpx.get",
        lambda *_args, **_kwargs: httpx.Response(
            200,
            text='<script src="/cdn-cgi/challenge-platform/orchestrate.js"></script>',
        ),
    )

    assert "browser-challenge content" in (_blocked_site_probe(tmp_path) or "")


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

    diagnostic = _blocked_site_probe(tmp_path) or ""

    assert "browser-like HTTPX preflight returned HTTP 200" in diagnostic
    assert "TLS/HTTP fingerprint" in diagnostic


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

    diagnostic = (
        _blocked_site_probe(
            tmp_path,
            runner_output=runner_output,
        )
        or ""
    )

    assert "network RequestError" in diagnostic
    assert "proxy/TLS compatibility problem" in diagnostic


def test_runner_assertion_failure_is_not_misclassified_as_network_failure() -> None:
    assert not _is_runner_network_failure(
        "panicked at src/generated_smoke.rs:94: chapter date is missing"
    )
    assert _is_runner_network_failure("request failed: RequestError(RequestError)")
    assert _is_runner_network_failure("first image returned HTTP 403")
    assert _is_runner_network_failure("popular listing returned no manga")


def test_proxy_is_passed_to_probe_without_being_reported(tmp_path: Path, monkeypatch) -> None:
    source_metadata_project(tmp_path)
    captured: dict[str, object] = {}

    def fake_get(*_args: object, **kwargs: object) -> httpx.Response:
        captured.update(kwargs)
        return httpx.Response(403, text="blocked")

    monkeypatch.setattr("convert2aidoku.validator.httpx.get", fake_get)
    proxy = "http://user:password@127.0.0.1:7890"

    diagnostic = _blocked_site_probe(tmp_path, proxy=proxy) or ""

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


def test_tool_environment_drops_credentials(monkeypatch) -> None:
    monkeypatch.setenv("C2A_API_KEY", "secret")
    monkeypatch.setenv("OTHER_TOKEN", "secret")
    env = tool_environment()
    assert "C2A_API_KEY" not in env
    assert "OTHER_TOKEN" not in env


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


def test_generated_safety_ignores_tool_owned_smoke_module(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "lib.rs").write_text(
        "#![no_std]\n\n#[cfg(test)]\nmod generated_smoke;\n",
        encoding="utf-8",
    )

    stage = _generated_safety_stage(tmp_path)

    assert stage.ok, stage.output
