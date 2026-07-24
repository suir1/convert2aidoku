import os
from pathlib import Path

import pytest

from convert2aidoku.checkpoint_store import CheckpointStore, ManifestWrite
from convert2aidoku.conversion_completion import complete_conversion
from convert2aidoku.models import (
    ConversionCheckpoint,
    ConversionStatus,
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
