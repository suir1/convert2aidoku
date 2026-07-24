from __future__ import annotations

import shutil
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from .ai import AIResult, OpenAICompatibleClient, ai_round
from .analyzer import analyze_source
from .checkpoint_store import CheckpointStore, ManifestWrite
from .config import AISettings
from .conversion_completion import ConversionOutcome, complete_conversion
from .errors import InputError
from .generated_source_metadata import GeneratedSourceMetadata
from .ingest import resolve_source
from .live_validation_evidence import live_validation_evidence
from .manifest_contract import ContractEvaluation, evaluate_manifest_contract
from .models import (
    Capability,
    ConversionCheckpoint,
    ConversionReport,
    GeneratedResources,
    GenerationManifest,
    SourceIR,
    ValidationResult,
)
from .reports import classify_status, write_report
from .scaffold import (
    apply_generation_manifest,
    create_scaffold,
)
from .targeted_repair import TargetedRepair, repair_required
from .validator import validate_project


def _prepare_output(output: Path, *, force: bool) -> None:
    if output.exists():
        if not force:
            raise InputError(f"output already exists: {output}; pass --force to replace it")
        if output.is_symlink():
            raise InputError(f"refusing to replace a symbolic-link output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)


def _workspace_path(output: Path) -> Path:
    return output.parent / f".{output.name}.c2a-work"


@dataclass
class _ConversionRoundRunner:
    ir: SourceIR
    store: CheckpointStore
    project: Path
    checkpoint: ConversionCheckpoint
    query: str | None
    live: bool
    proxy: str | None
    manifest: GenerationManifest = field(init=False)
    contract: ContractEvaluation = field(init=False)
    capability_gaps: list[str] = field(init=False)
    validation: ValidationResult = field(init=False)

    def load(self) -> GenerationManifest:
        if self.checkpoint.current_manifest is None:
            raise InputError("resume checkpoint has no saved generation manifest")
        return self.store.read_manifest(self.checkpoint.current_manifest)

    def _effective(self, manifest: GenerationManifest) -> GenerationManifest:
        """Carry required resources across rounds without altering raw audit manifests."""
        required_paths = set()
        if Capability.FILTERS in self.ir.capabilities:
            required_paths.add("res/filters.json")
        if (
            Capability.SETTINGS in self.ir.capabilities
            or Capability.DYNAMIC_BASE_URLS in self.ir.capabilities
        ):
            required_paths.add("res/settings.json")
        missing = required_paths - {item.path for item in manifest.files}
        inherited = []
        if missing:
            for number in range(len(self.checkpoint.ai_rounds) - 1, 0, -1):
                previous = self.store.read_round(number)
                for item in previous.files:
                    if item.path in missing:
                        inherited.append(item)
                        missing.remove(item.path)
                if not missing:
                    break
        effective = manifest
        if inherited:
            paths = sorted(item.path for item in inherited)
            warning = (
                "repair manifest omitted SourceIR-required resources; preserved from the prior "
                "round: " + ", ".join(paths)
            )
            if warning not in self.checkpoint.warnings:
                self.checkpoint.warnings.append(warning)
            effective = manifest.model_copy(update={"files": manifest.files + inherited})
        setting_overrides = live_validation_evidence(self.ir).setting_overrides
        return GeneratedResources(effective).with_defaults(
            filter_specs=self.ir.filter_specs,
            setting_overrides=setting_overrides,
        )

    def evaluate(self, manifest: GenerationManifest) -> None:
        self.manifest = self._effective(manifest)
        generated_files = apply_generation_manifest(
            self.project, self.ir, self.manifest, query=self.query
        )
        self.contract = evaluate_manifest_contract(self.ir, self.manifest)
        self.capability_gaps = self.contract.messages
        self.validation = validate_project(self.project, live=self.live, proxy=self.proxy)
        self.validation.contract_ok = not self.capability_gaps
        self.checkpoint.generated_files = generated_files
        self.checkpoint.capability_gaps = self.capability_gaps
        self.checkpoint.validation = self.validation
        self.checkpoint.phase = "validated"
        self.store.commit(checkpoint=self.checkpoint)

    def accept(self, result: AIResult[GenerationManifest], *, purpose: str) -> None:
        number = len(self.checkpoint.ai_rounds) + 1
        relative = self.store.round_path(number)
        self.checkpoint.current_manifest = relative
        self.checkpoint.ai_rounds.append(ai_round(number, purpose, result))
        self.checkpoint.warnings.extend(result.warnings)
        self.checkpoint.manifest_warnings = list(result.value.warnings)
        self.checkpoint.unsupported_features.extend(result.value.unsupported_features)
        self.checkpoint.phase = "manifest_saved"
        self.checkpoint.validation = None
        self.store.commit(
            manifest=ManifestWrite(number, result.value),
            checkpoint=self.checkpoint,
        )
        self.evaluate(result.value)

    def repair(self, settings: AISettings) -> None:
        repair_number = max(0, len(self.checkpoint.ai_rounds) - 1)
        if not repair_required(self.validation, self.capability_gaps, live=self.live):
            return
        with OpenAICompatibleClient(settings) as client:
            while (
                repair_required(self.validation, self.capability_gaps, live=self.live)
                and repair_number < settings.max_repair_rounds
            ):
                repair_number += 1
                repair = TargetedRepair(
                    ir=self.ir,
                    store=self.store,
                    checkpoint=self.checkpoint,
                    manifest=self.manifest,
                    validation=self.validation,
                    contract=self.contract,
                )
                self.accept(repair.request(client), purpose="repair")


def _refresh_resume_source_ir(
    ir: SourceIR,
    *,
    input_ref: str,
    store: CheckpointStore,
) -> SourceIR:
    if ir.schema_version >= 2:
        return ir
    with resolve_source(input_ref) as resolved:
        refreshed = analyze_source(resolved)
    if refreshed.metadata.source_id != ir.metadata.source_id:
        raise InputError(
            "refreshed resume input changed source id from "
            f"{ir.metadata.source_id!r} to {refreshed.metadata.source_id!r}"
        )
    store.commit(source_ir=refreshed)
    return refreshed


def _bump_completed_resume_version(store: CheckpointStore, ir: SourceIR) -> SourceIR:
    project = store.project
    try:
        source = GeneratedSourceMetadata.load(project).with_bumped_version(ir.metadata.version)
        version = source.version
        source.write(project)
    except (OSError, ValueError) as exc:
        raise InputError(f"unable to bump installed source version: {exc}") from exc
    bumped = ir.model_copy(
        update={
            "metadata": ir.metadata.model_copy(update={"version": version}),
        }
    )
    store.commit(source_ir=bumped)
    return bumped


def _check_resume_compatibility(
    checkpoint: ConversionCheckpoint,
    *,
    input_ref: str,
    output: Path,
    settings: AISettings,
    query: str | None,
    live: bool,
) -> None:
    mismatches = []
    if checkpoint.input_ref != input_ref:
        mismatches.append("input")
    if checkpoint.output != str(output):
        mismatches.append("output")
    if checkpoint.provider_base_url.rstrip("/") != settings.base_url.rstrip("/"):
        mismatches.append("base URL")
    if checkpoint.model != settings.model:
        mismatches.append("model")
    if checkpoint.query != query:
        mismatches.append("query")
    if checkpoint.live != live:
        mismatches.append("live mode")
    if mismatches:
        raise InputError("resume options do not match the saved run: " + ", ".join(mismatches))


def convert_source(
    input_ref: str,
    *,
    output: Path,
    settings: AISettings,
    query: str | None = None,
    live: bool = True,
    force: bool = False,
    proxy: str | None = None,
    resume: bool = False,
) -> ConversionOutcome:
    output = output.expanduser().absolute()
    if output.exists() and output.is_symlink():
        raise InputError(f"refusing to replace a symbolic-link output: {output}")
    workspace = _workspace_path(output)
    store = CheckpointStore(workspace)
    project = store.project

    if resume:
        store, checkpoint = CheckpointStore.resume(
            workspace,
            installed_output=output,
        )
        _check_resume_compatibility(
            checkpoint,
            input_ref=input_ref,
            output=output,
            settings=settings,
            query=query,
            live=live,
        )
        if not project.is_dir() or project.is_symlink():
            raise InputError(f"resume staging project is missing or unsafe: {project}")
        ir = store.read_source_ir()
        ir = _refresh_resume_source_ir(
            ir,
            input_ref=input_ref,
            store=store,
        )
        if checkpoint.phase == "complete":
            ir = _bump_completed_resume_version(store, ir)
    else:
        _prepare_output(output, force=force)
        if workspace.exists() or workspace.is_symlink():
            raise InputError(
                f"conversion workspace already exists: {workspace}; pass --resume to continue it"
            )
        workspace.mkdir()
        try:
            with resolve_source(input_ref) as resolved:
                ir = analyze_source(resolved)
                create_scaffold(project, ir, resolved)
        except BaseException:
            shutil.rmtree(workspace, ignore_errors=True)
            raise
        checkpoint = ConversionCheckpoint(
            input_ref=input_ref,
            output=str(output),
            provider_base_url=settings.base_url,
            model=settings.model,
            query=query,
            live=live,
            force=force,
            warnings=list(ir.warnings),
            unsupported_features=list(ir.unsupported_features),
        )
        store = CheckpointStore.initialize(
            workspace,
            source_ir=ir,
            checkpoint=checkpoint,
        )

    rounds = _ConversionRoundRunner(
        ir=ir,
        store=store,
        project=project,
        checkpoint=checkpoint,
        query=query,
        live=live,
        proxy=proxy,
    )
    if checkpoint.current_manifest is not None:
        rounds.evaluate(rounds.load())
    else:
        with OpenAICompatibleClient(settings) as client:
            rounds.accept(
                client.generate(ir),
                purpose="generate",
            )

    rounds.repair(settings)
    return complete_conversion(store, ir, checkpoint, rounds.validation)


def validate_existing(
    project: Path,
    *,
    live: bool = True,
    proxy: str | None = None,
) -> ConversionReport:
    project = project.expanduser().resolve()
    if not GeneratedSourceMetadata.exists(project):
        raise InputError(f"not an Aidoku source directory: {project}")
    source_id = GeneratedSourceMetadata.load(project).source_id
    if source_id is None:
        source_id = project.name
    validation: ValidationResult = validate_project(project, live=live, proxy=proxy)
    status = classify_status(validation, live_requested=live)
    report = ConversionReport(
        status=status,
        input_ref=str(project),
        source_id=source_id,
        generated_files=[],
        validation=validation,
    )
    existing_report = project / "report.json"
    if existing_report.is_file():
        with suppress(OSError, ValueError):
            report = ConversionReport.model_validate_json(
                existing_report.read_text(encoding="utf-8")
            ).model_copy(
                update={
                    "status": status,
                    "validation": validation,
                }
            )
    write_report(project, report)
    return report
