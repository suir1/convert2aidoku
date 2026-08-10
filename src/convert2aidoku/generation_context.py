from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .decompiled_input import decompiled_dto_shapes, project_java_behavior
from .errors import InputError
from .listing_renderer import search_listing_ownership
from .manga_detail_renderer import manga_update_ownership
from .models import Capability, SourceFile, SourceIR
from .page_renderer import page_list_ownership

DEFAULT_GENERATION_EVIDENCE_CHARS = 110_000
DEFAULT_SETTINGS_EVIDENCE_CHARS = 50_000
_SETTING_DECLARATION = re.compile(
    r"Preference|SharedPreferences|setupPreferenceScreen|initPreferences|"
    r"\b(?:KEY|DEFAULT|ENTRIES|ENTRY_KEYS)\b|defaults_get|settings\.json",
    re.IGNORECASE,
)
_SETTING_USAGE = re.compile(
    r"setupPreferenceScreen|initPreferences|settings\.json",
    re.IGNORECASE,
)
_SETTING_ACCESS = re.compile(
    r"\.(?:getBoolean|getString|setKey|setTitle|setSummary|setEntries|"
    r"setEntryValues|setDefaultValue)\s*\("
)
_SETTING_UI_METHOD = re.compile(r"\b(?:initPreferences|setupPreferenceScreen)\b")
_SETTING_UI_STATEMENT = re.compile(
    r"\bnew\s+(?:ListPreference|MultiSelectListPreference|SwitchPreferenceCompat|"
    r"EditTextPreference|PreferenceCategory|PreferenceScreen)\b|"
    r"\.(?:setKey|setTitle|setSummary|setEntries|setEntryValues|setDefaultValue)\s*\(|"
    r"\b(?:Preference|ListPreference|MultiSelectListPreference|SwitchPreferenceCompat|"
    r"EditTextPreference)\s*\[\s*\]|"
    r"\breturn\s+(?:new\s+Preference\s*\[|preferenceArr\b)"
)


@dataclass(frozen=True)
class GenerationContext:
    source_ir: dict[str, Any]
    source_evidence: list[dict[str, Any]]
    decompiled_dto_shapes: list[str]
    omitted_source_files: list[dict[str, Any]]
    context_stats: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {
            "source_ir": self.source_ir,
            "source_evidence": self.source_evidence,
            "decompiled_dto_shapes": self.decompiled_dto_shapes,
            "omitted_source_files": self.omitted_source_files,
            "context_stats": self.context_stats,
        }

    def as_prompt_payload(self) -> dict[str, Any]:
        """Drop audit-only hashes and per-file omission details from provider input."""
        omission_counts = Counter(item["reason"] for item in self.omitted_source_files)
        return {
            "source_ir": self.source_ir,
            "source_evidence": [
                {key: value for key, value in item.items() if key != "sha256"}
                for item in self.source_evidence
            ],
            "decompiled_dto_shapes": self.decompiled_dto_shapes,
            "omitted_source_summary": dict(sorted(omission_counts.items())),
            "context_stats": self.context_stats,
        }


def source_ir_prompt_payload(ir: SourceIR) -> dict[str, Any]:
    """Return source facts needed by the model without local-only audit data."""
    return ir.model_dump(
        mode="json",
        exclude={"files", "license_text", "analysis_rule_ids"},
    )


def _evidence_priority(ir: SourceIR, source: SourceFile) -> int:
    path = source.path
    stem = PurePosixPath(path).stem
    if stem == ir.main_class:
        return 1_000
    if "/api/" in f"/{path}" and "/dto/" not in f"/{path}":
        return 900
    if "/api/dto/" in f"/{path}" or "C2A compacted JADX DTO" in source.content:
        return 850
    if "/interceptor/" in f"/{path}":
        return 800
    if any(marker in stem for marker in ("Filter", "Option", "Preference")):
        return 750
    return 500


def _source_evidence(
    ir: SourceIR,
    source: SourceFile,
    *,
    excluded_methods: frozenset[str],
    excluded_method_prefixes: tuple[str, ...],
) -> dict[str, Any]:
    content = source.content
    representation = "complete"
    if source.path.endswith(".java") and "/api/dto/" not in f"/{source.path}":
        main = PurePosixPath(source.path).stem == ir.main_class
        sliced = project_java_behavior(
            content,
            main=main,
            public_only=ir.feature_scope == "public_only",
            excluded_methods=excluded_methods,
            excluded_method_prefixes=(
                *excluded_method_prefixes,
                *(
                    ("initPreferences", "setupPreferenceScreen")
                    if Capability.SETTINGS in ir.capabilities
                    else ()
                ),
            ),
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
    source_ir = source_ir_prompt_payload(ir)
    original_chars = sum(len(source.content) for source in ir.files)
    if ir.source_format == "kotlin_module":
        return GenerationContext(
            source_ir=source_ir,
            source_evidence=[source.model_dump(mode="json") for source in ir.files],
            decompiled_dto_shapes=[],
            omitted_source_files=[],
            context_stats={
                "mode": "complete_kotlin_source",
                "original_files": len(ir.files),
                "evidence_files": len(ir.files),
                "original_chars": original_chars,
                "evidence_chars": original_chars,
            },
        )

    listing_ownership = search_listing_ownership(ir)
    update_ownership = manga_update_ownership(ir)
    page_ownership = page_list_ownership(ir)
    listing_dto_types = (
        listing_ownership.dto_types if listing_ownership is not None else frozenset()
    )
    update_dto_types = update_ownership.dto_types if update_ownership is not None else frozenset()
    page_dto_types = page_ownership.dto_types if page_ownership is not None else frozenset()
    page_source_stems = page_ownership.source_stems if page_ownership is not None else frozenset()
    owned_dto_types = listing_dto_types | update_dto_types | page_dto_types
    excluded_methods = frozenset(
        {
            *(listing_ownership.java_methods if listing_ownership is not None else ()),
            *(update_ownership.java_methods if update_ownership is not None else ()),
            *(page_ownership.java_methods if page_ownership is not None else ()),
        }
    )
    excluded_method_prefixes = tuple(
        dict.fromkeys(
            [
                *(update_ownership.java_method_prefixes if update_ownership is not None else ()),
                *(page_ownership.java_method_prefixes if page_ownership is not None else ()),
            ]
        )
    )
    dto_shapes = [
        shape.render()
        for shape in decompiled_dto_shapes(ir.files)
        if shape.name not in owned_dto_types
    ]
    candidates: list[tuple[int, int, SourceFile, dict[str, Any]]] = []
    omitted: list[dict[str, Any]] = []
    for index, source in enumerate(ir.files):
        if PurePosixPath(source.path).stem in page_source_stems:
            omitted.append(
                {
                    "path": source.path,
                    "sha256": source.sha256,
                    "chars": len(source.content),
                    "reason": "represented_in_deterministic_page_list",
                }
            )
            continue
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
        if (
            "C2A compacted JADX DTO" in source.content
            and PurePosixPath(source.path).stem in owned_dto_types
        ):
            omitted.append(
                {
                    "path": source.path,
                    "sha256": source.sha256,
                    "chars": len(source.content),
                    "reason": (
                        "represented_in_deterministic_search_listing"
                        if PurePosixPath(source.path).stem in listing_dto_types
                        else (
                            "represented_in_deterministic_manga_update"
                            if PurePosixPath(source.path).stem in update_dto_types
                            else "represented_in_deterministic_page_list"
                        )
                    ),
                }
            )
            continue
        if (
            "// C2A compacted JADX DTO:" in source.content
            and "// Source-specific behavior:" not in source.content
        ):
            omitted.append(
                {
                    "path": source.path,
                    "sha256": source.sha256,
                    "chars": len(source.content),
                    "reason": "represented_in_decompiled_dto_shapes",
                }
            )
            continue
        evidence = _source_evidence(
            ir,
            source,
            excluded_methods=excluded_methods,
            excluded_method_prefixes=excluded_method_prefixes,
        )
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
        decompiled_dto_shapes=dto_shapes,
        omitted_source_files=omitted,
        context_stats={
            "mode": "decompiled_behavior_evidence",
            "original_files": len(ir.files),
            "evidence_files": len(evidence_files),
            "omitted_files": len(omitted),
            "original_chars": original_chars,
            "evidence_chars": total,
            "max_evidence_chars": max_chars,
            "dto_shapes": len(dto_shapes),
            "deterministic_search_listing_dto_shapes": len(listing_dto_types),
            "deterministic_manga_update_dto_shapes": len(update_dto_types),
            "deterministic_page_list_dto_shapes": len(page_dto_types),
        },
    )


def _settings_excerpt(
    content: str,
    *,
    pattern: re.Pattern[str] = _SETTING_DECLARATION,
    context_lines: int = 24,
) -> str:
    lines = content.splitlines()
    ranges: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        if pattern.search(line):
            ranges.append(
                (max(0, index - context_lines), min(len(lines), index + context_lines + 1))
            )
    merged: list[list[int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return "\n\n".join("\n".join(lines[start:end]) for start, end in merged)


def _settings_declaration_slice(content: str) -> str:
    """Keep option values and preference constants without decompiled method noise."""
    lines = content.splitlines()
    selected: list[str] = []
    in_option_constants = False
    saw_class = False
    for line in lines:
        stripped = line.strip()
        if not saw_class and re.search(r"\b(?:class|enum)\s+[A-Za-z_][A-Za-z0-9_]*", line):
            selected.append(stripped)
            saw_class = True
            continue
        if (
            saw_class
            and not in_option_constants
            and re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*\(", stripped)
        ):
            in_option_constants = True
        if in_option_constants:
            selected.append(stripped)
            if stripped.endswith(";"):
                in_option_constants = False
            continue
        if re.search(
            r"\bpublic\s+static\s+final\s+String\s+[A-Z][A-Z0-9_]*\b|"
            r"\bpublic\s+static\s+final\s+[A-Za-z_]\w*\s+[A-Z][A-Z0-9_]*\s*=\s*"
            r"new\s+[A-Za-z_]\w*\s*\(|"
            r"\b(?:public|private|protected)\s+static\s+final\b[^;=\n]*\b"
            r"(?:KEY|DEFAULT|SUMMARY|SUMMERY|ENTRIES|ENTRY_KEYS|[A-Z0-9_]+_KEY)\b",
            line,
        ):
            selected.append(stripped)
    return "\n".join(dict.fromkeys(line for line in selected if line))


def _settings_accessor_slice(content: str) -> str:
    """Keep complete decompiled preference accessors and builders, not adjacent business code."""
    lines = content.splitlines()
    ranges: list[tuple[int, int]] = []
    declaration = re.compile(
        r"\b(?:public|protected|private)\s+(?:static\s+)?(?:final\s+)?"
        r"[A-Za-z0-9_.$<>?, \[\]]+\s+[A-Za-z_]\w*\s*\([^;]*\)\s*\{"
    )
    for index, line in enumerate(lines):
        if _SETTING_ACCESS.search(line) is None:
            continue
        start = next(
            (
                candidate
                for candidate in range(index, -1, -1)
                if declaration.search(lines[candidate])
            ),
            index,
        )
        depth = 0
        saw_opening = False
        end = start + 1
        for candidate in range(start, len(lines)):
            depth += lines[candidate].count("{") - lines[candidate].count("}")
            saw_opening |= "{" in lines[candidate]
            end = candidate + 1
            if saw_opening and depth <= 0:
                break
        ranges.append((start, end))
    merged: list[list[int]] = []
    for start, end in sorted(set(ranges)):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    excerpts = []
    for start, end in merged:
        block = lines[start:end]
        if block and _SETTING_UI_METHOD.search(block[0]):
            compact = [block[0]]
            compact.extend(line for line in block[1:] if _SETTING_UI_STATEMENT.search(line))
            compact.append("}")
            excerpts.append("\n".join(compact))
        else:
            excerpts.append("\n".join(block))
    return "\n\n".join(excerpts)


def _settings_usage_slice(content: str) -> str:
    lines = content.splitlines()
    ranges: list[tuple[int, int]] = []
    for start, line in enumerate(lines):
        if _SETTING_USAGE.search(line) is None:
            continue
        depth = 0
        saw_opening = False
        end = start + 1
        for index in range(start, len(lines)):
            depth += lines[index].count("{") - lines[index].count("}")
            saw_opening |= "{" in lines[index]
            end = index + 1
            if saw_opening and depth <= 0:
                break
        if not saw_opening:
            end = min(len(lines), start + 2)
        ranges.append((start, end))
    merged: list[list[int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return "\n\n".join("\n".join(lines[start:end]) for start, end in merged)


def build_settings_context(
    ir: SourceIR,
    *,
    max_chars: int = DEFAULT_SETTINGS_EVIDENCE_CHARS,
) -> dict[str, Any]:
    """Project only setting declarations/defaults into a bounded resource-generation prompt."""
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for index, source in enumerate(ir.files):
        stem = PurePosixPath(source.path).stem
        path_relevant = any(marker in stem for marker in ("Option", "Preference", "Setting"))
        if not path_relevant and _SETTING_USAGE.search(source.content) is None:
            continue
        if path_relevant:
            excerpt = "\n\n".join(
                part
                for part in (
                    _settings_declaration_slice(source.content),
                    _settings_accessor_slice(source.content),
                )
                if part
            )
        else:
            excerpt = _settings_usage_slice(source.content)
        if not excerpt:
            continue
        candidates.append(
            (
                1_000 if path_relevant else 500,
                index,
                {
                    "path": source.path,
                    "content": excerpt,
                    "sha256": source.sha256,
                    "representation": "settings_evidence_slice",
                },
            )
        )
    selected: list[dict[str, Any]] = []
    total = 0
    for _priority, _index, evidence in sorted(candidates, key=lambda item: (-item[0], item[1])):
        size = len(evidence["content"])
        if total + size > max_chars:
            continue
        selected.append(evidence)
        total += size
    return {
        "source": source_ir_prompt_payload(ir),
        "settings_evidence": selected,
        "context_stats": {
            "evidence_files": len(selected),
            "evidence_chars": total,
            "max_evidence_chars": max_chars,
        },
    }
