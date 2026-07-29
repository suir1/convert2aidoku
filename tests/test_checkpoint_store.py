import shutil
from pathlib import Path

import pytest

from convert2aidoku.checkpoint_store import CheckpointStore, ManifestWrite
from convert2aidoku.errors import InputError
from convert2aidoku.models import (
    ConversionCheckpoint,
    GeneratedFile,
    GenerationManifest,
)
from tests.scenarios import minimal_source_ir


def _checkpoint(output: Path) -> ConversionCheckpoint:
    return ConversionCheckpoint(
        input_ref="fixture",
        output=str(output),
        provider_base_url="http://provider/v1",
        model="model",
    )


def _manifest(content: str = "fn source() {}") -> GenerationManifest:
    return GenerationManifest(
        source_struct="Example",
        files=[GeneratedFile(path="src/lib.rs", content=content)],
    )


def test_initialize_and_commit_preserve_raw_typed_state_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ir = minimal_source_ir().model_copy(update={"license_text": "private license body"})
    checkpoint = _checkpoint(tmp_path / "output")
    writes: list[str] = []
    atomic_write = CheckpointStore._atomic_write

    def record_write(path: Path, content: str) -> None:
        writes.append(path.name)
        atomic_write(path, content)

    monkeypatch.setattr(CheckpointStore, "_atomic_write", staticmethod(record_write))

    store = CheckpointStore.initialize(
        workspace,
        source_ir=ir,
        checkpoint=checkpoint,
    )
    manifest = _manifest()
    checkpoint.current_manifest = store.round_path(1)
    store.commit(
        source_ir=ir,
        manifest=ManifestWrite(1, manifest),
        checkpoint=checkpoint,
    )

    assert writes == [
        "source-ir.json",
        "implementation-ir.json",
        "checkpoint.json",
        "source-ir.json",
        "implementation-ir.json",
        "round-01.json",
        "checkpoint.json",
    ]
    assert store.read_checkpoint() == checkpoint
    assert store.read_source_ir().license_text is None
    assert store.read_implementation_ir().source_id == ir.metadata.source_id
    assert store.read_manifest(checkpoint.current_manifest) == manifest
    assert "private license body" not in (workspace / "source-ir.json").read_text()
    assert not list(workspace.rglob(".*.tmp-*"))


@pytest.mark.parametrize(
    "relative",
    [
        "../outside.json",
        "/tmp/outside.json",
        "other/round-01.json",
        "manifests/nested/round-01.json",
    ],
)
def test_manifest_paths_cannot_escape_the_store(tmp_path: Path, relative: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CheckpointStore(workspace)

    with pytest.raises(InputError, match="invalid manifest path in resume checkpoint"):
        store.read_manifest(relative)


def test_manifest_symlink_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    manifests = workspace / "manifests"
    manifests.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text(_manifest().model_dump_json())
    (manifests / "round-01.json").symlink_to(outside)
    store = CheckpointStore(workspace)

    with pytest.raises(InputError, match="refusing symbolic-link resume manifest"):
        store.read_round(1)


def test_missing_and_corrupt_store_files_keep_specific_errors(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CheckpointStore(workspace)

    with pytest.raises(InputError, match="resume workspace has no checkpoint"):
        store.read_checkpoint()

    (workspace / "checkpoint.json").write_text("not json")
    with pytest.raises(InputError, match="invalid resume checkpoint"):
        store.read_checkpoint()

    (workspace / "source-ir.json").write_text("not json")
    with pytest.raises(InputError, match="invalid saved SourceIR"):
        store.read_source_ir()

    manifests = workspace / "manifests"
    manifests.mkdir()
    with pytest.raises(InputError, match="saved generation manifest is missing"):
        store.read_round(1)
    (manifests / "round-01.json").write_text("not json")
    with pytest.raises(InputError, match="invalid saved generation manifest"):
        store.read_round(1)


def test_publish_replaces_audit_and_returns_stable_file_order(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ir = minimal_source_ir()
    checkpoint = _checkpoint(tmp_path / "output")
    checkpoint.current_manifest = CheckpointStore.round_path(2)
    store = CheckpointStore.initialize(
        workspace,
        source_ir=ir,
        checkpoint=checkpoint,
    )
    store.commit(manifest=ManifestWrite(2, _manifest("fn second() {}")))
    store.commit(manifest=ManifestWrite(1, _manifest("fn first() {}")))
    project = tmp_path / "project"
    stale = project / ".c2a"
    stale.mkdir(parents=True)
    (stale / "stale.txt").write_text("stale")

    files = store.publish_audit(project)

    assert files == [
        ".c2a/checkpoint.json",
        ".c2a/source-ir.json",
        ".c2a/implementation-ir.json",
        ".c2a/manifests/round-01.json",
        ".c2a/manifests/round-02.json",
    ]
    assert not (project / ".c2a" / "stale.txt").exists()
    assert GenerationManifest.model_validate_json(
        (project / ".c2a" / "manifests" / "round-01.json").read_text()
    ) == _manifest("fn first() {}")


def test_resume_restores_an_installed_audit(tmp_path: Path) -> None:
    original_workspace = tmp_path / "original-workspace"
    original_workspace.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    (output / "keep.txt").write_text("generated source")
    ir = minimal_source_ir()
    checkpoint = _checkpoint(output)
    checkpoint.current_manifest = CheckpointStore.round_path(1)
    original = CheckpointStore.initialize(
        original_workspace,
        source_ir=ir,
        checkpoint=checkpoint,
    )
    original.commit(manifest=ManifestWrite(1, _manifest()))
    original.publish_audit(output)
    # Installed outputs from before Implementation IR remain resumable.
    (output / ".c2a" / "implementation-ir.json").unlink()
    shutil.rmtree(original_workspace)
    restored_workspace = tmp_path / "restored-workspace"

    restored, loaded = CheckpointStore.resume(
        restored_workspace,
        installed_output=output,
    )

    assert loaded == checkpoint
    assert not output.exists()
    assert (restored.project / "keep.txt").read_text() == "generated source"
    assert restored.read_source_ir() == ir.model_copy(update={"license_text": None})
    assert restored.read_implementation_ir().source_id == ir.metadata.source_id
    assert restored.read_round(1) == _manifest()


def test_invalid_installed_checkpoint_does_not_move_output(tmp_path: Path) -> None:
    output = tmp_path / "output"
    audit = output / ".c2a"
    audit.mkdir(parents=True)
    (audit / "checkpoint.json").write_text("not json")
    workspace = tmp_path / "workspace"

    with pytest.raises(InputError, match="invalid installed conversion checkpoint"):
        CheckpointStore.resume(workspace, installed_output=output)

    assert output.is_dir()
    assert not workspace.exists()


def test_incomplete_installed_audit_rolls_output_back(tmp_path: Path) -> None:
    output = tmp_path / "output"
    audit = output / ".c2a"
    audit.mkdir(parents=True)
    checkpoint = _checkpoint(output)
    (audit / "checkpoint.json").write_text(checkpoint.model_dump_json())
    (audit / "manifests").mkdir()
    workspace = tmp_path / "workspace"

    with pytest.raises(FileNotFoundError):
        CheckpointStore.resume(workspace, installed_output=output)

    assert output.is_dir()
    assert (output / ".c2a" / "checkpoint.json").is_file()
    assert not workspace.exists()


def test_resume_rejects_workspace_symlink_and_missing_state(tmp_path: Path) -> None:
    output = tmp_path / "output"
    workspace = tmp_path / "workspace"

    with pytest.raises(InputError, match="no resumable conversion workspace"):
        CheckpointStore.resume(workspace, installed_output=output)

    target = tmp_path / "target"
    target.mkdir()
    workspace.symlink_to(target)
    with pytest.raises(InputError, match="refusing symbolic-link resume workspace"):
        CheckpointStore.resume(workspace, installed_output=output)
