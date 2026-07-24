import pytest

from convert2aidoku.errors import AIProviderError
from tests import scenarios


def test_scenarios_return_fresh_defaults() -> None:
    first_ir = scenarios.minimal_source_ir()
    second_ir = scenarios.minimal_source_ir()
    first = scenarios.generation_manifest("first")
    second = scenarios.generation_manifest("second")

    assert first_ir.files is not second_ir.files
    assert first.files is not second.files


def test_scripted_ai_clients_have_isolated_calls_and_checked_patch_routes() -> None:
    generation = scenarios.generation_manifest("generate")
    repair = scenarios.generation_manifest("repair")
    first_adapter, first_calls = scenarios.scripted_ai_client(generation=generation, repair=repair)
    second_adapter, second_calls = scenarios.scripted_ai_client(generation=generation)

    with first_adapter(scenarios.provider_settings()) as client:
        assert client.generate(object()).value is generation
        assert client.repair(object(), current_files=[], diagnostics="failure").value is repair
    with second_adapter(scenarios.provider_settings()) as client:
        assert client.generate(object()).value is generation

    assert first_calls == scenarios.ScriptedAICalls(generate=1, repair=1)
    assert second_calls == scenarios.ScriptedAICalls(generate=1)

    adapter, calls = scenarios.scripted_ai_client(
        generation=generation,
        repair_patch=AIProviderError("invalid patch"),
        patch_scope="contract",
        patch_diagnostic="expected gap",
    )

    with pytest.raises(AIProviderError):
        adapter(scenarios.provider_settings()).repair_patch(
            object(),
            current_file_excerpts=[{"path": "src/lib.rs"}],
            diagnostics="expected gap found",
            scope="contract",
        )

    assert calls.repair_patch == 1
