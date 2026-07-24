from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .ai import AIResult, OpenAICompatibleClient, ai_round
from .analyzer import analyze_source
from .checkpoint_store import CheckpointStore, ManifestWrite
from .config import AISettings
from .constants import MAX_AI_DIAGNOSTIC_CHARS
from .errors import AIProviderError, InputError, SecurityError
from .ingest import resolve_source
from .manifest_contract import ContractEvaluation, evaluate_manifest_contract
from .models import (
    Capability,
    ConversionCheckpoint,
    ConversionReport,
    GeneratedFile,
    GeneratedResources,
    GenerationManifest,
    RepairPatch,
    SourceIR,
    ValidationResult,
)
from .reports import classify_status, write_report
from .scaffold import (
    apply_generation_manifest,
    create_scaffold,
    normalize_pinned_aidoku_rust,
    read_generated_files,
    validate_generated_content,
)
from .templates import match_templates
from .validator import validate_project


@dataclass(frozen=True)
class ConversionOutcome:
    output: Path
    report: ConversionReport
    source_ir: SourceIR


_LIVE_VALIDATION_EVIDENCE = {
    "zh.mycomic": (
        "Independent browser-only benchmark evidence (not proof of runner connectivity): "
        "GET /comics?sort=-views returned manga entries; /comics/54348 returned details and "
        "chapters; /chapters/794527 returned 30 pages. The CLI/test runner may still receive "
        "HTTP 403 because it does not share the browser's network/session. Preserve relative "
        "keys, but make every request URL absolute."
    ),
    "zh.copymanga": (
        "Independent 2026-07-21 public API reachability evidence from the Tachi input only: "
        "the required CopyManga headers are Accept: application/json, Origin: "
        "https://2025copy.com, Version: 2025.11.21, Region: 0, Webp: 0, platform: 1, and a "
        "browser User-Agent. GET mapi.copy20.com/api/v3/comic2/<path> and "
        "mapi.copy2000.site/api/v3/comic2/<path> returned API code 200, while the input's "
        "api.copy3000.com default returned custom HTTP/API code 210 on this network. Keep the "
        "finite input allowlist, but prefer a currently reachable public domain as the default. "
        "Official AidokuRunner differential evidence for generated v83 loaded all seven filters "
        "across the Swift/Postcard boundary. Region, sort, and dynamic theme changed manga keys, "
        "but rank=day and audience=female produced Manga values with empty titles and key "
        "'/comic/' because /ranks returns RankResult.list of ListItem { comic }, not direct "
        "Comic entries. The free_type filter is marked HotManga-only by the input and is expected "
        "not to change results on the default CopyManga domain. The /comic2/<path> detail "
        "endpoint is also wrapped as ApiResponse<ComicDetailResult>: deserialize the outer "
        "ApiResponse<DetailResult> and use .results before reading .comic or .groups. "
        "Deserializing the HTTP response directly into DetailResult silently produces default "
        "empty fields. Official AidokuRunner evidence for clean4 loaded the dynamic theme UI, "
        "but the filter had no effect because get_search_manga_list never read FilterValue id "
        "'theme'. Read that same id and append &theme=<selected path_word> to the /comics "
        "request; a visible filter that does not change its request is incomplete."
    ),
}

# Values here are benchmark observations, not arbitrary URLs. They are applied only when the AI
# already emitted the same value in a finite select-setting allowlist recovered from the input.
_LIVE_VALIDATED_SETTING_DEFAULTS = {
    "zh.copymanga": {"v2.pref.api_domain": "mapi.copy20.com"},
}

_RUST_DIAGNOSTIC_LOCATION = re.compile(
    r"-->\s+(?P<path>src/[A-Za-z0-9_./-]+\.rs):(?P<line>[1-9][0-9]*):[1-9][0-9]*"
)


def _diagnostic_file_excerpts(
    project: Path,
    diagnostics: str,
    *,
    context_lines: int = 10,
) -> list[dict[str, object]]:
    locations: dict[str, set[int]] = {}
    for match in _RUST_DIAGNOSTIC_LOCATION.finditer(diagnostics):
        path = match.group("path")
        if path == "src/generated_smoke.rs" or ".." in Path(path).parts:
            continue
        locations.setdefault(path, set()).add(int(match.group("line")))

    excerpts: list[dict[str, object]] = []
    for relative, line_numbers in sorted(locations.items()):
        path = project / relative
        if not path.is_file() or path.is_symlink():
            continue
        resolved = path.resolve()
        project_resolved = project.resolve()
        if resolved != project_resolved and project_resolved not in resolved.parents:
            continue
        lines = _remove_generated_smoke_for_repair(path.read_text(encoding="utf-8")).splitlines()
        ranges = sorted(
            (max(1, line - context_lines), min(len(lines), line + context_lines))
            for line in line_numbers
        )
        merged: list[list[int]] = []
        for start, end in ranges:
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        for start, end in merged:
            excerpts.append(
                {
                    "path": relative,
                    "start_line": start,
                    "end_line": end,
                    "content": "\n".join(lines[start - 1 : end]),
                }
            )
    return excerpts[:12]


def _remove_generated_smoke_for_repair(content: str) -> str:
    return content.replace("\n#[cfg(test)]\nmod generated_smoke;\n", "\n")


def _apply_repair_patch(
    manifest: GenerationManifest,
    current_files: list[dict[str, str]],
    patch: RepairPatch,
    allowed_excerpts: list[dict[str, object]],
) -> GenerationManifest:
    contents = {item["path"]: item["content"] for item in current_files}
    excerpt_contents: dict[str, list[str]] = {}
    for excerpt in allowed_excerpts:
        path = excerpt.get("path")
        content = excerpt.get("content")
        if isinstance(path, str) and isinstance(content, str):
            excerpt_contents.setdefault(path, []).append(content)
    for edit in patch.edits:
        if not any(edit.old_text in excerpt for excerpt in excerpt_contents.get(edit.path, [])):
            raise AIProviderError(
                f"repair patch old_text was not present in a supplied excerpt: {edit.path}"
            )
        content = contents.get(edit.path)
        if content is None:
            raise AIProviderError(f"repair patch references a missing current file: {edit.path}")
        occurrences = content.count(edit.old_text)
        if occurrences != 1:
            raise AIProviderError(
                f"repair patch old_text must match exactly once in {edit.path}; "
                f"matched {occurrences} times"
            )
        contents[edit.path] = content.replace(edit.old_text, edit.new_text, 1)

    payload = manifest.model_dump(mode="json")
    files = []
    for path, content in sorted(contents.items()):
        if path.endswith(".rs"):
            content = normalize_pinned_aidoku_rust(content)
        try:
            validate_generated_content(path, content)
        except SecurityError as exc:
            raise AIProviderError(f"repair patch failed safety validation: {exc}") from exc
        try:
            generated = GeneratedFile(path=path, content=content)
        except ValueError as exc:
            raise AIProviderError(f"repair patch produced an invalid file: {exc}") from exc
        files.append(generated.model_dump(mode="json"))
    payload["files"] = files
    try:
        return GenerationManifest.model_validate(payload)
    except ValueError as exc:
        raise AIProviderError(f"repair patch produced an invalid manifest: {exc}") from exc


def _needs_toolchain_installation(validation: ValidationResult) -> bool:
    return any(stage.kind.value == "toolchain" for stage in validation.stages)


def _should_repair(
    validation: ValidationResult,
    capability_gaps: list[str],
    *,
    live: bool,
) -> bool:
    if _needs_toolchain_installation(validation):
        return False
    if capability_gaps:
        return True
    if validation.blocked:
        return False
    return not (validation.build_ok and validation.package_ok and (validation.live_ok or not live))


def _repair_diagnostics(
    ir: SourceIR,
    validation: ValidationResult,
    capability_gaps: list[str],
) -> str:
    parts = [validation.diagnostics]
    evidence = _LIVE_VALIDATION_EVIDENCE.get(ir.metadata.source_id)
    if evidence:
        parts.append(evidence)
    if capability_gaps:
        parts.append("Generated capability/contract gaps:\n- " + "\n- ".join(capability_gaps))
    return "\n\n".join(part for part in parts if part)


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

    def _history(self) -> list[dict[str, object]]:
        history: list[dict[str, object]] = []
        for number in range(1, len(self.checkpoint.ai_rounds) + 1):
            manifest = self.store.read_round(number)
            history.append(
                {
                    "round": number,
                    "implemented_traits": manifest.implemented_traits,
                    "dependencies": [
                        item.model_dump(mode="json") for item in manifest.dependencies
                    ],
                    "file_paths": [item.path for item in manifest.files],
                }
            )
        return history

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
        setting_overrides = (
            _LIVE_VALIDATED_SETTING_DEFAULTS.get(self.ir.metadata.source_id)
            if Capability.DYNAMIC_BASE_URLS in self.ir.capabilities
            else None
        )
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

    def request_repair(
        self,
        client: OpenAICompatibleClient,
    ) -> AIResult[GenerationManifest]:
        current_files = read_generated_files(self.project)
        targeted_diagnostics = self.validation.diagnostics[-MAX_AI_DIAGNOSTIC_CHARS:]
        excerpts = _diagnostic_file_excerpts(self.project, targeted_diagnostics)
        contract_repair = self.contract.repair(self.project)
        patch_request = None
        if contract_repair is not None:
            patch_request = (
                "contract",
                contract_repair.excerpts,
                contract_repair.diagnostics,
                "contract",
            )
        else:
            failed_stages = {stage.name for stage in self.validation.stages if not stage.ok}
            if (
                not self.capability_gaps
                and excerpts
                and failed_stages
                and failed_stages <= {"cargo-check", "clippy", "clippy-fix"}
            ):
                patch_request = ("compiler", excerpts, targeted_diagnostics, "targeted")

        fallback_warning = None
        if patch_request is not None:
            scope, patch_excerpts, patch_diagnostics, fallback_label = patch_request
            try:
                patch_result = client.repair_patch(
                    self.ir,
                    current_file_excerpts=patch_excerpts,
                    diagnostics=patch_diagnostics,
                    scope=scope,
                )
                patched_manifest = _apply_repair_patch(
                    self.manifest,
                    current_files,
                    patch_result.value,
                    patch_excerpts,
                )
                return patch_result.with_value(patched_manifest)
            except AIProviderError as exc:
                fallback_warning = f"{fallback_label} patch fallback: {exc}"

        diagnostics = _repair_diagnostics(
            self.ir,
            self.validation,
            self.capability_gaps,
        )[-MAX_AI_DIAGNOSTIC_CHARS:]
        repaired = client.repair(
            self.ir,
            current_files=current_files,
            diagnostics=diagnostics,
            manifest_history=self._history(),
        )
        if fallback_warning:
            repaired.warnings.append(fallback_warning)
        return repaired

    def repair(self, settings: AISettings) -> None:
        repair_number = max(0, len(self.checkpoint.ai_rounds) - 1)
        if not _should_repair(self.validation, self.capability_gaps, live=self.live):
            return
        with OpenAICompatibleClient(settings) as client:
            while (
                _should_repair(self.validation, self.capability_gaps, live=self.live)
                and repair_number < settings.max_repair_rounds
            ):
                repair_number += 1
                self.accept(self.request_repair(client), purpose="repair")


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
    source_path = project / "res" / "source.json"
    try:
        source = json.loads(source_path.read_text(encoding="utf-8"))
        current = int(source["info"]["version"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InputError(f"unable to bump installed source version: {exc}") from exc
    version = max(ir.metadata.version, current + 1)
    source["info"]["version"] = version
    _atomic_write_source_metadata(
        source_path,
        json.dumps(source, ensure_ascii=False, indent="\t") + "\n",
    )
    bumped = ir.model_copy(
        update={
            "metadata": ir.metadata.model_copy(update={"version": version}),
        }
    )
    store.commit(source_ir=bumped)
    return bumped


def _atomic_write_source_metadata(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


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


def _install_output(staged: Path, output: Path) -> None:
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

    template_matches = match_templates(ir)
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
    validation = rounds.validation

    deterministic_files = [
        ".cargo/config.toml",
        "Cargo.toml",
        "res/source.json",
        "res/icon.png",
        "PROVENANCE.md",
        "report.json",
        "report.md",
    ]
    if (project / "LICENSE.input").is_file():
        deterministic_files.append("LICENSE.input")
    if (project / "package.aix").is_file():
        deterministic_files.append("package.aix")
    audit_files = store.audit_files()
    report = ConversionReport(
        status=classify_status(validation, live_requested=live),
        input_ref=input_ref,
        source_id=ir.metadata.source_id,
        provider_base_url=settings.base_url,
        model=settings.model,
        ai_rounds=checkpoint.ai_rounds,
        generated_files=sorted(set(checkpoint.generated_files + deterministic_files + audit_files)),
        template_matches=template_matches,
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
    write_report(project, report)
    checkpoint.validation = validation
    resumable = report.status.value == "failed" or not validation.contract_ok
    checkpoint.phase = "validated" if resumable else "complete"
    store.commit(checkpoint=checkpoint)
    store.publish_audit(project)

    if resumable:
        return ConversionOutcome(output=project, report=report, source_ir=ir)
    _install_output(project, output)
    shutil.rmtree(workspace, ignore_errors=True)
    return ConversionOutcome(output=output, report=report, source_ir=ir)


def validate_existing(
    project: Path,
    *,
    live: bool = True,
    proxy: str | None = None,
) -> ConversionReport:
    project = project.expanduser().resolve()
    source_json = project / "res" / "source.json"
    if not source_json.is_file():
        raise InputError(f"not an Aidoku source directory: {project}")
    data = json.loads(source_json.read_text(encoding="utf-8"))
    source_id = str(data.get("info", {}).get("id", project.name))
    validation: ValidationResult = validate_project(project, live=live, proxy=proxy)
    existing_report = project / "report.json"
    if existing_report.is_file():
        try:
            report = ConversionReport.model_validate_json(
                existing_report.read_text(encoding="utf-8")
            ).model_copy(
                update={
                    "status": classify_status(validation, live_requested=live),
                    "validation": validation,
                }
            )
        except (OSError, ValueError):
            report = ConversionReport(
                status=classify_status(validation, live_requested=live),
                input_ref=str(project),
                source_id=source_id,
                generated_files=[],
                validation=validation,
            )
    else:
        report = ConversionReport(
            status=classify_status(validation, live_requested=live),
            input_ref=str(project),
            source_id=source_id,
            generated_files=[],
            validation=validation,
        )
    write_report(project, report)
    return report
