from __future__ import annotations

import re

from .ingest import ResolvedSource


def match(pattern: str, text: str, default: str = "") -> str:
    result = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    return result.group(1).strip() if result else default


def input_license(resolved: ResolvedSource) -> tuple[str | None, str | None]:
    if resolved.license_path is None:
        return None, None
    return (
        resolved.license_path.name,
        resolved.license_path.read_text(encoding="utf-8", errors="replace"),
    )
