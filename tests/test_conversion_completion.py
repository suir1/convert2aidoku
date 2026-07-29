import os
from pathlib import Path

import pytest

from convert2aidoku.checkpoint_store import CheckpointStore, ManifestWrite
from convert2aidoku.conversion_completion import complete_conversion
from convert2aidoku.models import (
    ConversionCheckpoint,
    ConversionStatus,
    GenerationManifest,
    SourceIR,
    ValidationResult,
)
from tests.scenarios import generation_manifest, scaffold_project


def _completion_scenario(
    tmp_path: Path,
) -> tuple[CheckpointStore, SourceIR, ConversionCheckpoint, Path]:
    workspace = tmp_path / ".en.simple.c2a-work"
    _project, ir = scaffold_project(workspace)
    output = tmp_path / "generated" / "en.simple"
    output.parent.mkdir()
    checkpoint = ConversionCheckpoint(
        input_ref="fixture",
        output=str(output),
        provider_base_url="http://local/v1",
        model="test",
        live=True,
        current_manifest=CheckpointStore.round_path(1),
        generated_files=["src/lib.rs"],
    )
    store = CheckpointStore.initialize(
        workspace,
        source_ir=ir,
        checkpoint=checkpoint,
    )
    store.commit(manifest=ManifestWrite(1, generation_manifest("fn source() {}")))
    return store, ir, checkpoint, output


def test_completion_installs_verified_source_and_removes_workspace(tmp_path: Path) -> None:
    store, ir, checkpoint, output = _completion_scenario(tmp_path)

    outcome = complete_conversion(
        store,
        ir,
        checkpoint,
        ValidationResult(build_ok=True, package_ok=True, live_ok=True),
    )

    assert outcome.output == output
    assert outcome.report.status is ConversionStatus.VERIFIED
    assert (output / "report.json").is_file()
    assert (output / ".c2a" / "checkpoint.json").is_file()
    assert not store.workspace.exists()


def test_completion_reports_deterministic_findings_not_raw_manifest_claims(
    tmp_path: Path,
) -> None:
    store, ir, checkpoint, output = _completion_scenario(tmp_path)
    ir = ir.model_copy(update={"unsupported_features": ["input unsupported"]})
    raw_manifest = generation_manifest("fn source() {}").model_copy(
        update={
            "warnings": ["model warning superseded by deterministic projection"],
            "unsupported_features": ["model unsupported claim"],
        }
    )
    store.commit(manifest=ManifestWrite(1, raw_manifest))
    checkpoint.warnings = ["input or provider warning"]
    checkpoint.manifest_warnings = list(raw_manifest.warnings)
    checkpoint.capability_gaps = ["deterministic contract gap"]
    checkpoint.unsupported_features = [
        "input unsupported",
        *raw_manifest.unsupported_features,
    ]

    outcome = complete_conversion(
        store,
        ir,
        checkpoint,
        ValidationResult(build_ok=True, package_ok=True, live_ok=True),
    )

    assert outcome.report.warnings == [
        "input or provider warning",
        "deterministic contract gap",
    ]
    assert outcome.report.unsupported_features == ["input unsupported"]
    audited = GenerationManifest.model_validate_json(
        (output / ".c2a" / "manifests" / "round-01.json").read_text(encoding="utf-8")
    )
    assert audited.warnings == raw_manifest.warnings
    assert audited.unsupported_features == raw_manifest.unsupported_features


def test_completion_keeps_contract_failure_resumable(tmp_path: Path) -> None:
    store, ir, checkpoint, output = _completion_scenario(tmp_path)

    outcome = complete_conversion(
        store,
        ir,
        checkpoint,
        ValidationResult(
            build_ok=True,
            package_ok=True,
            live_ok=True,
            contract_ok=False,
        ),
    )

    saved = store.read_checkpoint()
    audit = ConversionCheckpoint.model_validate_json(
        (store.project / ".c2a" / "checkpoint.json").read_text(encoding="utf-8")
    )
    assert outcome.output == store.project
    assert outcome.report.status is ConversionStatus.BUILD_ONLY
    assert saved.phase == audit.phase == "validated"
    assert store.workspace.is_dir()
    assert not output.exists()


def test_completion_restores_old_output_when_atomic_install_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, ir, checkpoint, output = _completion_scenario(tmp_path)
    output.mkdir()
    (output / "keep.txt").write_text("old", encoding="utf-8")
    replace = os.replace

    def interrupted_replace(source: Path, destination: Path) -> None:
        if Path(source) == store.project and Path(destination) == output:
            raise OSError("synthetic install interruption")
        replace(source, destination)

    monkeypatch.setattr("convert2aidoku.conversion_completion.os.replace", interrupted_replace)

    with pytest.raises(OSError, match="install interruption"):
        complete_conversion(
            store,
            ir,
            checkpoint,
            ValidationResult(build_ok=True, package_ok=True, live_ok=True),
        )

    assert (output / "keep.txt").read_text(encoding="utf-8") == "old"
    assert store.project.is_dir()
    assert not list(output.parent.glob(f".{output.name}.backup-*"))
