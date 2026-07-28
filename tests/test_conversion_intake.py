from pathlib import Path

import pytest

from convert2aidoku.conversion_intake import ConversionIntake
from convert2aidoku.errors import InputError
from convert2aidoku.generated_source_metadata import GeneratedSourceMetadata
from tests.scenarios import SIMPLE_FIXTURE, conversion_settings


def test_fresh_intake_prepares_scaffold_source_ir_and_checkpoint(tmp_path: Path) -> None:
    output = tmp_path / "generated" / "en.simple"

    intake = ConversionIntake.prepare(
        str(SIMPLE_FIXTURE),
        output=output,
        settings=conversion_settings(),
    )

    assert intake.output == output.absolute()
    assert intake.project.is_dir()
    assert intake.source_ir.metadata.source_id == "en.simple"
    assert intake.checkpoint.output == str(output.absolute())
    assert (intake.project / "res" / "source.json").is_file()
    assert "secret" not in (intake.store.workspace / "checkpoint.json").read_text()


def test_fresh_intake_removes_incomplete_workspace_on_failure(tmp_path: Path) -> None:
    output = tmp_path / "generated" / "en.missing"
    workspace = output.parent / f".{output.name}.c2a-work"

    with pytest.raises(InputError, match="input does not exist"):
        ConversionIntake.prepare(
            str(tmp_path / "missing"),
            output=output,
            settings=conversion_settings(),
        )

    assert not workspace.exists()


def test_resume_rejects_mismatched_saved_options_without_removing_workspace(
    tmp_path: Path,
) -> None:
    output = tmp_path / "generated" / "en.simple"
    intake = ConversionIntake.prepare(
        str(SIMPLE_FIXTURE),
        output=output,
        settings=conversion_settings(),
    )

    with pytest.raises(InputError, match="model"):
        ConversionIntake.prepare(
            str(SIMPLE_FIXTURE),
            output=output,
            settings=conversion_settings(model="different"),
            resume=True,
        )

    assert intake.store.workspace.is_dir()
    assert intake.store.read_checkpoint() == intake.checkpoint


def test_resume_refreshes_legacy_source_ir(tmp_path: Path) -> None:
    output = tmp_path / "generated" / "en.simple"
    settings = conversion_settings()
    intake = ConversionIntake.prepare(
        str(SIMPLE_FIXTURE),
        output=output,
        settings=settings,
    )
    intake.store.commit(source_ir=intake.source_ir.model_copy(update={"schema_version": 1}))

    resumed = ConversionIntake.prepare(
        str(SIMPLE_FIXTURE),
        output=output,
        settings=settings,
        resume=True,
    )

    assert resumed.source_ir.schema_version == 3
    assert resumed.store.read_source_ir().schema_version == 3


def test_completed_resume_bumps_generated_source_and_source_ir_versions(tmp_path: Path) -> None:
    output = tmp_path / "generated" / "en.simple"
    settings = conversion_settings()
    intake = ConversionIntake.prepare(
        str(SIMPLE_FIXTURE),
        output=output,
        settings=settings,
    )
    previous = GeneratedSourceMetadata.load(intake.project).version
    intake.checkpoint.phase = "complete"
    intake.store.commit(checkpoint=intake.checkpoint)

    resumed = ConversionIntake.prepare(
        str(SIMPLE_FIXTURE),
        output=output,
        settings=settings,
        resume=True,
    )

    assert resumed.source_ir.metadata.version == previous + 1
    assert GeneratedSourceMetadata.load(resumed.project).version == previous + 1
    assert resumed.store.read_source_ir().metadata.version == previous + 1
