from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .decompiled_input import project_java_behavior
from .errors import InputError
from .models import SourceFile, SourceIR

DEFAULT_GENERATION_EVIDENCE_CHARS = 110_000


@dataclass(frozen=True)
class GenerationContext:
    source_ir: dict[str, Any]
    source_evidence: list[dict[str, Any]]
    omitted_source_files: list[dict[str, Any]]
    context_stats: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {
            "source_ir": self.source_ir,
            "source_evidence": self.source_evidence,
            "omitted_source_files": self.omitted_source_files,
            "context_stats": self.context_stats,
        }


def _evidence_priority(ir: SourceIR, source: SourceFile) -> int:
    path = source.path
    stem = PurePosixPath(path).stem
    if stem == ir.main_class:
        return 1_000
    if "/api/" in f"/{path}" and "/dto/" not in f"/{path}":
        return 900
    if "/api/dto/" in f"/{path}":
        return 850
    if "/interceptor/" in f"/{path}":
        return 800
    if any(marker in stem for marker in ("Filter", "Option", "Preference")):
        return 750
    return 500


def _source_evidence(ir: SourceIR, source: SourceFile) -> dict[str, Any]:
    content = source.content
    representation = "complete"
    if source.path.endswith(".java") and "/api/dto/" not in f"/{source.path}":
        main = PurePosixPath(source.path).stem == ir.main_class
        sliced = project_java_behavior(
            content,
            main=main,
            public_only=ir.feature_scope == "public_only",
        )
        if sliced != content:
            content = sliced
            representation = "java_behavior_slice"
    return {
        "path": source.path,
        "content": content,
        "sha256": source.sha256,
        "representation": representation,
    }


def build_generation_context(
    ir: SourceIR,
    *,
    max_chars: int = DEFAULT_GENERATION_EVIDENCE_CHARS,
) -> GenerationContext:
    source_ir = ir.model_dump(mode="json", exclude={"files", "license_text"})
    original_chars = sum(len(source.content) for source in ir.files)
    if ir.source_format == "kotlin_module":
        return GenerationContext(
            source_ir=source_ir,
            source_evidence=[source.model_dump(mode="json") for source in ir.files],
            omitted_source_files=[],
            context_stats={
                "mode": "complete_kotlin_source",
                "original_files": len(ir.files),
                "evidence_files": len(ir.files),
                "original_chars": original_chars,
                "evidence_chars": original_chars,
            },
        )

    candidates: list[tuple[int, int, SourceFile, dict[str, Any]]] = []
    omitted: list[dict[str, Any]] = []
    for index, source in enumerate(ir.files):
        if source.path == "resources/AndroidManifest.xml":
            omitted.append(
                {
                    "path": source.path,
                    "sha256": source.sha256,
                    "chars": len(source.content),
                    "reason": "represented_in_source_ir",
                }
            )
            continue
        evidence = _source_evidence(ir, source)
        candidates.append((_evidence_priority(ir, source), index, source, evidence))

    essential = next(
        (item for item in candidates if PurePosixPath(item[2].path).stem == ir.main_class),
        None,
    )
    if essential is None:
        raise InputError(f"generation context has no main source for {ir.main_class}")
    if len(essential[3]["content"]) > max_chars:
        raise InputError(
            f"essential generation evidence exceeds {max_chars:,} characters: {essential[2].path}"
        )

    selected: list[tuple[int, dict[str, Any]]] = []
    total = 0
    for _priority, index, source, evidence in sorted(
        candidates, key=lambda item: (-item[0], item[1])
    ):
        size = len(evidence["content"])
        if total + size <= max_chars:
            selected.append((index, evidence))
            total += size
        else:
            omitted.append(
                {
                    "path": source.path,
                    "sha256": source.sha256,
                    "chars": len(source.content),
                    "reason": "generation_evidence_budget",
                }
            )

    evidence_files = [evidence for _index, evidence in sorted(selected)]
    return GenerationContext(
        source_ir=source_ir,
        source_evidence=evidence_files,
        omitted_source_files=omitted,
        context_stats={
            "mode": "decompiled_behavior_evidence",
            "original_files": len(ir.files),
            "evidence_files": len(evidence_files),
            "omitted_files": len(omitted),
            "original_chars": original_chars,
            "evidence_chars": total,
            "max_evidence_chars": max_chars,
        },
    )
