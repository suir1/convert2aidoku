from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from convert2aidoku.models import ConversionStatus
from convert2aidoku.toolchain import ToolStatus
from convert2aidoku.web_app import create_web_app

FIXTURE = Path(__file__).parent / "fixtures" / "simple"


@pytest.fixture
def web_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("C2A_API_KEY", "web-secret")
    monkeypatch.setattr(
        "convert2aidoku.web_app.doctor",
        lambda: [ToolStatus("git", True, "/usr/bin/git", "git version test")],
    )
    app = create_web_app(working_directory=tmp_path)
    with TestClient(app) as client:
        yield app, client


def _csrf(app) -> dict[str, str]:
    return {"X-C2A-CSRF": app.state.c2a_csrf}


def test_local_dashboard_is_self_contained_and_hardened(web_client) -> None:
    _app, client = web_client

    response = client.get("/")

    assert response.status_code == 200
    assert "把源代码变成" in response.text
    assert "/static/app.css" in response.text
    assert "/static/app.js" in response.text
    assert "web-secret" not in response.text
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert client.get("/static/app.css").status_code == 200


def test_mutating_routes_require_local_csrf_and_origin(web_client) -> None:
    app, client = web_client
    payload = {"base_url": "http://localhost/v1", "model": "model"}

    missing = client.post("/api/ai-check", json=payload)
    cross_origin = client.post(
        "/api/ai-check",
        json=payload,
        headers={**_csrf(app), "Origin": "https://attacker.example"},
    )

    assert missing.status_code == 403
    assert cross_origin.status_code == 403


def test_ai_probe_reports_structured_output_without_returning_credentials(
    web_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = web_client

    class FakeClient:
        def __init__(self, settings) -> None:
            assert settings.api_key.get_secret_value() == "web-secret"

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def check(self):
            return SimpleNamespace(model="checked-model", structured_output=True)

    monkeypatch.setattr("convert2aidoku.web_app.OpenAICompatibleClient", FakeClient)
    response = client.post(
        "/api/ai-check",
        headers=_csrf(app),
        json={"base_url": "http://localhost/v1", "model": "model"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "model": "checked-model",
        "structured_output": True,
    }
    assert "web-secret" not in response.text


def test_conversion_requires_explicit_source_disclosure_consent(web_client) -> None:
    app, client = web_client

    response = client.post(
        "/api/jobs",
        headers=_csrf(app),
        json={
            "input_ref": str(FIXTURE),
            "output": "generated/en.simple",
            "base_url": "http://localhost/v1",
            "model": "model",
            "consent": False,
        },
    )

    assert response.status_code == 422


def test_analyze_previews_a_local_module_without_ai(web_client) -> None:
    app, client = web_client

    response = client.post(
        "/api/analyze",
        data={"input_ref": str(FIXTURE)},
        headers=_csrf(app),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"]["id"] == "en.simple"
    assert payload["source"]["format"] == "kotlin_module"
    assert payload["suggested_output"].endswith("generated/en.simple")


def test_web_upload_rejects_non_apk_files(web_client) -> None:
    app, client = web_client

    response = client.post(
        "/api/analyze",
        files={"source_file": ("source.txt", b"not an apk", "text/plain")},
        headers=_csrf(app),
    )

    assert response.status_code == 400
    assert ".apk" in response.json()["detail"]


def test_conversion_job_streams_terminal_state_and_downloads_artifacts(
    web_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = web_client

    def fake_convert(_input_ref: str, **kwargs):
        output = kwargs["output"]
        output.mkdir(parents=True)
        (output / "package.aix").write_bytes(b"aix")
        (output / "report.md").write_text("verified")
        (output / "report.json").write_text('{"status":"verified"}')
        kwargs["progress"]("Requesting initial AI generation")
        kwargs["progress"]("AI round 1 validation passed")
        return SimpleNamespace(
            output=output,
            report=SimpleNamespace(status=ConversionStatus.VERIFIED, ai_rounds=[]),
        )

    monkeypatch.setattr("convert2aidoku.web_jobs.convert_source", fake_convert)
    response = client.post(
        "/api/jobs",
        headers=_csrf(app),
        json={
            "input_ref": str(FIXTURE),
            "output": "generated/en.simple",
            "base_url": "http://localhost/v1",
            "model": "model",
            "consent": True,
        },
    )
    assert response.status_code == 202
    job_id = response.json()["id"]

    for _attempt in range(100):
        snapshot = client.get(f"/api/jobs/{job_id}").json()
        if snapshot["status"] == "verified":
            break
        time.sleep(0.01)
    else:
        pytest.fail("Web conversion job did not finish")

    assert snapshot["artifacts"]["package"].endswith("/artifacts/package")
    with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
        assert '"status":"verified"' in "".join(stream.iter_text())
    package = client.get(snapshot["artifacts"]["package"])
    assert package.status_code == 200
    assert package.content == b"aix"


def test_job_resume_reuses_the_saved_request(web_client, monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = web_client
    calls: list[bool] = []

    def fake_convert(_input_ref: str, **kwargs):
        calls.append(kwargs["resume"])
        raise RuntimeError("expected test failure")

    monkeypatch.setattr("convert2aidoku.web_jobs.convert_source", fake_convert)
    created = client.post(
        "/api/jobs",
        headers=_csrf(app),
        json={
            "input_ref": str(FIXTURE),
            "output": "generated/en.simple",
            "base_url": "http://localhost/v1",
            "model": "model",
            "consent": True,
        },
    ).json()
    for _attempt in range(100):
        if client.get(f"/api/jobs/{created['id']}").json()["status"] == "failed":
            break
        time.sleep(0.01)
    resumed = client.post(
        f"/api/jobs/{created['id']}/resume",
        headers=_csrf(app),
    )

    assert resumed.status_code == 202
    for _attempt in range(100):
        if len(calls) == 2:
            break
        time.sleep(0.01)
    assert calls == [False, True]
