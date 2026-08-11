from __future__ import annotations

import re

from .normalization_trace import NormalizationTrace
from .rust_inspection import RustInspection


def _inject_source_new(content: str) -> str:
    inspection = RustInspection.from_content(content)
    structs = {struct.name: struct for struct in inspection.structs}
    replacements: list[tuple[str, str]] = []
    for node in inspection.nodes("impl_item"):
        text = node.text.decode("utf-8", errors="replace")
        header = re.match(r"impl\s+Source\s+for\s+(?P<name>[A-Za-z_]\w*)\s*\{", text)
        if header is None or re.search(r"\bfn\s+new\s*\(", text):
            continue
        name = header.group("name")
        struct = structs.get(name)
        is_unit = re.search(rf"\bstruct\s+{re.escape(name)}\s*;", content) is not None
        expression = (
            "Self" if is_unit or (struct is not None and not struct.fields) else "Self::default()"
        )
        replacement = (
            text[: header.end()]
            + f"\n    fn new() -> Self {{ {expression} }}\n"
            + text[header.end() :]
        )
        replacements.append((text, replacement))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    return content


def _normalize_rsa_bootstrap_diagnostics(content: str) -> str:
    """Preserve a signed API's response body when anonymous bootstrap JSON is rejected."""
    if "Pkcs1v15Encrypt" not in content:
        return content
    pattern = re.compile(
        r"(?P<prefix>\bfn\s+fetch_token\b[\s\S]{0,8000}?)"
        r"(?P<indent>^[ \t]*)let\s+(?P<name>[A-Za-z_]\w*)\s*:\s*"
        r"(?P<type>[A-Za-z_]\w*)\s*=\s*(?P<request>[A-Za-z_]\w*)"
        r"\.send\(\)\?\.get_json_owned\(\)\?;",
        re.MULTILINE,
    )

    def replace(found: re.Match[str]) -> str:
        indent = found.group("indent")
        response_text = f"{found.group('name')}_text"
        return (
            found.group("prefix")
            + f"{indent}let {response_text} = {found.group('request')}.send()?.get_string()?;\n"
            + f"{indent}let {found.group('name')}: {found.group('type')} = "
            + f"serde_json::from_str(&{response_text})\n"
            + f"{indent}    .map_err(|_| aidoku::AidokuError::message({response_text}))?;"
        )

    content = pattern.sub(replace, content, count=1)
    # Tachi Date().time is milliseconds. Aidoku current_date() is seconds.
    return content.replace(
        "(current_date() / 1_000) * 1_000",
        "current_date() * 1_000",
    )


def _normalize_rate_limit_integer_types(content: str) -> str:
    periods = {
        match.group("period")
        for match in re.finditer(
            r"set_rate_limit\(\s*[A-Za-z_]\w*\s*,\s*(?P<period>[A-Za-z_]\w*)\s*,",
            content,
        )
    }
    for name in periods:
        content = re.sub(
            rf"(\blet\s+(?:mut\s+)?{re.escape(name)}\s*:\s*)i64\b",
            r"\g<1>i32",
            content,
        )
    return content


def _normalize_source_new_delegation(content: str) -> str:
    inherent_new = {
        match.group("name")
        for match in re.finditer(
            r"impl\s+(?P<name>[A-Za-z_]\w*)\s*\{[\s\S]{0,12000}?\bpub\s+fn\s+new\s*\(",
            content,
        )
    }
    for name in inherent_new:
        content = re.sub(
            rf"(impl\s+Source\s+for\s+{re.escape(name)}\s*\{{[\s\S]{{0,1200}}?"
            rf"\bfn\s+new\s*\(\s*\)\s*->\s*Self\s*\{{\s*){re.escape(name)}::default\(\)",
            rf"\g<1>{name}::new()",
            content,
            count=1,
        )
    return content


def _normalize_safe_std_paths(content: str, *, remove_extern_std: bool) -> str:
    """Project allocation/core-only std paths into the no_std Aidoku runtime."""
    if remove_extern_std:
        content = re.sub(r"(?m)^\s*extern\s+crate\s+std\s*;\s*\n?", "", content)
    safe_aidoku_std_imports = {
        "String": "String",
        "ToString": "string::ToString",
        "Vec": "Vec",
        "format": "format",
        "vec": "vec",
    }
    for node in RustInspection.from_content(content).nodes("use_declaration"):
        original = node.text.decode("utf-8", errors="replace")
        if re.match(r"use\s+aidoku::", original) is None:
            continue
        match = re.search(r"\bstd::\{(?P<body>[^{}]+)\}", original)
        if match is None:
            continue
        items = [item.strip() for item in match.group("body").split(",") if item.strip()]
        if not items or any(item not in safe_aidoku_std_imports for item in items):
            continue
        projected = ", ".join(safe_aidoku_std_imports[item] for item in items)
        normalized = original[: match.start()] + f"alloc::{{{projected}}}" + original[match.end() :]
        content = content.replace(original, normalized, 1)
    content = re.sub(r"(?<!aidoku::)\balloc::vec!", "vec!", content)
    collection_aliases = {"HashMap": "BTreeMap", "HashSet": "BTreeSet"}
    for source, target in collection_aliases.items():
        marker = f"std::collections::{source}"
        if marker in content:
            content = content.replace(marker, f"aidoku::alloc::collections::{target}")
            content = re.sub(rf"\b{source}\b", target, content)
    content = content.replace("aidoku::alloc::collections::HashMap", "aidoku::HashMap")
    invalid_hash_set = "aidoku::alloc::collections::HashSet"
    if invalid_hash_set in content:
        content = content.replace(invalid_hash_set, "aidoku::alloc::collections::BTreeSet")
        content = re.sub(r"\bHashSet\b", "BTreeSet", content)
    replacements = {
        "std::collections::BTreeMap": "aidoku::alloc::collections::BTreeMap",
        "std::collections::BTreeSet": "aidoku::alloc::collections::BTreeSet",
        "std::borrow::Cow": "aidoku::alloc::borrow::Cow",
        "std::boxed::Box": "aidoku::alloc::boxed::Box",
        "std::string::String": "aidoku::alloc::string::String",
        "std::vec::Vec": "aidoku::alloc::vec::Vec",
        "std::collections::BinaryHeap": "aidoku::alloc::collections::BinaryHeap",
        "std::collections::LinkedList": "aidoku::alloc::collections::LinkedList",
        "std::collections::VecDeque": "aidoku::alloc::collections::VecDeque",
        "std::rc::": "aidoku::alloc::rc::",
        "std::sync::Arc": "aidoku::alloc::sync::Arc",
        "std::sync::Weak": "aidoku::alloc::sync::Weak",
        "std::any::": "core::any::",
        "std::cell::": "core::cell::",
        "std::cmp::": "core::cmp::",
        "std::convert::": "core::convert::",
        "std::error::": "core::error::",
        "std::fmt::": "core::fmt::",
        "std::hash::": "core::hash::",
        "std::iter::": "core::iter::",
        "std::marker::": "core::marker::",
        "std::mem::": "core::mem::",
        "std::num::": "core::num::",
        "std::option::Option": "core::option::Option",
        "std::ops::": "core::ops::",
        "std::result::Result": "core::result::Result",
        "std::slice::": "core::slice::",
        "std::str::": "core::str::",
        "std::time::": "core::time::",
    }
    for source, target in replacements.items():
        content = content.replace(source, target)
    return content


def _normalize_allow_dead_code(content: str, allow_dead_code: bool) -> str:
    if allow_dead_code and not re.search(r"#!\[allow\([^\]]*\bdead_code\b", content):
        return "#![allow(dead_code)]\n" + content.lstrip()
    return content


def normalize_source_runtime_prelude(
    content: str,
    *,
    remove_extern_std: bool,
    trace: NormalizationTrace,
) -> str:
    return trace.apply(
        "normalize_safe_std_paths",
        content,
        lambda value: _normalize_safe_std_paths(value, remove_extern_std=remove_extern_std),
    )


def normalize_source_bootstrap_compatibility(content: str, *, trace: NormalizationTrace) -> str:
    return trace.apply(
        "normalize_rsa_bootstrap_diagnostics",
        content,
        _normalize_rsa_bootstrap_diagnostics,
    )


def normalize_source_lifecycle_compatibility(content: str, *, trace: NormalizationTrace) -> str:
    for rewrite in (
        _inject_source_new,
        _normalize_source_new_delegation,
        _normalize_rate_limit_integer_types,
    ):
        content = trace.apply(rewrite.__name__.removeprefix("_"), content, rewrite)
    return content


def normalize_source_runtime_attributes(
    content: str,
    *,
    allow_dead_code: bool,
    trace: NormalizationTrace,
) -> str:
    return trace.apply(
        "allow_dead_code",
        content,
        lambda value: _normalize_allow_dead_code(value, allow_dead_code),
    )
