from __future__ import annotations

import re
from typing import Any

from .constants import MAX_GENERATED_FILE_CHARS
from .errors import SecurityError
from .rust_inspection import RustInspection
from .rust_inspection import first_rust_identifier as _first_rust_identifier
from .rust_inspection import last_rust_identifier as _last_rust_identifier
from .rust_inspection import rust_identifier as _rust_identifier

_FORBIDDEN_GENERATED_TOKENS = (
    "unsafe",
    'extern "C"',
    "Command::",
    "generated_smoke",
    "#[cfg(test)]",
    "#[test]",
)

_SMOKE_MARKER = "#[cfg(test)]\nmod generated_smoke;"
_FORBIDDEN_GENERATED_MACRO = re.compile(
    r"\b(?:env|option_env|include|include_str|include_bytes)\s*!\s*\(",
)
_FORBIDDEN_GENERATED_RUST = re.compile(r"\bextern\s+crate\s+std\b")
_FORBIDDEN_RUST_MACROS = {
    "env",
    "include",
    "include_bytes",
    "include_str",
    "option_env",
}
_FORBIDDEN_RUST_ATTRIBUTES = {"cfg", "cfg_attr", "path", "test"}


def _is_aidoku_imports_std(node: Any) -> bool:
    relative_import = False
    current = node.parent
    while current is not None:
        compact = RustInspection.compact_node(current)
        if current.type == "scoped_identifier":
            if compact.startswith("aidoku::imports::std"):
                return True
            if compact.startswith("imports::std"):
                relative_import = True
        elif current.type == "scoped_use_list":
            if compact.startswith("aidoku::imports::{"):
                return True
            if compact.startswith("imports::{"):
                relative_import = True
            elif compact.startswith("aidoku::{"):
                return relative_import
        elif current.type in {"use_declaration", "source_file"}:
            break
        current = current.parent
    return False


def _rust_line_excerpt(content: str, node: Any) -> str:
    row = node.start_point.row
    lines = content.splitlines()
    if row >= len(lines):
        return "std"
    line = lines[row].strip()
    return line if len(line) <= 160 else line[:157] + "..."


def _validate_generated_rust_ast(path: str, content: str) -> None:
    """Reject compile-time I/O and ways to bypass the tool-owned smoke tests."""
    inspection = RustInspection.from_content(content)
    for node in inspection.nodes():
        identifier = _rust_identifier(node)
        if identifier == "std" and not _is_aidoku_imports_std(node):
            excerpt = _rust_line_excerpt(content, node)
            raise SecurityError(f"generated Rust uses std, which is forbidden: {path} ({excerpt})")
        if identifier == "generated_smoke":
            raise SecurityError(
                f"generated Rust references forbidden reserved module generated_smoke: {path}"
            )

        if node.type == "macro_invocation":
            name = _last_rust_identifier(node.child_by_field_name("macro"))
            if name in _FORBIDDEN_RUST_MACROS:
                raise SecurityError(
                    f"generated Rust uses forbidden environment/file macro {name}: {path}"
                )

        if node.type in {"attribute_item", "inner_attribute_item"}:
            name = _first_rust_identifier(node.named_child(0))
            if name in _FORBIDDEN_RUST_ATTRIBUTES:
                raise SecurityError(f"generated Rust uses forbidden attribute {name}: {path}")

        # This catches unsafe blocks as well as unsafe fn/trait/impl keywords,
        # including variants separated from adjacent tokens by comments.
        if node.type in {"unsafe", "unsafe_block"}:
            raise SecurityError(f"generated Rust uses forbidden unsafe code: {path}")


def validate_generated_content(path: str, content: str) -> None:
    if len(content) > MAX_GENERATED_FILE_CHARS:
        raise SecurityError(f"generated file is too large: {path}")
    if path.endswith(".rs"):
        _validate_generated_rust_ast(path, content)
        if _FORBIDDEN_GENERATED_MACRO.search(content):
            raise SecurityError(f"generated Rust uses a forbidden environment/file macro: {path}")
        if _FORBIDDEN_GENERATED_RUST.search(content):
            raise SecurityError(f"generated Rust uses std, which is not allowed: {path}")
        for token in _FORBIDDEN_GENERATED_TOKENS:
            if token in content:
                raise SecurityError(f"generated Rust uses forbidden construct {token}: {path}")


def _remove_reserved_smoke_marker(content: str) -> str:
    """Remove only the validator-owned module marker echoed by a repair model."""
    return content.replace(f"\n{_SMOKE_MARKER}\n", "\n").replace(f"\n{_SMOKE_MARKER}", "\n")
