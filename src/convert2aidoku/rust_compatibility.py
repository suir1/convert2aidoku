from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .aidoku_import_compatibility import (
    finalize_aidoku_imports,
    normalize_aidoku_api_paths,
    normalize_aidoku_registration_imports,
)
from .aidoku_model_compatibility import normalize_aidoku_models
from .aidoku_page_context_compatibility import (
    normalize_image_request_compatibility,
    normalize_page_context_compatibility,
)
from .chapter_date_compatibility import (
    finalize_chapter_date_compatibility,
    normalize_chapter_date_compatibility,
)
from .dynamic_filter_compatibility import (
    normalize_filter_predicate_compatibility,
    normalize_legacy_dynamic_filters,
    normalize_select_filter_path_compatibility,
    normalize_select_filter_struct_compatibility,
)
from .generated_rust_safety import _remove_reserved_smoke_marker as _remove_reserved_smoke_marker
from .generated_rust_safety import validate_generated_content as validate_generated_content
from .generation_setting_compatibility import normalize_generation_settings
from .graphql_compatibility import (
    normalize_graphql_fragment_compatibility,
    normalize_graphql_projection_compatibility,
    normalize_graphql_request_compatibility,
)
from .normalization_trace import NormalizationTrace
from .request_response_compatibility import (
    normalize_legacy_request_compatibility,
    normalize_request_response_compatibility,
    normalize_request_result_tails,
    normalize_request_retry_compatibility,
)
from .rust_control_flow_compatibility import (
    normalize_early_control_flow,
    normalize_indexing_control_flow,
    normalize_iteration_control_flow,
    normalize_late_control_flow,
)
from .rust_inspection import RustInspection
from .rust_ownership_compatibility import normalize_detail_ownership, normalize_pagination_ownership
from .source_url_compatibility import (
    normalize_deep_link_compatibility,
    normalize_ir_source_urls,
    normalize_literal_url_compatibility,
    normalize_preserved_source_urls,
    normalize_source_path_helpers,
)


def _normalize_defaults_get_bindings(content: str) -> str:
    """Unwrap defaults into explicitly non-optional local bindings."""
    pattern = re.compile(
        r"(?P<prefix>\blet\s+(?:mut\s+)?[A-Za-z_]\w*\s*:\s*)"
        r"(?P<type>String|bool|i(?:8|16|32|64|128|size)|u(?:8|16|32|64|128|size)|f(?:32|64))"
        r"(?P<equals>\s*=\s*)"
        r"(?P<path>(?:aidoku::imports::defaults::)?defaults_get)"
        r"(?:\s*::\s*<[^>]+>)?\s*\(\s*(?P<key>[^()]+?)\s*\)\s*;"
    )

    def replace(match: re.Match[str]) -> str:
        value_type = match.group("type")
        return (
            f"{match.group('prefix')}{value_type}{match.group('equals')}"
            f"{match.group('path')}::<{value_type}>({match.group('key')})"
            ".unwrap_or_default();"
        )

    return pattern.sub(replace, content)


def _normalize_owned_setting_routes(content: str) -> str:
    """Keep a selected setting-owned route alive after its branch closes."""
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        saved = re.search(
            r"\blet\s+(?P<name>[A-Za-z_]\w*)\s*=\s*"
            r"(?:aidoku::imports::defaults::)?defaults_get\s*::\s*<\s*String\s*>",
            function.text,
        )
        if saved is None or f"=> {saved.group('name')}.as_str()," not in function.text:
            continue
        normalized = function.text.replace(
            f"=> {saved.group('name')}.as_str(),",
            f"=> {saved.group('name')}.clone(),",
        )
        normalized = re.sub(
            r"(?P<prefix>\blet\s+[A-Za-z_]\w*\s*=\s*if\s+[^\{]{1,300}\{\s*)"
            r"(?P<literal>\"(?:\\.|[^\"\\])*\")(?P<suffix>\s*\}\s*else\s*\{)",
            r"\g<prefix>String::from(\g<literal>)\g<suffix>",
            normalized,
            count=1,
        )
        normalized = re.sub(
            r"(?P<prefix>=>\s*)(?P<literal>\"(?:\\.|[^\"\\])*\")(?P<suffix>\s*,)",
            r"\g<prefix>String::from(\g<literal>)\g<suffix>",
            normalized,
        )
        replacements.append((function.text, normalized))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    return content


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


def _normalize_defaults_set_string_values(content: str) -> str:
    """Wrap obvious owned String expressions for the pinned defaults_set API."""
    pattern = re.compile(
        r"(?P<prefix>\bdefaults_set\(\s*\"(?:\\.|[^\"\\])*\"\s*,\s*)"
        r"(?P<value>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\.(?:clone|to_string)\(\))"
        r"(?P<suffix>\s*\);)"
    )
    return pattern.sub(
        lambda found: (
            found.group("prefix")
            + "aidoku::imports::defaults::DefaultValue::String("
            + found.group("value")
            + ")"
            + found.group("suffix")
        ),
        content,
    )


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


def _normalize_mutated_aidoku_models(content: str) -> str:
    content = re.sub(
        r"(?P<target>\b[A-Za-z_]\w*)\.author\s*=\s*"
        r"(?P<expression>[\s\S]{1,700}?\.collect(?:::\s*<Vec<_>>)?\(\))"
        r"\s*\.join\([^;]{0,120}\)\s*;",
        r"\g<target>.authors = Some(\g<expression>);",
        content,
    )

    def wrap_optional(match: re.Match[str]) -> str:
        expression = match.group("expression").strip()
        if expression.startswith("Some("):
            return match.group(0)
        return (
            f"{match.group('indent')}{match.group('target')}.{match.group('field')} = "
            f"Some({expression});"
        )

    content = re.sub(
        r"(?m)^(?P<indent>[ \t]*)(?P<target>manga)\."
        r"(?P<field>url|cover|description)\s*=\s*(?P<expression>[^;\n]+);",
        wrap_optional,
        content,
    )
    content = re.sub(
        r"(?m)^(?P<indent>[ \t]*)(?P<target>chapter)\."
        r"(?P<field>url|title)\s*=\s*(?P<expression>[^;\n]+);",
        wrap_optional,
        content,
    )
    edits: list[tuple[int, int, bytes]] = []
    for node in RustInspection.from_content(content).nodes("assignment_expression"):
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None:
            continue
        target = re.fullmatch(
            r"(?P<model>manga|chapter)\.(?P<field>[A-Za-z_]\w*)",
            left.text.decode("utf-8", errors="replace"),
        )
        if target is None:
            continue
        required_fields = {"key", "title"} if target.group("model") == "manga" else {"key"}
        expression = right.text.decode("utf-8", errors="replace")
        wrapped_required = re.fullmatch(r"Some\((?P<value>[\s\S]+)\)", expression)
        if target.group("field") in required_fields and wrapped_required is not None:
            edits.append(
                (
                    right.start_byte,
                    right.end_byte,
                    wrapped_required.group("value").encode(),
                )
            )
            continue
        optional_fields = (
            {"url", "cover", "description"}
            if target.group("model") == "manga"
            else {"url", "title"}
        )
        if target.group("field") not in optional_fields:
            continue
        if expression.startswith(("Some(", "None")):
            continue
        edits.append((right.start_byte, right.end_byte, f"Some({expression})".encode()))
    encoded = content.encode("utf-8")
    for begin, end, replacement in sorted(edits, reverse=True):
        encoded = encoded[:begin] + replacement + encoded[end:]
    content = encoded.decode("utf-8")
    manga_sources = re.findall(
        r"\blet\s+(?:mut\s+)?(?P<source>[A-Za-z_]\w*)\s*=\s*"
        r"[^;\n]+\.to_manga\(\)\s*;",
        content,
    )
    for source in manga_sources:
        content = re.sub(
            rf"\bmanga\.(?P<field>cover|description)\s*=\s*Some\("
            rf"{re.escape(source)}\.(?P=field)\);",
            rf"manga.\g<field> = {source}.\g<field>;",
            content,
        )
    return content


def _normalize_default_model_assignments(content: str) -> str:
    encoded = content.encode("utf-8")
    edits: list[tuple[int, int, bytes]] = []
    for function in RustInspection.from_content(content).functions:
        body = function.node.child_by_field_name("body")
        if body is None:
            continue
        children = body.named_children
        for index, child in enumerate(children):
            declaration = child.text.decode("utf-8", errors="replace")
            match = re.fullmatch(
                r"let\s+mut\s+(?P<variable>[A-Za-z_]\w*)\s*=\s*"
                r"(?P<model>Manga|Chapter|Page)::default\(\)\s*;",
                declaration.strip(),
            )
            if match is None:
                continue
            variable = match.group("variable")
            fields: list[tuple[str, str]] = []
            cursor = index + 1
            while cursor < len(children):
                assignment = children[cursor].text.decode("utf-8", errors="replace").strip()
                field_match = re.fullmatch(
                    rf"{re.escape(variable)}\.(?P<field>[A-Za-z_]\w*)\s*=\s*"
                    r"(?P<expression>[\s\S]+);",
                    assignment,
                )
                if field_match is None:
                    break
                fields.append((field_match.group("field"), field_match.group("expression").strip()))
                cursor += 1
            if (
                not fields
                or cursor >= len(children)
                or children[cursor].text.decode("utf-8", errors="replace").strip() != variable
                or len({field for field, _expression in fields}) != len(fields)
            ):
                continue
            line_start = encoded.rfind(b"\n", 0, child.start_byte) + 1
            indent = encoded[line_start : child.start_byte].decode("utf-8", errors="replace")
            lines = [
                f"{indent}let {variable} = {match.group('model')} {{",
                *[f"{indent}    {field}: {expression}," for field, expression in fields],
                f"{indent}    ..Default::default()",
                f"{indent}}};",
                f"{indent}{variable}",
            ]
            edits.append(
                (
                    child.start_byte,
                    children[cursor].end_byte,
                    "\n".join(lines).encode("utf-8"),
                )
            )
            break
    for start, end, replacement in reversed(edits):
        encoded = encoded[:start] + replacement + encoded[end:]
    return encoded.decode("utf-8")


def _normalize_page_index_fields(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for node in RustInspection.from_content(content).nodes("struct_expression"):
        name = node.child_by_field_name("name")
        if name is None or name.text.decode("utf-8", errors="replace") != "Page":
            continue
        original = node.text.decode("utf-8", errors="replace")
        normalized = re.sub(r"(?m)^[ \t]*index\s*:\s*[^,\n]+,\s*\n?", "", original)
        normalized = re.sub(r"(?m)^[ \t]*index\s*,\s*\n?", "", normalized)
        normalized = re.sub(r"\{\s*index\s*:\s*[^,}]+,\s*", "{ ", normalized)
        if "content:" in normalized:
            normalized = re.sub(r"(?m)^[ \t]*url\s*:\s*[^,\n]+,\s*\n?", "", normalized)
        if normalized != original:
            replacements.append((original, normalized))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    removals: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        normalized = function.text
        for binding in re.finditer(
            r"(?m)^(?P<indent>[ \t]*)let\s+(?P<name>index)\s*=\s*[^;]+;\s*\n?",
            function.text,
        ):
            if len(re.findall(rf"\b{binding.group('name')}\b", function.text)) == 1:
                normalized = normalized.replace(binding.group(0), "", 1)
        if normalized != function.text:
            removals.append((function.text, normalized))
    for original, replacement in removals:
        content = content.replace(original, replacement, 1)
    return content


def _normalize_generic_deserialize(content: str) -> str:
    pattern = re.compile(
        r"(?P<derive>#\[derive\([^\]]*\bDeserialize\b[^\]]*\)\]\s*)"
        r"(?P<attributes>(?:#\[[^\]]+\]\s*)*)"
        r"(?P<header>struct\s+(?P<name>[A-Za-z_]\w*)\s*<(?P<params>[^>{}]+)>\s*\{)"
        r"(?P<body>[\s\S]*?\n\})"
    )

    def add_bound(match: re.Match[str]) -> str:
        attributes = match.group("attributes")
        if "serde(bound" in attributes or "serde(default)" not in match.group("body"):
            return match.group(0)
        params = [item.strip() for item in match.group("params").split(",")]
        names = [item.split(":", 1)[0].strip() for item in params]
        if not names or not all(re.fullmatch(r"[A-Za-z_]\w*", name) for name in names):
            return match.group(0)
        bound = ", ".join(f"{name}: aidoku::serde::Deserialize<'de>" for name in names)
        return (
            match.group("derive")
            + f'#[serde(bound(deserialize = "{bound}"))]\n'
            + attributes
            + match.group("header")
            + match.group("body")
        )

    content = pattern.sub(add_bound, content)
    content = re.sub(
        r"(?P<type>[A-Za-z_]\w*)\s*:\s*Deserialize\s*<\s*'static\s*>",
        r"\g<type>: for<'de> Deserialize<'de>",
        content,
    )
    content = re.sub(
        r"fn\s+(?P<name>[A-Za-z_]\w*)\s*<\s*'(?P<lifetime>[A-Za-z_]\w*)\s*,\s*"
        r"(?P<type>[A-Za-z_]\w*)\s*:\s*Deserialize\s*<\s*'(?P=lifetime)\s*>\s*>",
        r"fn \g<name><\g<type>: for<'de> Deserialize<'de>>",
        content,
    )
    generic_structs = re.findall(
        r"\bstruct\s+(?P<name>[A-Za-z_]\w*)\s*<(?P<params>[^>{}]+)>\s*\{",
        content,
    )
    for name, params in generic_structs:
        declarations = [item.strip() for item in params.split(",")]
        arguments = [item.split(":", 1)[0].strip() for item in declarations]
        if not arguments or not all(
            re.fullmatch(r"[A-Za-z_]\w*", argument) for argument in arguments
        ):
            continue
        content = re.sub(
            rf"\bimpl\s+{re.escape(name)}\s*\{{",
            f"impl<{', '.join(declarations)}> {name}<{', '.join(arguments)}> {{",
            content,
        )
    return content


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


def _normalize_html_element_text(content: str) -> str:
    content = re.sub(
        r"\.map\(\s*\|(?P<value>[A-Za-z_]\w*)\|\s*"
        r"(?P=value)\.text\(\)\s*\)",
        r".and_then(|\g<value>| \g<value>.text())",
        content,
    )
    content = re.sub(
        r"(?P<element>[A-Za-z_]\w*)\.text\(\)\.as_str\(\)",
        r"\g<element>.text().as_deref().unwrap_or_default()",
        content,
    )
    content = re.sub(
        r"(?P<element>[A-Za-z_]\w*)\s*\.text\(\)(?P<space>\s*)(?P<method>\."
        r"(?:chars|find|is_empty|len|parse|split|trim|contains|starts_with|ends_with))"
        r"(?P<generic>::\s*<[^>]+>)?\(",
        r"\g<element>.text().unwrap_or_default()\g<space>\g<method>\g<generic>(",
        content,
    )
    content = re.sub(
        r"&(?P<element>[A-Za-z_]\w*)\.text\(\)(?!\.unwrap_or_default\(\))",
        r"&\g<element>.text().unwrap_or_default()",
        content,
    )
    content = re.sub(
        r"(?P<element>[A-Za-z_]\w*)\.text\(\)\s*(?P<operator>==|!=)\s*"
        r"(?P<literal>\"(?:\\.|[^\"\\])*\")",
        r"\g<element>.text().as_deref() \g<operator> Some(\g<literal>)",
        content,
    )
    content = re.sub(
        r"(?P<callee>(?:self\.)?normalized_text|(?:aidoku::)?AidokuError::message)"
        r"\(\s*(?P<element>[A-Za-z_]\w*)\.text\(\)(?!\.unwrap_or_default\(\))",
        r"\g<callee>(\g<element>.text().unwrap_or_default()",
        content,
    )
    content = re.sub(
        r"(?P<target>\bmanga\.title)\s*=\s*"
        r"(?P<element>[A-Za-z_]\w*)\.text\(\)\s*;",
        r"\g<target> = \g<element>.text().unwrap_or_default();",
        content,
    )
    content = re.sub(
        r"(?P<values>\b[A-Za-z_]\w*)\.push\("
        r"(?P<element>[A-Za-z_]\w*)\.text\(\)\)\s*;",
        r"\g<values>.extend(\g<element>.text());",
        content,
    )
    replacements: list[tuple[str, str]] = []
    binding_pattern = re.compile(
        r"\blet\s+(?:mut\s+)?(?P<name>[A-Za-z_]\w*)\s*=\s*"
        r"(?P<element>[A-Za-z_]\w*)\.text\(\)\s*;"
    )
    for function in RustInspection.from_content(content).functions:
        normalized = function.text
        for binding in binding_pattern.finditer(function.text):
            name = binding.group("name")
            remaining = function.text[binding.end() :]
            preserves_option = re.search(
                rf"\b{re.escape(name)}\.(?:as_deref|as_ref|and_then|map|is_some|is_none|"
                r"unwrap|unwrap_or|unwrap_or_default|take)\b"
                rf"|\b(?:if|while)\s+let\s+Some\([^)]*\)\s*=\s*{re.escape(name)}\b"
                r"(?!\s*\.)"
                rf"|\bmatch\s+{re.escape(name)}\b",
                remaining,
            )
            if preserves_option is not None:
                continue
            normalized = normalized.replace(
                binding.group(0),
                binding.group(0).replace(".text()", ".text().unwrap_or_default()"),
                1,
            )
        if normalized != function.text:
            replacements.append((function.text, normalized))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    return content


def _normalize_struct_expression_defaults(content: str) -> str:
    filter_fields = {
        "CheckFilter": {"id", "title", "hide_from_header", "name", "can_exclude", "default"},
        "MultiSelectFilter": {
            "id",
            "title",
            "hide_from_header",
            "is_genre",
            "can_exclude",
            "uses_tag_style",
            "options",
            "ids",
            "default_included",
            "default_excluded",
        },
        "SelectFilter": {
            "id",
            "title",
            "hide_from_header",
            "is_genre",
            "uses_tag_style",
            "options",
            "ids",
            "default",
        },
        "SortFilter": {"id", "title", "hide_from_header", "can_ascend", "options", "default"},
    }
    replacements: list[tuple[str, str]] = []
    for node in RustInspection.from_content(content).nodes("struct_expression"):
        name = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name is None or body is None:
            continue
        full_type_name = name.text.decode("utf-8", errors="replace")
        if re.search(r"(?:^|::)DeepLinkResult::", full_type_name):
            continue
        type_name = full_type_name.rsplit("::", 1)[-1]
        text = node.text.decode("utf-8", errors="replace")
        if (
            type_name
            not in {
                "Manga",
                "Chapter",
                "Page",
                "CheckFilter",
                "MultiSelectFilter",
                "SelectFilter",
                "SortFilter",
            }
            or "..Default::default()" in text
        ):
            continue
        required = filter_fields.get(type_name)
        if required is not None:
            present = {
                field.text.decode("utf-8", errors="replace")
                for child in body.named_children
                if (field := child.child_by_field_name("field")) is not None
            }
            if required.issubset(present):
                continue
        closing = re.search(r"\n(?P<indent>[ \t]*)\}$", text)
        if closing is None:
            continue
        indent = closing.group("indent")
        head = text[: closing.start()].rstrip()
        if not head.endswith(","):
            head += ","
        replacements.append((text, f"{head}\n{indent}    ..Default::default()\n{indent}}}"))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    return content


def _normalize_pagination_result_impls(content: str) -> str:
    additions = []
    inspection = RustInspection.from_content(content)
    for struct in inspection.structs:
        fields = {field.name for field in struct.fields}
        if not {"total", "limit", "offset"}.issubset(fields):
            continue
        has_impl = re.search(
            rf"\bimpl(?:\s*<[^>]+>)?\s+{re.escape(struct.name)}"
            r"(?:\s*<[^>]+>)?\s*\{[\s\S]*?"
            r"\bfn\s+has_next\s*\(",
            content,
        )
        if has_impl is not None:
            continue
        generic = re.search(
            rf"\bstruct\s+{re.escape(struct.name)}\s*<(?P<params>[^>]+)>",
            struct.text,
        )
        header = f"impl {struct.name}"
        if generic is not None:
            declarations = [item.strip() for item in generic.group("params").split(",")]
            arguments = [item.split(":", 1)[0].strip() for item in declarations]
            if arguments and all(re.fullmatch(r"[A-Za-z_]\w*", argument) for argument in arguments):
                header = f"impl<{', '.join(declarations)}> {struct.name}<{', '.join(arguments)}>"
        additions.append(
            f"{header} {{\n"
            "    pub fn has_next(&self) -> bool {\n"
            "        self.total >= self.offset + self.limit\n"
            "    }\n"
            "}"
        )
    if additions:
        content = content.rstrip() + "\n\n" + "\n\n".join(additions) + "\n"
    return content


def _normalize_resolution_regex(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        pattern = r"\d+(?=x\.(?:jpg|webp)$)"
        escaped_pattern = pattern.replace("\\", "\\\\")
        if (
            pattern not in function.text and escaped_pattern not in function.text
        ) or "resolution" not in function.text:
            continue
        opening = function.text.find("{")
        if opening < 0:
            continue
        replacement = (
            function.text[:opening].rstrip()
            + """ {
    let suffix_start = if url.ends_with(".jpg") {
        Some(url.len() - 4)
    } else if url.ends_with(".webp") {
        Some(url.len() - 5)
    } else {
        None
    };
    if let Some(suffix_start) = suffix_start {
        let before_suffix = &url[..suffix_start];
        if let Some(x_pos) = before_suffix.rfind('x') {
            let before_x = &before_suffix[..x_pos];
            let digits_start = before_x
                .rfind(|character: char| !character.is_ascii_digit())
                .map_or(0, |position| position + 1);
            if digits_start < x_pos {
                return format!("{}{}{}", &url[..digits_start], resolution, &url[x_pos..]);
            }
        }
    }
    url.to_string()
}"""
        )
        replacements.append((function.text, replacement))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    return content


def normalize_pinned_aidoku_rust(
    content: str,
    *,
    allow_dead_code: bool = False,
    setting_defaults: Mapping[str, str] | None = None,
    setting_keys: tuple[str, ...] | None = None,
    setting_values: Mapping[str, tuple[str, ...]] | None = None,
    prequeried_url_helpers: set[str] | None = None,
    preserve_cover_urls: bool = False,
    public_base_url: str | None = None,
    chapter_key_templates: tuple[str, ...] | None = None,
    request_builder_helpers: set[str] | None = None,
    remove_extern_std: bool = False,
    trace: NormalizationTrace | None = None,
) -> str:
    """Apply small type-safe compatibility rewrites for the pinned Aidoku/Rust APIs."""
    active_trace = trace or NormalizationTrace()

    def apply(rewrite: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal content
        content = active_trace.apply(
            rewrite.__name__.removeprefix("_"),
            content,
            lambda value: rewrite(value, *args, **kwargs),
        )

    def record(rule_id: str, before: str) -> None:
        active_trace.observe(rule_id, before, content)

    content = normalize_early_control_flow(content, trace=active_trace)
    apply(_normalize_safe_std_paths, remove_extern_std=remove_extern_std)
    content = normalize_graphql_request_compatibility(content, trace=active_trace)
    content = normalize_aidoku_api_paths(content, trace=active_trace)
    apply(_normalize_generic_deserialize)
    content = normalize_graphql_fragment_compatibility(content, trace=active_trace)
    apply(_normalize_html_element_text)
    content = normalize_indexing_control_flow(content, trace=active_trace)
    content = normalize_graphql_projection_compatibility(content, trace=active_trace)
    content = normalize_image_request_compatibility(content, trace=active_trace)
    content = normalize_request_result_tails(content, trace=active_trace)
    content = normalize_detail_ownership(content, trace=active_trace)
    content = normalize_legacy_request_compatibility(content, trace=active_trace)
    apply(_normalize_defaults_get_bindings)
    apply(_normalize_owned_setting_routes)
    apply(_normalize_defaults_set_string_values)
    apply(_normalize_rsa_bootstrap_diagnostics)
    content = normalize_request_response_compatibility(
        content,
        request_builder_helpers=request_builder_helpers,
        trace=active_trace,
    )
    apply(_inject_source_new)
    apply(_normalize_source_new_delegation)
    apply(_normalize_rate_limit_integer_types)
    apply(_normalize_mutated_aidoku_models)
    apply(_normalize_default_model_assignments)
    apply(_normalize_page_index_fields)
    content = normalize_legacy_dynamic_filters(content, trace=active_trace)
    content = normalize_page_context_compatibility(content, trace=active_trace)
    content = normalize_deep_link_compatibility(content, trace=active_trace)
    content = normalize_chapter_date_compatibility(content, trace=active_trace)
    before = content
    if allow_dead_code and not re.search(r"#!\[allow\([^\]]*\bdead_code\b", content):
        content = "#![allow(dead_code)]\n" + content.lstrip()
    record("allow_dead_code", before)
    content = normalize_select_filter_path_compatibility(content, trace=active_trace)
    content = normalize_aidoku_registration_imports(content, trace=active_trace)
    content = normalize_literal_url_compatibility(content, trace=active_trace)
    content = normalize_request_retry_compatibility(content, trace=active_trace)
    content = normalize_aidoku_models(content, trace=active_trace)
    content = normalize_source_path_helpers(content, trace=active_trace)
    apply(_normalize_struct_expression_defaults)
    apply(_normalize_pagination_result_impls)
    content = normalize_pagination_ownership(content, trace=active_trace)
    content = normalize_select_filter_struct_compatibility(content, trace=active_trace)
    apply(_normalize_resolution_regex)
    content = normalize_iteration_control_flow(content, trace=active_trace)
    content = normalize_filter_predicate_compatibility(content, trace=active_trace)
    content = normalize_ir_source_urls(
        content,
        prequeried_url_helpers=prequeried_url_helpers,
        public_base_url=public_base_url,
        chapter_key_templates=chapter_key_templates,
        trace=active_trace,
    )
    content = normalize_late_control_flow(content, trace=active_trace)
    content = normalize_preserved_source_urls(
        content,
        preserve_cover_urls=preserve_cover_urls,
        trace=active_trace,
    )
    before = content
    content = re.sub(
        r"\blet\s+domain\s*=\s*defaults_get\(",
        "let domain: String = defaults_get(",
        content,
    )
    content = content.replace(
        "let domain: String = defaults_get(",
        "let domain: String = defaults_get::<String>(",
    )
    record("typed_domain_default", before)
    before = content
    content = re.sub(
        r"(?P<prefix>\b(?:id|title):\s*(?:Some\()?)"
        r'(?P<literal>"(?:\\.|[^"\\])*")\.to_string\(\)',
        r"\g<prefix>\g<literal>.into()",
        content,
    )
    record("aidoku_model_string_into", before)
    before = content
    content = content.replace(
        '.header("User-Agent", get_user_agent())',
        '.header("User-Agent", &get_user_agent())',
    )
    content = content.replace(".header(key, val)", ".header(key, &val)")
    content = re.sub(
        r'(?P<prefix>\.header\(\s*"(?:\\.|[^"\\])*"\s*,\s*)'
        r"(?P<value>[A-Za-z_]\w*)(?P<suffix>\s*\))",
        r"\g<prefix>&\g<value>\g<suffix>",
        content,
    )
    record("borrow_header_values", before)
    content = normalize_generation_settings(
        content,
        setting_defaults=setting_defaults,
        setting_keys=setting_keys,
        setting_values=setting_values,
        trace=active_trace,
    )
    content = finalize_aidoku_imports(content, trace=active_trace)
    content = finalize_chapter_date_compatibility(content, trace=active_trace)
    return content
