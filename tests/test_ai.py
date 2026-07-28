import json
import time

import httpx
import pytest

from convert2aidoku.ai import OpenAICompatibleClient, _contract_text, _strict_model_schema
from convert2aidoku.config import ReasoningEffort
from convert2aidoku.errors import AIProviderError
from convert2aidoku.models import (
    AIUsage,
    Capability,
    GenerationManifest,
    RepairPatch,
    SourceFile,
    SourceFilterOption,
    SourceFilterSpec,
)
from tests.scenarios import minimal_source_ir, provider_settings


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
    schema = _strict_model_schema(GenerationManifest)

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
        "decompiled_dto_shapes",
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

    settings = provider_settings()
    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = client._request_manifest([{"role": "user", "content": "test"}])
    assert result.structured_output
    assert result.value.source_struct == "Example"
    assert result.usage and result.usage.total_tokens == 12


def test_falls_back_when_json_schema_is_rejected() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if payload.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, text="unsupported response_format")
        assert payload["response_format"]["type"] == "json_object"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_manifest())}}]},
        )

    settings = provider_settings()
    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = client._request_manifest([{"role": "user", "content": "test"}])
    assert calls == 2
    assert not result.structured_output
    assert result.warnings


def test_json_schema_rejection_is_remembered_for_later_exchange() -> None:
    response_formats: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        response_format = payload.get("response_format", {}).get("type")
        response_formats.append(response_format)
        if response_format == "json_schema":
            return httpx.Response(400, text="response_format unavailable")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_manifest())}}]},
        )

    settings = provider_settings()
    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        client._request_manifest([{"role": "user", "content": "first"}])
        second = client._request_manifest([{"role": "user", "content": "second"}])

    assert response_formats == ["json_schema", "json_object", "json_object"]
    assert not second.structured_output


def test_json_object_rejection_falls_back_to_plain_json() -> None:
    response_formats: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        response_format = payload.get("response_format", {}).get("type")
        response_formats.append(response_format)
        if response_format is not None:
            return httpx.Response(400, text="response_format unavailable")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_manifest())}}]},
        )

    settings = provider_settings()
    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = client._request_manifest([{"role": "user", "content": "test"}])

    assert response_formats == ["json_schema", "json_object", None]
    assert not result.structured_output


def test_json_fallback_accepts_one_object_with_trailing_explanation() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(_manifest()) + "\nGeneration manifest complete."
                        }
                    }
                ]
            },
        )

    settings = provider_settings()
    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        client._response_mode = "plain"
        result = client._request_manifest([{"role": "user", "content": "test"}])

    assert calls == 1
    assert result.value.source_struct == "Example"


@pytest.mark.parametrize(
    "content",
    [
        "Explanation before JSON.\n" + json.dumps(_manifest()),
        json.dumps(_manifest()) + "\n" + json.dumps({"second": True}),
        json.dumps(_manifest()) + "\n```sh\nrm -rf generated\n```",
    ],
)
def test_json_fallback_rejects_non_unique_or_active_trailing_content(content: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    settings = provider_settings()
    with (
        OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(AIProviderError),
    ):
        client._response_mode = "plain"
        client._request_model(
            [{"role": "user", "content": "test"}],
            GenerationManifest,
            max_validation_attempts=1,
        )


def test_typed_exchange_falls_back_for_repair_patch() -> None:
    calls = 0
    patch = {
        "edits": [
            {
                "path": "src/lib.rs",
                "old_text": "old();",
                "new_text": "new();",
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if payload.get("response_format", {}).get("type") == "json_schema":
            assert payload["response_format"]["json_schema"]["name"] == "aidoku_repair_patch"
            return httpx.Response(422, text="json_schema unsupported")
        assert payload["response_format"]["type"] == "json_object"
        assert "matching this repair patch schema" in payload["messages"][-1]["content"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(patch)}}]},
        )

    settings = provider_settings()
    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = client._request_model([{"role": "user", "content": "test"}], RepairPatch)

    assert calls == 2
    assert not result.structured_output
    assert result.value.edits[0].new_text == "new();"


def test_response_format_fallback_does_not_consume_validation_retries() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if payload.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, text="json_schema unsupported")
        manifest = _manifest()
        if calls < 4:
            manifest["files"] = [{"path": "Cargo.toml", "content": "forbidden"}]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(manifest)}}]},
        )

    settings = provider_settings()
    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = client._request_manifest([{"role": "user", "content": "test"}])

    assert calls == 4
    assert result.value.source_struct == "Example"


def test_terminal_provider_error_is_not_retried_as_invalid_model_output() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(402, text="Insufficient Balance")

    settings = provider_settings()
    with (
        OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(AIProviderError, match="HTTP 402"),
    ):
        client._request_manifest([{"role": "user", "content": "test"}])

    assert calls == 1


def test_request_timeout_is_total_not_reset_by_provider_activity() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        time.sleep(0.1)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_manifest())}}]},
        )

    settings = provider_settings().model_copy(update={"timeout_seconds": 0.01})
    with (
        OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(AIProviderError, match="exceeded total timeout"),
    ):
        client._request_manifest([{"role": "user", "content": "test"}])

    assert calls == 1


def test_reasoning_effort_rejection_is_remembered_without_consuming_retry() -> None:
    calls = 0
    reasoning_values: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        reasoning = payload.get("reasoning_effort")
        reasoning_values.append(reasoning)
        if reasoning is not None:
            return httpx.Response(400, text="unsupported parameter reasoning_effort")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_manifest())}}]},
        )

    settings = provider_settings()
    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        first = client._request_model(
            [{"role": "user", "content": "first"}],
            GenerationManifest,
            reasoning_effort=ReasoningEffort.LOW,
        )
        second = client._request_model(
            [{"role": "user", "content": "second"}],
            GenerationManifest,
            reasoning_effort=ReasoningEffort.LOW,
        )

    assert calls == 3
    assert reasoning_values == ["low", None, None]
    assert first.reasoning_effort is None
    assert first.warnings
    assert second.reasoning_effort is None


def test_ai_check_disables_thinking() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["thinking"] == {"type": "disabled"}
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok":true}'}}]},
        )

    settings = provider_settings()
    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = client.check()

    assert result.ok
    assert result.structured_output


def test_thinking_rejection_is_remembered_for_later_check() -> None:
    calls = 0
    thinking_values: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        thinking = payload.get("thinking")
        thinking_values.append(thinking)
        if thinking is not None:
            return httpx.Response(400, text="unknown parameter thinking")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok":true}'}}]},
        )

    settings = provider_settings()
    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        first = client.check()
        second = client.check()

    assert calls == 3
    assert thinking_values == [{"type": "disabled"}, None, None]
    assert first.ok and second.ok


def test_manifest_dependency_policy_participates_in_validation_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if calls == 2:
            assert "disallowed dependencies: reqwest" in payload["messages"][-1]["content"]
        manifest = _manifest()
        if calls == 1:
            manifest["dependencies"] = [{"name": "reqwest"}]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(manifest)}}]},
        )

    settings = provider_settings()
    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = client._request_manifest([{"role": "user", "content": "test"}])

    assert calls == 2
    assert result.value.dependencies == []
    assert any("disallowed dependencies: reqwest" in warning for warning in result.warnings)


def test_manifest_security_violation_is_retried_and_usage_is_accumulated() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        manifest = _manifest()
        if calls == 1:
            manifest["files"].append({"path": "Cargo.toml", "content": "forbidden"})
            usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        else:
            assert "outside the allowlist: Cargo.toml" in payload["messages"][-1]["content"]
            usage = {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(manifest)}}],
                "usage": usage,
            },
        )

    settings = provider_settings()
    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = client._request_manifest([{"role": "user", "content": "test"}])

    assert calls == 2
    assert [item.path for item in result.value.files] == ["src/lib.rs"]
    assert result.usage is not None
    assert result.usage.prompt_tokens == 18
    assert result.usage.completion_tokens == 9
    assert result.usage.total_tokens == 27
    assert any("outside the allowlist: Cargo.toml" in warning for warning in result.warnings)


def test_manifest_rust_content_security_violation_is_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        manifest = _manifest()
        if calls == 1:
            manifest["files"][0]["content"] = "use std::fs;"
        else:
            assert "generated Rust uses std" in payload["messages"][-1]["content"]
            assert "use std::fs;" in payload["messages"][-1]["content"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(manifest)}}]},
        )

    settings = provider_settings()
    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = client._request_manifest([{"role": "user", "content": "test"}])

    assert calls == 2
    assert result.value.files[0].content == "#![no_std]"


def test_initial_generation_normalizes_safe_std_allocations_without_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        manifest = _manifest()
        manifest["files"] = [
            {
                "path": "src/source.rs",
                "content": (
                    "use std::collections::HashMap;\n"
                    "pub struct Example;\n"
                    "fn values() { let _ = HashMap::<String, String>::new(); }"
                ),
            }
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(manifest)}}]},
        )

    settings = provider_settings()
    ir = minimal_source_ir(
        files=[SourceFile(path="src/Example.kt", content="class Example", sha256="0")]
    )

    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = client.generate(ir)

    assert calls == 1
    files = {item.path: item.content for item in result.value.files}
    assert "std::" not in files["src/lib.rs"]
    assert "mod source;" in files["src/lib.rs"]
    assert "pub use source::Example;" in files["src/lib.rs"]
    assert "BTreeMap::<String, String>" in files["src/source.rs"]


def test_initial_generation_stops_after_two_unsafe_full_outputs() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        manifest = _manifest()
        manifest["files"][0]["content"] = "use std::fs;"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(manifest)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    settings = provider_settings()
    ir = minimal_source_ir(
        files=[SourceFile(path="src/Example.kt", content="class Example", sha256="0")]
    )

    with (
        OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(AIProviderError, match="generated Rust uses std") as raised,
    ):
        client.generate(ir)

    assert calls == 2
    assert raised.value.usage == AIUsage(
        prompt_tokens=20,
        completion_tokens=10,
        total_tokens=30,
    )


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

    settings = provider_settings()
    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = client._request_manifest([{"role": "user", "content": "test"}])

    assert calls == 2
    assert result.value.files[1].path == "res/filters.json"


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

    settings = provider_settings()
    with OpenAICompatibleClient(
        settings,
        transport=httpx.MockTransport(handler),
        sleep=delays.append,
    ) as client:
        result = client._request_manifest([{"role": "user", "content": "test"}])

    assert result.value.source_struct == "Example"
    assert calls == 3
    assert delays == [5.0, 15.0]


def test_generate_sends_deterministic_source_evidence_instead_of_raw_files() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        system_prompt = payload["messages"][0]["content"]
        generation_payload = json.loads(payload["messages"][1]["content"].split("\n\n", 1)[1])
        assert "FilterValue::Select { id: String, value: String }" in system_prompt
        assert "Option<&'a str>" in system_prompt
        assert "Cargo.toml is forbidden" in payload["messages"][1]["content"]
        assert "reasoning_effort" not in payload
        assert "thinking" not in payload
        assert "source_files" not in generation_payload
        assert generation_payload["source_evidence"][0]["path"] == "src/Example.kt"
        assert generation_payload["context_stats"]["mode"] == "complete_kotlin_source"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_manifest())}}]},
        )

    settings = provider_settings()
    ir = minimal_source_ir(
        files=[SourceFile(path="src/Example.kt", content="class Example", sha256="0")],
    )

    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = client.generate(ir)

    assert result.value.source_struct == "Example"
    assert result.reasoning_effort == ReasoningEffort.AUTO


def test_generate_structures_settings_and_owns_static_filters() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if calls == 1:
            assert "only src/**/*.rs" in payload["messages"][1]["content"]
            assert "filters.json or settings.json" in payload["messages"][1]["content"]
            response = _manifest()
            usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        else:
            assert payload["reasoning_effort"] == "low"
            assert "unrelated-settings-test-marker" not in request.content.decode()
            response = {
                "groups": [
                    {
                        "type": "group",
                        "title": "Request",
                        "items": [
                            {
                                "type": "select",
                                "key": "platform",
                                "title": "Platform",
                                "values": ["1", "2"],
                                "titles": ["1", "2"],
                                "default": "1",
                            }
                        ],
                    }
                ]
            }
            usage = {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(response)}}],
                "usage": usage,
            },
        )

    ir = minimal_source_ir(
        capabilities=[Capability.FILTERS, Capability.SETTINGS],
        filter_specs=[
            SourceFilterSpec(
                source_class="SortFilter",
                id="sort",
                title="Sort",
                kind="sort",
                options=[
                    SourceFilterOption(title="Popular", value="popular"),
                    SourceFilterOption(title="Updated", value="updated"),
                ],
                default_index=1,
                default_ascending=True,
            )
        ],
        files=[
            SourceFile(
                path="src/PlatformOption.kt",
                content='const val KEY = "platform"\nconst val DEFAULT = "1"',
                sha256="0",
            ),
            SourceFile(
                path="src/Unrelated.kt",
                content='const val value = "unrelated-settings-test-marker"',
                sha256="1",
            ),
        ],
    )
    settings = provider_settings()

    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = client.generate(ir)

    assert calls == 2
    assert result.usage and result.usage.total_tokens == 160
    resources = {item.path: item.content for item in result.value.files}
    assert set(resources) == {"src/lib.rs", "res/filters.json", "res/settings.json"}
    assert json.loads(resources["res/filters.json"])[0]["default"] == {
        "index": 1,
        "ascending": True,
    }
    assert json.loads(resources["res/settings.json"])[0]["items"][0]["default"] == "1"


def test_repair_uses_compact_context_without_original_source_bodies() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        repair_payload = json.loads(payload["messages"][1]["content"])
        assert payload["reasoning_effort"] == "low"
        assert "source_files" not in repair_payload
        assert "files" not in repair_payload["source_ir"]
        assert repair_payload["current_files"][0]["content"] == "current rust"
        assert len(repair_payload["current_files"]) == 1
        assert b"current-resource-marker" not in request.content
        assert repair_payload["prior_generation_manifests"][0]["round"] == 1
        assert "resource_files" not in repair_payload["prior_generation_manifests"][0]
        assert b"original-source-body-marker" not in request.content
        assert b"historical-resource-body-marker" not in request.content
        assert len(request.content) < 80_000
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_manifest())}}]},
        )

    settings = provider_settings()
    ir = minimal_source_ir(
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
            current_files=[
                {"path": "src/lib.rs", "content": "current rust"},
                {"path": "res/settings.json", "content": "current-resource-marker"},
            ],
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
    assert result.value.source_struct == "Example"


def test_compiler_repair_sends_only_excerpts_and_returns_exact_edits() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["response_format"]["json_schema"]["name"] == "aidoku_repair_patch"
        assert payload["reasoning_effort"] == "low"
        repair_payload = json.loads(payload["messages"][1]["content"])
        assert "current_files" not in repair_payload
        assert "source_ir" not in repair_payload
        assert repair_payload["current_file_excerpts"][0]["start_line"] == 10
        assert len(request.content) < 12_000
        patch = {
            "edits": [
                {
                    "path": "src/lib.rs",
                    "old_text": "title,",
                    "new_text": "title: Some(title),",
                }
            ]
        }
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(patch)}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 80, "total_tokens": 980},
            },
        )

    settings = provider_settings()
    ir = minimal_source_ir()
    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = client.repair_patch(
            ir,
            current_file_excerpts=[
                {
                    "path": "src/lib.rs",
                    "start_line": 10,
                    "end_line": 20,
                    "content": "Chapter { title, ..Default::default() }",
                }
            ],
            diagnostics="error[E0308]: mismatched types",
        )

    assert result.value.edits[0].new_text == "title: Some(title),"
    assert result.usage and result.usage.total_tokens == 980


def test_contract_repair_sends_only_scoped_excerpts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "contract or performance" in payload["messages"][0]["content"]
        repair_payload = json.loads(payload["messages"][1]["content"])
        assert "current_files" not in repair_payload
        assert repair_payload["contract_diagnostics"].startswith("standard Kotlin HttpSource")
        assert len(request.content) < 12_000
        patch = {
            "edits": [
                {
                    "path": "src/lib.rs",
                    "old_text": "self.request(url)?.send()?",
                    "new_text": "self.request(url.clone())?.send()?",
                }
            ]
        }
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(patch)}}],
                "usage": {"prompt_tokens": 700, "completion_tokens": 60, "total_tokens": 760},
            },
        )

    settings = provider_settings()
    ir = minimal_source_ir()
    with OpenAICompatibleClient(settings, transport=httpx.MockTransport(handler)) as client:
        result = client.repair_patch(
            ir,
            current_file_excerpts=[
                {
                    "path": "src/lib.rs",
                    "start_line": 10,
                    "end_line": 14,
                    "content": "self.request(url)?.send()?",
                }
            ],
            diagnostics="standard Kotlin HttpSource generated no one-retry helper",
            scope="contract",
        )

    assert result.value.edits[0].old_text == "self.request(url)?.send()?"
    assert result.usage and result.usage.total_tokens == 760
