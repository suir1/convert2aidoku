from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .ai import AIResult, OpenAICompatibleClient
from .checkpoint_store import CheckpointStore
from .constants import MAX_AI_DIAGNOSTIC_CHARS
from .errors import AIProviderError, SecurityError
from .live_validation_evidence import live_validation_evidence
from .manifest_contract import ContractEvaluation
from .models import (
    ConversionCheckpoint,
    GeneratedFile,
    GenerationManifest,
    RepairPatch,
    SourceIR,
    ValidationResult,
)
from .rust_inspection import RustInspection
from .scaffold import (
    normalize_pinned_aidoku_rust,
    read_generated_files,
    validate_generated_content,
)

_RUST_DIAGNOSTIC_LOCATION = re.compile(
    r"-->\s+(?P<path>src/[A-Za-z0-9_./-]+\.rs):(?P<line>[1-9][0-9]*):[1-9][0-9]*"
)
_RUST_DIAGNOSTIC_NAMED_TYPE = re.compile(
    r"^\s*[1-9][0-9]*\s*\|\s*(?:pub(?:\([^)]*\))?\s+)?"
    r"(?:struct|enum|union)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
    re.MULTILINE,
)


def diagnostic_file_excerpts(
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
    named_types = set(_RUST_DIAGNOSTIC_NAMED_TYPE.findall(diagnostics))
    for relative, line_numbers in sorted(locations.items()):
        path = project / relative
        if not path.is_file() or path.is_symlink():
            continue
        resolved = path.resolve()
        project_resolved = project.resolve()
        if resolved != project_resolved and project_resolved not in resolved.parents:
            continue
        lines = _remove_generated_smoke(path.read_text(encoding="utf-8")).splitlines()
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
        if named_types:
            inspection = RustInspection.from_content("\n".join(lines))
            for name in sorted(named_types):
                struct = inspection.struct_named(name)
                if struct is None:
                    continue
                excerpts.append(
                    {
                        "path": relative,
                        "start_line": struct.node.start_point[0] + 1,
                        "end_line": struct.node.end_point[0] + 1,
                        "content": struct.text,
                    }
                )
    unique = {
        (str(item["path"]), int(item["start_line"]), int(item["end_line"])): item
        for item in excerpts
    }
    return [unique[key] for key in sorted(unique)][:12]


def _remove_generated_smoke(content: str) -> str:
    return content.replace("\n#[cfg(test)]\nmod generated_smoke;\n", "\n")


def apply_repair_patch(
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
            content = normalize_pinned_aidoku_rust(
                content,
                allow_dead_code=path != "src/lib.rs",
            )
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


def repair_required(
    validation: ValidationResult,
    capability_gaps: list[str],
    *,
    live: bool,
) -> bool:
    if any(stage.kind.value == "toolchain" for stage in validation.stages):
        return False
    if capability_gaps:
        return True
    if validation.blocked:
        return False
    return not (validation.build_ok and validation.package_ok and (validation.live_ok or not live))


def repair_state_signature(
    validation: ValidationResult,
    capability_gaps: list[str],
) -> str:
    """Identify repeated repair states while ignoring unstable source locations."""

    def normalize(output: str) -> str:
        output = re.sub(r"(?<=\.rs):[1-9][0-9]*:[1-9][0-9]*", ":LINE:COL", output)
        output = re.sub(r"^\s*[1-9][0-9]*\s*\|", "LINE |", output, flags=re.MULTILINE)
        return " ".join(output.split())

    payload = {
        "failed_stages": [
            {
                "name": stage.name,
                "kind": stage.kind.value,
                "blocked": stage.blocked,
                "output": normalize(stage.output),
            }
            for stage in validation.stages
            if not stage.ok
        ],
        "capability_gaps": capability_gaps,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def repair_diagnostics(
    ir: SourceIR,
    validation: ValidationResult,
    capability_gaps: list[str],
) -> str:
    parts = [validation.diagnostics]
    evidence = live_validation_evidence(ir)
    if evidence.repair_context:
        parts.append(evidence.repair_context)
    if capability_gaps:
        parts.append("Generated capability/contract gaps:\n- " + "\n- ".join(capability_gaps))
    return "\n\n".join(part for part in parts if part)


@dataclass(frozen=True)
class _PatchRequest:
    scope: Literal["compiler", "contract"]
    excerpts: list[dict[str, object]]
    diagnostics: str
    fallback_label: str


@dataclass(frozen=True)
class TargetedRepair:
    ir: SourceIR
    store: CheckpointStore
    checkpoint: ConversionCheckpoint
    manifest: GenerationManifest
    validation: ValidationResult
    contract: ContractEvaluation

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

    def _patch_request(self, diagnostics: str) -> _PatchRequest | None:
        contract_repair = self.contract.repair(self.store.project)
        if contract_repair is not None:
            return _PatchRequest(
                "contract",
                contract_repair.excerpts,
                contract_repair.diagnostics,
                "contract",
            )
        excerpts = diagnostic_file_excerpts(self.store.project, diagnostics)
        failed_stages = {stage.name for stage in self.validation.stages if not stage.ok}
        if excerpts and failed_stages and failed_stages <= {"cargo-check", "clippy", "clippy-fix"}:
            return _PatchRequest("compiler", excerpts, diagnostics, "targeted")
        return None

    def request(self, client: OpenAICompatibleClient) -> AIResult[GenerationManifest]:
        current_files = read_generated_files(self.store.project)
        diagnostics = self.validation.diagnostics[-MAX_AI_DIAGNOSTIC_CHARS:]
        patch_request = self._patch_request(diagnostics)
        fallback_warning = None
        if patch_request is not None:
            try:
                patch_result = client.repair_patch(
                    self.ir,
                    current_file_excerpts=patch_request.excerpts,
                    diagnostics=patch_request.diagnostics,
                    scope=patch_request.scope,
                )
                patched_manifest = apply_repair_patch(
                    self.manifest,
                    current_files,
                    patch_result.value,
                    patch_request.excerpts,
                )
                return patch_result.with_value(patched_manifest)
            except AIProviderError as exc:
                fallback_warning = f"{patch_request.fallback_label} patch fallback: {exc}"

        diagnostics = repair_diagnostics(
            self.ir,
            self.validation,
            self.contract.messages,
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
