from pathlib import Path

import pytest

from convert2aidoku.checkpoint_store import CheckpointStore
from convert2aidoku.errors import AIProviderError
from convert2aidoku.live_validation_evidence import LiveValidationEvidence
from convert2aidoku.manifest_contract import ContractDiagnostic, ContractEvaluation
from convert2aidoku.models import (
    ConversionCheckpoint,
    DependencyRequest,
    RepairPatch,
    ValidationResult,
    ValidationStage,
)
from convert2aidoku.targeted_repair import (
    TargetedRepair,
    apply_repair_patch,
    diagnostic_file_excerpts,
    repair_diagnostics,
    repair_required,
    repair_round_limit,
    repair_state_signature,
)
from tests.scenarios import (
    generation_manifest,
    minimal_source_ir,
    provider_settings,
    scripted_ai_client,
)


def test_blocked_validation_never_repairs_even_with_contract_gaps() -> None:
    validation = ValidationResult(build_ok=True, package_ok=True, blocked=True)

    assert not repair_required(validation, [], live=True)
    assert not repair_required(validation, ["relative URL gap"], live=True)


def test_compile_and_contract_repairs_are_capped_at_two_rounds() -> None:
    compiler_failure = ValidationResult(
        stages=[ValidationStage(name="cargo-check", kind="check", ok=False, output="error")]
    )
    live_failure = ValidationResult(
        stages=[
            ValidationStage(name="core-live-smoke", kind="live_test", ok=False, output="wrong")
        ],
        build_ok=True,
        package_ok=True,
    )

    assert repair_round_limit(compiler_failure, [], live=True, configured_limit=8) == 2
    assert (
        repair_round_limit(
            ValidationResult(build_ok=True, package_ok=True),
            ["contract gap"],
            live=True,
            configured_limit=8,
        )
        == 2
    )
    assert repair_round_limit(live_failure, [], live=True, configured_limit=8) == 8
    assert (
        repair_round_limit(
            ValidationResult(build_ok=True, package_ok=True, blocked=True),
            ["contract gap"],
            live=True,
            configured_limit=8,
        )
        == 0
    )


def test_repair_diagnostics_include_live_validation_evidence(monkeypatch) -> None:
    ir = minimal_source_ir()
    monkeypatch.setattr(
        "convert2aidoku.targeted_repair.live_validation_evidence",
        lambda _ir: LiveValidationEvidence(repair_context="benchmark context"),
    )

    diagnostics = repair_diagnostics(
        ir,
        ValidationResult(blocked=True),
        ["relative URL gap"],
    )

    assert "benchmark context" in diagnostics
    assert "relative URL gap" in diagnostics


def test_repair_state_signature_ignores_unstable_rust_line_locations() -> None:
    first = ValidationResult(
        stages=[
            ValidationStage(
                name="cargo-check",
                kind="check",
                ok=False,
                output="error\n --> src/lib.rs:10:4\n10 | broken()",
            )
        ]
    )
    shifted = ValidationResult(
        stages=[
            ValidationStage(
                name="cargo-check",
                kind="check",
                ok=False,
                output="error\n --> src/lib.rs:99:8\n99 | broken()",
            )
        ]
    )

    assert repair_state_signature(first, ["same gap"]) == repair_state_signature(
        shifted, ["same gap"]
    )


def test_compiler_diagnostics_produce_bounded_source_excerpts(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    lines = [f"line {index}" for index in range(1, 41)]
    (source / "lib.rs").write_text("\n".join(lines) + "\n", encoding="utf-8")

    excerpts = diagnostic_file_excerpts(
        tmp_path,
        "error\n  --> src/lib.rs:20:5\nhelp\n  --> src/lib.rs:24:9",
        context_lines=3,
    )

    assert excerpts == [
        {
            "path": "src/lib.rs",
            "start_line": 17,
            "end_line": 27,
            "content": "\n".join(lines[16:27]),
        }
    ]


def test_compiler_diagnostics_accept_absolute_rust_paths(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "lib.rs").write_text("fn broken() {}\n", encoding="utf-8")

    excerpts = diagnostic_file_excerpts(
        tmp_path,
        f"error\n  --> {source / 'lib.rs'}:1:4",
        context_lines=0,
    )

    assert excerpts == [
        {
            "path": "src/lib.rs",
            "start_line": 1,
            "end_line": 1,
            "content": "fn broken() {}",
        }
    ]


def test_compiler_diagnostics_include_named_type_definition_excerpt(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    rust = (
        "struct Comic { title: String }\n"
        "\n"
        "fn unrelated() {}\n"
        "\n"
        "fn update(detail: Detail) {\n"
        "    consume(detail.comic.clone());\n"
        "}\n"
    )
    (source / "lib.rs").write_text(rust, encoding="utf-8")

    excerpts = diagnostic_file_excerpts(
        tmp_path,
        """error[E0599]: no method named `clone` found for struct `Comic`
  --> src/lib.rs:6:26
   |
 1 | struct Comic { title: String }
   | ------------ method `clone` not found for this struct
""",
        context_lines=0,
    )

    assert excerpts == [
        {
            "path": "src/lib.rs",
            "start_line": 1,
            "end_line": 1,
            "content": "struct Comic { title: String }",
        },
        {
            "path": "src/lib.rs",
            "start_line": 6,
            "end_line": 6,
            "content": "    consume(detail.comic.clone());",
        },
    ]


def test_repair_patch_requires_one_exact_match_and_preserves_manifest_metadata() -> None:
    manifest = generation_manifest(
        "let title = title;\n",
        traits=("DynamicFilters",),
        dependencies=(DependencyRequest(name="serde"),),
    )
    patch = RepairPatch.model_validate(
        {
            "edits": [
                {
                    "path": "src/lib.rs",
                    "old_text": "let title = title;",
                    "new_text": "let title = Some(title);",
                }
            ]
        }
    )

    repaired = apply_repair_patch(
        manifest,
        [{"path": "src/lib.rs", "content": "let title = title;\n"}],
        patch,
        [
            {
                "path": "src/lib.rs",
                "start_line": 1,
                "end_line": 1,
                "content": "let title = title;",
            }
        ],
    )

    assert repaired.files[0].content == "let title = Some(title);\n"
    assert repaired.implemented_traits == ["DynamicFilters"]
    assert repaired.dependencies == [DependencyRequest(name="serde")]


def test_repair_patch_cannot_edit_text_outside_supplied_excerpts() -> None:
    manifest = generation_manifest("safe();\nother();\n")
    patch = RepairPatch.model_validate(
        {"edits": [{"path": "src/lib.rs", "old_text": "other();", "new_text": "changed();"}]}
    )

    with pytest.raises(AIProviderError, match="not present in a supplied excerpt"):
        apply_repair_patch(
            manifest,
            [{"path": "src/lib.rs", "content": "safe();\nother();\n"}],
            patch,
            [{"path": "src/lib.rs", "content": "safe();"}],
        )


def test_targeted_repair_applies_compiler_patch_without_loading_history(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "workspace")
    source = store.project / "src"
    source.mkdir(parents=True)
    rust = "let title = title;\n"
    (source / "lib.rs").write_text(rust, encoding="utf-8")
    manifest = generation_manifest(rust)
    patch = RepairPatch.model_validate(
        {
            "edits": [
                {
                    "path": "src/lib.rs",
                    "old_text": "let title = title;",
                    "new_text": "let title = Some(title);",
                }
            ]
        }
    )
    adapter, calls = scripted_ai_client(
        generation=manifest,
        repair_patch=patch,
        patch_scope="compiler",
    )
    repair = TargetedRepair(
        ir=minimal_source_ir(),
        store=store,
        checkpoint=ConversionCheckpoint(
            input_ref="fixture",
            output="generated/en.example",
            provider_base_url="http://local/v1",
            model="test",
        ),
        manifest=manifest,
        validation=ValidationResult(
            stages=[
                ValidationStage(
                    name="cargo-check",
                    kind="check",
                    ok=False,
                    output="error\n  --> src/lib.rs:1:1",
                )
            ]
        ),
        contract=ContractEvaluation(()),
    )

    with adapter(provider_settings()) as client:
        result = repair.request(client)

    assert result.value.files[0].content == "let title = Some(title);\n"
    assert calls.repair_patch == 1
    assert calls.repair == 0


def test_format_parse_failure_uses_bounded_compiler_patch(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "workspace")
    source = store.project / "src"
    source.mkdir(parents=True)
    rust = "fn broken() { invalid(); }\n"
    source_path = source / "lib.rs"
    source_path.write_text(rust, encoding="utf-8")
    manifest = generation_manifest(rust)
    patch = RepairPatch.model_validate(
        {
            "edits": [
                {
                    "path": "src/lib.rs",
                    "old_text": "invalid();",
                    "new_text": "valid();",
                }
            ]
        }
    )
    adapter, calls = scripted_ai_client(
        generation=manifest,
        repair_patch=patch,
        patch_scope="compiler",
    )
    repair = TargetedRepair(
        ir=minimal_source_ir(),
        store=store,
        checkpoint=ConversionCheckpoint(
            input_ref="fixture",
            output="generated/en.example",
            provider_base_url="http://local/v1",
            model="test",
        ),
        manifest=manifest,
        validation=ValidationResult(
            stages=[
                ValidationStage(
                    name="format",
                    kind="format",
                    ok=False,
                    output=f"error: expected expression\n  --> {source_path}:1:15",
                )
            ]
        ),
        contract=ContractEvaluation(()),
    )

    with adapter(provider_settings()) as client:
        result = repair.request(client)

    assert "valid();" in result.value.files[0].content
    assert calls.repair_patch == 1
    assert calls.repair == 0


def test_compiler_patch_is_preferred_when_contract_gap_is_not_targetable(
    tmp_path: Path,
) -> None:
    store = CheckpointStore(tmp_path / "workspace")
    source = store.project / "src"
    source.mkdir(parents=True)
    rust = "fn broken() { old(); }\n"
    (source / "lib.rs").write_text(rust, encoding="utf-8")
    manifest = generation_manifest(rust)
    patch = RepairPatch.model_validate(
        {"edits": [{"path": "src/lib.rs", "old_text": "old();", "new_text": "fixed();"}]}
    )
    adapter, calls = scripted_ai_client(
        generation=manifest,
        repair_patch=patch,
        patch_scope="compiler",
    )
    repair = TargetedRepair(
        ir=minimal_source_ir(),
        store=store,
        checkpoint=ConversionCheckpoint(
            input_ref="fixture",
            output="generated/en.example",
            provider_base_url="http://local/v1",
            model="test",
        ),
        manifest=manifest,
        validation=ValidationResult(
            stages=[
                ValidationStage(
                    name="cargo-check",
                    kind="check",
                    ok=False,
                    output="error\n  --> src/lib.rs:1:15",
                )
            ]
        ),
        contract=ContractEvaluation((ContractDiagnostic("unscoped contract gap"),)),
    )

    with adapter(provider_settings()) as client:
        result = repair.request(client)

    assert "fixed();" in result.value.files[0].content
    assert calls.repair_patch == 1
    assert calls.repair == 0


def test_compiler_patch_precedes_targetable_contract_repair(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "workspace")
    source = store.project / "src"
    source.mkdir(parents=True)
    rust = "fn broken() { old(); }\n"
    (source / "lib.rs").write_text(rust, encoding="utf-8")
    manifest = generation_manifest(rust)

    class UnexpectedContract:
        messages = ["targetable contract gap"]

        def repair(self, _project: Path):
            raise AssertionError("contract repair must wait until the crate compiles")

    repair = TargetedRepair(
        ir=minimal_source_ir(),
        store=store,
        checkpoint=ConversionCheckpoint(
            input_ref="fixture",
            output="generated/en.example",
            provider_base_url="http://local/v1",
            model="test",
        ),
        manifest=manifest,
        validation=ValidationResult(
            stages=[
                ValidationStage(
                    name="cargo-check",
                    kind="check",
                    ok=False,
                    output="error\n  --> src/lib.rs:1:15",
                )
            ]
        ),
        contract=UnexpectedContract(),  # type: ignore[arg-type]
    )

    request = repair._patch_request(repair.validation.diagnostics)

    assert request is not None
    assert request.scope == "compiler"
