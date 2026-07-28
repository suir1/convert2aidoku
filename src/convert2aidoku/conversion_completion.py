from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .checkpoint_store import CheckpointStore
from .models import (
    ConversionCheckpoint,
    ConversionReport,
    ConversionStatus,
    SourceIR,
    ValidationResult,
)
from .reports import classify_status, write_report
from .templates import match_templates


@dataclass(frozen=True)
class ConversionOutcome:
    output: Path
    report: ConversionReport
    source_ir: SourceIR


def _generated_files(store: CheckpointStore, checkpoint: ConversionCheckpoint) -> list[str]:
    project = store.project
    deterministic = [
        ".cargo/config.toml",
        "Cargo.toml",
        "res/source.json",
        "res/icon.png",
        "PROVENANCE.md",
        "report.json",
        "report.md",
    ]
    if (project / "LICENSE.input").is_file():
        deterministic.append("LICENSE.input")
    if (project / "package.aix").is_file():
        deterministic.append("package.aix")
    return sorted(set(checkpoint.generated_files + deterministic + store.audit_files()))


def _report(
    store: CheckpointStore,
    ir: SourceIR,
    checkpoint: ConversionCheckpoint,
    validation: ValidationResult,
) -> ConversionReport:
    return ConversionReport(
        status=classify_status(validation, live_requested=checkpoint.live),
        input_ref=checkpoint.input_ref,
        source_id=ir.metadata.source_id,
        provider_base_url=checkpoint.provider_base_url,
        model=checkpoint.model,
        ai_rounds=checkpoint.ai_rounds,
        failed_ai_exchanges=checkpoint.failed_ai_exchanges,
        generated_files=_generated_files(store, checkpoint),
        template_matches=match_templates(ir),
        warnings=list(
            dict.fromkeys(
                checkpoint.warnings + checkpoint.manifest_warnings + checkpoint.capability_gaps
            )
        ),
        unsupported_features=list(dict.fromkeys(checkpoint.unsupported_features)),
        validation=validation,
        provenance={
            "input_commit": ir.commit,
            "input_license": ir.license_name,
            "input_format": ir.source_format,
            "feature_scope": ir.feature_scope,
        },
    )


def _install_generated_source(staged: Path, output: Path) -> None:
    """Install a completed staging tree while keeping an old output recoverable."""
    if not output.exists():
        os.replace(staged, output)
        return
    backup = output.parent / f".{output.name}.backup-{uuid.uuid4().hex}"
    os.replace(output, backup)
    try:
        os.replace(staged, output)
    except BaseException:
        os.replace(backup, output)
        raise
    if backup.is_dir() and not backup.is_symlink():
        shutil.rmtree(backup, ignore_errors=True)
    else:
        backup.unlink(missing_ok=True)


def complete_conversion(
    store: CheckpointStore,
    ir: SourceIR,
    checkpoint: ConversionCheckpoint,
    validation: ValidationResult,
) -> ConversionOutcome:
    project = store.project
    output = Path(checkpoint.output)
    report = _report(store, ir, checkpoint, validation)
    write_report(project, report)

    checkpoint.validation = validation
    resumable = report.status is ConversionStatus.FAILED or not validation.contract_ok
    checkpoint.phase = "validated" if resumable else "complete"
    store.commit(checkpoint=checkpoint)
    store.publish_audit(project)

    if resumable:
        return ConversionOutcome(output=project, report=report, source_ir=ir)
    _install_generated_source(project, output)
    shutil.rmtree(store.workspace, ignore_errors=True)
    return ConversionOutcome(output=output, report=report, source_ir=ir)
