from __future__ import annotations

from collections.abc import Callable

from .decompiled_analysis import analyze_decompiled_source
from .ingest import ResolvedSource, resolve_source
from .kotlin_analysis import _uses_relative_url_keys, analyze_kotlin_source
from .models import SourceIR

__all__ = ["_uses_relative_url_keys", "analyze_path", "analyze_source"]

_ANALYZERS: dict[str, Callable[[ResolvedSource], SourceIR]] = {
    "kotlin_module": analyze_kotlin_source,
    "decompiled_apk": analyze_decompiled_source,
}


def analyze_source(resolved: ResolvedSource) -> SourceIR:
    return _ANALYZERS[resolved.source_format](resolved)


def analyze_path(input_ref: str) -> SourceIR:
    with resolve_source(input_ref) as resolved:
        return analyze_source(resolved)
