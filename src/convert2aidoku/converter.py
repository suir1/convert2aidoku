from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from .ai import AIResult, OpenAICompatibleClient, ai_round
from .checkpoint_store import CheckpointStore, ManifestWrite
from .config import AISettings
from .conversion_completion import ConversionOutcome, complete_conversion
from .conversion_intake import ConversionIntake
from .errors import AIProviderError, InputError
from .generated_source_metadata import GeneratedSourceMetadata
from .kotlin_settings import with_kotlin_settings
from .listing_renderer import with_deterministic_search_listing
from .live_validation_evidence import live_validation_evidence
from .manifest_contract import (
    ContractEvaluation,
    evaluate_manifest_contract,
    normalize_decompiled_dto_manifest,
    normalize_decompiled_setting_manifest,
)
from .models import (
    AIFailedExchange,
    AIUsage,
    Capability,
    ConversionCheckpoint,
    ConversionReport,
    GeneratedResources,
    GenerationManifest,
    SourceIR,
    ValidationResult,
)
from .reports import classify_status, write_report
from .scaffold import apply_generation_manifest, normalize_generation_manifest
from .targeted_repair import (
    TargetedRepair,
    repair_required,
    repair_round_limit,
    repair_state_signature,
)
from .validator import validate_project


@dataclass
class _ConversionRoundRunner:
    ir: SourceIR
    store: CheckpointStore
    project: Path
    checkpoint: ConversionCheckpoint
    query: str | None
    live: bool
    proxy: str | None
    progress: Callable[[str], None]
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
        effective = normalize_decompiled_dto_manifest(self.ir, effective)
        effective = normalize_decompiled_setting_manifest(self.ir, effective)
        effective = with_kotlin_settings(self.ir, effective)
        effective = GeneratedResources(effective).with_source_filters(self.ir.filter_specs)
        setting_overrides = live_validation_evidence(self.ir).setting_overrides
        effective = GeneratedResources(effective).with_defaults(
            filter_specs=self.ir.filter_specs,
            setting_overrides=setting_overrides,
        )
        effective = with_deterministic_search_listing(self.ir, effective)
        return normalize_generation_manifest(self.ir, effective)

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
        round_number = len(self.checkpoint.ai_rounds)
        if repair_required(self.validation, self.capability_gaps, live=self.live):
            failed = next(
                (stage.name for stage in self.validation.stages if not stage.ok),
                "contract",
            )
            self.progress(f"AI round {round_number} validation failed at {failed}")
        else:
            self.progress(f"AI round {round_number} validation passed")

    def accept(self, result: AIResult[GenerationManifest], *, purpose: str) -> None:
        number = len(self.checkpoint.ai_rounds) + 1
        relative = self.store.round_path(number)
        self.checkpoint.current_manifest = relative
        self.checkpoint.ai_rounds.append(ai_round(number, purpose, result))
        self.checkpoint.warnings.extend(result.warnings)
        self.checkpoint.manifest_warnings = list(result.value.warnings)
        self.checkpoint.phase = "manifest_saved"
        self.checkpoint.validation = None
        self.store.commit(
            manifest=ManifestWrite(number, result.value),
            checkpoint=self.checkpoint,
        )
        total_tokens = result.usage.total_tokens if result.usage is not None else None
        usage = f" ({total_tokens:,} tokens)" if total_tokens is not None else ""
        self.progress(f"AI round {number} returned{usage}; validating")
        self.evaluate(result.value)

    def record_failed_exchange(self, exc: AIProviderError, *, purpose: str) -> None:
        diagnostics = list(dict.fromkeys(exc.warnings or [str(exc)]))
        diagnostics = [item[-2_000:] for item in diagnostics[-2:]]
        usage = exc.usage if isinstance(exc.usage, AIUsage) else None
        self.checkpoint.failed_ai_exchanges.append(
            AIFailedExchange(
                purpose=purpose,
                usage=usage,
                diagnostics=diagnostics,
            )
        )
        self.store.commit(checkpoint=self.checkpoint)

    def repair(self, settings: AISettings) -> None:
        repair_number = max(0, len(self.checkpoint.ai_rounds) - 1)
        if self.validation.blocked:
            warning = (
                "AI repair skipped because live validation is blocked by the external network; "
                "resume the saved checkpoint after connectivity changes"
            )
            if warning not in self.checkpoint.warnings:
                self.checkpoint.warnings.append(warning)
                self.store.commit(checkpoint=self.checkpoint)
            self.progress("AI repair skipped: live validation is externally blocked")
            return
        if not repair_required(self.validation, self.capability_gaps, live=self.live):
            return
        with OpenAICompatibleClient(settings) as client:
            while repair_required(self.validation, self.capability_gaps, live=self.live):
                round_limit = repair_round_limit(
                    self.validation,
                    self.capability_gaps,
                    live=self.live,
                    configured_limit=settings.max_repair_rounds,
                )
                if repair_number >= round_limit:
                    break
                signature = repair_state_signature(self.validation, self.capability_gaps)
                if self.checkpoint.repair_attempt_signatures.count(signature) >= 2:
                    warning = "repair stopped after two attempts with an unchanged validation state"
                    if warning not in self.checkpoint.warnings:
                        self.checkpoint.warnings.append(warning)
                    self.store.commit(checkpoint=self.checkpoint)
                    self.progress("Repair stopped: unchanged validation state repeated twice")
                    break
                self.checkpoint.repair_attempt_signatures.append(signature)
                self.store.commit(checkpoint=self.checkpoint)
                repair_number += 1
                self.progress(f"Requesting AI repair {repair_number}/{round_limit}")
                repair = TargetedRepair(
                    ir=self.ir,
                    store=self.store,
                    checkpoint=self.checkpoint,
                    manifest=self.manifest,
                    validation=self.validation,
                    contract=self.contract,
                )
                try:
                    result = repair.request(client)
                except AIProviderError as exc:
                    self.record_failed_exchange(exc, purpose="repair")
                    warning = "AI repair failed; checkpoint retained for --resume"
                    if warning not in self.checkpoint.warnings:
                        self.checkpoint.warnings.append(warning)
                        self.store.commit(checkpoint=self.checkpoint)
                    self.progress(warning)
                    break
                self.accept(result, purpose="repair")


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
    progress: Callable[[str], None] | None = None,
) -> ConversionOutcome:
    notify = progress or (lambda _message: None)
    intake = ConversionIntake.prepare(
        input_ref,
        output=output,
        settings=settings,
        query=query,
        live=live,
        force=force,
        resume=resume,
    )
    store = intake.store
    project = intake.project
    ir = intake.source_ir
    checkpoint = intake.checkpoint

    rounds = _ConversionRoundRunner(
        ir=ir,
        store=store,
        project=project,
        checkpoint=checkpoint,
        query=query,
        live=live,
        proxy=proxy,
        progress=notify,
    )
    if checkpoint.current_manifest is not None:
        notify(f"Resuming AI round {len(checkpoint.ai_rounds)} from checkpoint")
        rounds.evaluate(rounds.load())
    else:
        notify("Requesting initial AI generation")
        with OpenAICompatibleClient(settings) as client:
            try:
                result = client.generate(ir)
            except AIProviderError as exc:
                rounds.record_failed_exchange(exc, purpose="generate")
                raise
            rounds.accept(result, purpose="generate")

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
