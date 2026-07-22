import json

import httpx
from pydantic import SecretStr

from convert2aidoku.ai import OpenAICompatibleClient, _contract_text, _strict_json_schema
from convert2aidoku.config import AISettings
from convert2aidoku.models import SourceFile, SourceIR, SourceMetadata


def _manifest() -> dict[str, object]:
    return {
        "source_struct": "Example",
        "implemented_traits": [],
        "files": [{"path": "src/lib.rs", "content": "#![no_std]"}],
        "dependencies": [],
        "warnings": [],
        "unsupported_features": [],
    }


def test_strict_schema_closes_every_object() -> None:
    schema = _strict_json_schema()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if "properties" in value:
                assert value["additionalProperties"] is False
                assert set(value["required"]) == set(value["properties"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)


def test_contract_requires_official_metadata_mapping() -> None:
    contract = _contract_text()

    for requirement in (
        "Manga.url",
        "Chapter.url",
        "chapter_number",
        "volume_number",
        "date_uploaded",
        "scanlators",
        "Manga.viewer",
        "legacy preference values",
        "exact same ID/path/absolute-URL strategy",
        "encrypted_json",
        "AES-CBC",
        "Dynamic base URLs",
    ):
        assert requirement in contract


def test_structured_manifest_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["response_format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(_manifest())}}],
                "usage": {"total_tokens": 12},
            },
        )

    settings = AISettings(
        base_url="http://local/v1",
        model="test",
        api_key=SecretStr("secret"),
    )
    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = client._request_manifest([{"role": "user", "content": "test"}])
    assert result.structured_output
    assert result.manifest.source_struct == "Example"
    assert result.usage and result.usage.total_tokens == 12


def test_falls_back_when_json_schema_is_rejected() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if "response_format" in payload:
            return httpx.Response(400, text="unsupported response_format")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_manifest())}}]},
        )

    settings = AISettings(
        base_url="http://local/v1",
        model="test",
        api_key=SecretStr("secret"),
    )
    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = client._request_manifest([{"role": "user", "content": "test"}])
    assert calls == 2
    assert not result.structured_output
    assert result.warnings


def test_invalid_filter_shape_is_retried_with_field_diagnostic() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if calls == 2:
            assert "options must be an array of strings" in payload["messages"][-1]["content"]
            assert payload["response_format"]["type"] == "json_schema"
        manifest = _manifest()
        manifest["files"] = [
            {"path": "src/lib.rs", "content": "#![no_std]"},
            {
                "path": "res/filters.json",
                "content": (
                    '[{"type":"select","options":[1]}]'
                    if calls == 1
                    else '[{"type":"select","options":["Latest"]}]'
                ),
            },
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(manifest)}}]},
        )

    settings = AISettings(
        base_url="http://local/v1",
        model="test",
        api_key=SecretStr("secret"),
    )
    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = client._request_manifest([{"role": "user", "content": "test"}])

    assert calls == 2
    assert result.manifest.files[1].path == "res/filters.json"


def test_transient_provider_errors_are_retried_with_backoff() -> None:
    calls = 0
    delays: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, text="rate limited")
        if calls == 2:
            return httpx.Response(503, text="temporarily unavailable")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_manifest())}}]},
        )

    settings = AISettings(
        base_url="http://local/v1",
        model="test",
        api_key=SecretStr("secret"),
    )
    with OpenAICompatibleClient(
        settings,
        transport=httpx.MockTransport(handler),
        sleep=delays.append,
    ) as client:
        result = client._request_manifest([{"role": "user", "content": "test"}])

    assert result.manifest.source_struct == "Example"
    assert calls == 3
    assert delays == [5.0, 15.0]


def test_repair_uses_compact_context_without_original_source_bodies() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        repair_payload = json.loads(payload["messages"][1]["content"])
        assert "source_files" not in repair_payload
        assert "files" not in repair_payload["source_ir"]
        assert repair_payload["current_files"][0]["content"] == "current rust"
        assert repair_payload["prior_generation_manifests"][0]["round"] == 1
        assert "resource_files" not in repair_payload["prior_generation_manifests"][0]
        assert b"original-source-body-marker" not in request.content
        assert b"historical-resource-body-marker" not in request.content
        assert len(request.content) < 80_000
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_manifest())}}]},
        )

    settings = AISettings(
        base_url="http://local/v1",
        model="test",
        api_key=SecretStr("secret"),
    )
    ir = SourceIR(
        input_ref="fixture",
        metadata=SourceMetadata(
            source_id="en.example",
            package_name="example",
            name="Example",
            language="en",
            base_url="https://example.com",
        ),
        main_class="Example",
        files=[
            SourceFile(
                path="src/Example.kt",
                content="original-source-body-marker" * 10_000,
                sha256="0",
            )
        ],
    )
    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = client.repair(
            ir,
            current_files=[{"path": "src/lib.rs", "content": "current rust"}],
            diagnostics="compile error",
            manifest_history=[
                {
                    "round": 1,
                    "implemented_traits": ["DynamicFilters"],
                    "dependencies": [{"name": "serde"}],
                    "file_paths": ["src/lib.rs", "res/settings.json"],
                    "resource_files": [
                        {
                            "path": "res/settings.json",
                            "content": "historical-resource-body-marker",
                        }
                    ],
                }
            ],
        )
    assert result.manifest.source_struct == "Example"
