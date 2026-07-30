from __future__ import annotations

import json
import re
from collections.abc import Mapping
from importlib.resources import files as resource_files
from pathlib import Path, PurePosixPath
from typing import Any

from jinja2 import Environment, StrictUndefined

from .constants import DEFAULT_BROWSER_USER_AGENT, MAX_GENERATED_FILE_CHARS
from .decompiled_input import (
    decompiled_detail_uses_api_envelope,
    decompiled_dto_shapes,
    decompiled_dynamic_filter_endpoint,
    decompiled_nullable_dto_fields,
    decompiled_rank_list_wraps_comic,
)
from .dependency_policy import (
    AIDOKU_RS_REPOSITORY,
    AIDOKU_RS_REV,
    PinnedDependency,
    evaluate_dependency_policy,
)
from .errors import SecurityError
from .generated_source_metadata import GeneratedSourceMetadata
from .icons import create_aidoku_icon
from .ingest import ResolvedSource, copy_input_license, find_icon
from .models import (
    Capability,
    GeneratedFile,
    GeneratedResources,
    GenerationManifest,
    SourceFilterSpec,
    SourceIR,
    validate_generated_path,
)
from .rust_inspection import RustInspection

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
_RUST_IDENTIFIER_NODES = {"identifier", "raw_identifier", "type_identifier"}
_AIDOKU_ROOT_NAMES = {
    "BaseUrlProvider",
    "Chapter",
    "DeepLinkHandler",
    "DeepLinkResult",
    "DynamicFilters",
    "Filter",
    "FilterKind",
    "FilterValue",
    "ImageRequestProvider",
    "Listing",
    "ListingKind",
    "ListingProvider",
    "Manga",
    "MangaPageResult",
    "MangaStatus",
    "Page",
    "PageContent",
    "PageContext",
    "Source",
    "Viewer",
}


def _rust_identifier(node: Any) -> str | None:
    if node is None or node.type not in _RUST_IDENTIFIER_NODES:
        return None
    return node.text.decode("utf-8", errors="replace").removeprefix("r#")


def _last_rust_identifier(node: Any) -> str | None:
    if node is None:
        return None
    found: str | None = None
    stack = [node]
    while stack:
        current = stack.pop()
        identifier = _rust_identifier(current)
        if identifier is not None:
            found = identifier
        stack.extend(reversed(current.children))
    return found


def _first_rust_identifier(node: Any) -> str | None:
    if node is None:
        return None
    stack = [node]
    while stack:
        current = stack.pop()
        identifier = _rust_identifier(current)
        if identifier is not None:
            return identifier
        stack.extend(reversed(current.children))
    return None


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
    # A repair model may echo the tool-owned marker from its current-files
    # context. Strip exactly that marker; any other test module is rejected.
    return content.replace(f"\n{_SMOKE_MARKER}\n", "\n").replace(f"\n{_SMOKE_MARKER}", "\n")


def _alloc_macro_is_imported(content: str, name: str) -> bool:
    inspection = RustInspection.from_content(content)
    pattern = re.compile(
        rf"\balloc(?:::\{{[^}}]*\b{re.escape(name)}\b(?!::)[^}}]*\}}|::"
        rf"{re.escape(name)}\b(?!::))"
    )
    return any(
        (compact := RustInspection.compact_node(node)).startswith("useaidoku::")
        and pattern.search(compact) is not None
        for node in inspection.nodes("use_declaration")
    )


def _inject_no_std_macro_imports(content: str) -> str:
    missing = [
        name
        for name in ("format", "vec")
        if re.search(rf"(?<![:\w]){name}!\s*[\(\[\{{]", content)
        and not _alloc_macro_is_imported(content, name)
    ]
    if not missing:
        return content
    imports = "\n".join(f"use aidoku::alloc::{name};" for name in missing)
    crate_attributes = re.match(r"(?:\s*#!\[[^\n]*\]\s*\n)+", content)
    if crate_attributes is None:
        return imports + "\n\n" + content.lstrip()
    boundary = crate_attributes.end()
    return content[:boundary] + "\n" + imports + "\n" + content[boundary:].lstrip("\n")


def _normalize_idempotent_get_retry(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    inspection = RustInspection.from_content(content)
    for function in inspection.functions:
        text = function.text
        if (
            "let make_request" not in text
            or ".or_else" not in text
            or text.count("make_request()") < 2
        ):
            continue
        tail = re.search(r"\n(?P<indent>\s*)Ok\(make_request\(\)[\s\S]*\n\}$", text)
        if tail is None:
            continue
        indent = tail.group("indent")
        replacement = (
            text[: tail.start()].rstrip()
            + f"\n\n{indent}let response = match make_request()?.send() {{\n"
            + f"{indent}    Ok(response) => response,\n"
            + f"{indent}    Err(_) => make_request()?.send()?,\n"
            + f"{indent}}};\n"
            + f"{indent}Ok(response)\n"
            + "}"
        )
        replacements.append((text, replacement))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    return content


def _normalize_pinned_model_shapes(content: str) -> str:
    """Repair unambiguous model/request shapes for the pinned Aidoku revision."""
    content = content.replace("Manga::new()", "Manga::default()")
    content = re.sub(
        r"(?m)^\s*[A-Za-z_]\w*\.initialized\s*=\s*[^;]+;\s*\n?",
        "",
        content,
    )
    content = re.sub(
        r"\bscanlator\s*:\s*Some\((?P<value>[^,\n]+)\),",
        r"scanlators: Some(vec![\g<value>]),",
        content,
    )
    content = re.sub(
        r"Err\([A-Za-z_]\w*\)\s*=>\s*aidoku::log!\([^;\n]+\),",
        "Err(_) => {},",
        content,
    )
    content = re.sub(
        r"(?<!::)\balloc::borrow::Cow\b",
        "aidoku::alloc::borrow::Cow",
        content,
    )
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        normalized = function.text
        for field, values in (
            ("authors", ("author", "authors")),
            ("artists", ("artist", "artists")),
        ):
            for value in values:
                binding = re.search(
                    rf"\blet\s+{value}(?P<annotation>\s*:\s*String)?\s*=\s*"
                    r"(?P<expression>[^;]{1,800});",
                    normalized,
                )
                if binding is None or not (
                    binding.group("annotation")
                    or "String::" in binding.group("expression")
                    or ".join(" in binding.group("expression")
                ):
                    continue
                normalized = re.sub(
                    rf"\b(?:{field}|{field.removesuffix('s')}):\s*Some\({value}\),",
                    f"{field}: Some(vec![{value}]),",
                    normalized,
                )
        if normalized != function.text:
            replacements.append((function.text, normalized))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    content = re.sub(
        r"(?m)^(?P<indent>\s*)(?P<request>[A-Za-z_]\w*)\.send\(\)\s*$",
        r"\g<indent>Ok(\g<request>.send()?)",
        content,
    )
    content = re.sub(
        r"\bmanga\.author\s*=\s*(?P<source>[A-Za-z_]\w*)\.author\s*;",
        r"manga.authors = \g<source>.authors;",
        content,
    )
    content = re.sub(
        r"\bPageContent::Url\((?P<value>[^,()]+)\)",
        r"PageContent::url(\g<value>)",
        content,
    )
    content = re.sub(
        r"(?m)^(?P<indent>\s*)(?P<request>[A-Za-z_]\w*)\.header\((?P<args>[^;\r\n]+)\);",
        r"\g<indent>\g<request> = \g<request>.header(\g<args>);",
        content,
    )
    content = re.sub(
        r"(?P<request>[A-Za-z_]\w*)\s*=\s*(?P=request)\.header\("
        r"(?P<args>[^;\r\n]+)\)(?=\s*\})",
        r"\g<request>.header(\g<args>)",
        content,
    )
    content = content.replace(
        "Err(_) => Request::get(url)?.send(),",
        "Err(_) => Ok(Request::get(url)?.send()?),",
    )
    content = content.replace(
        "if total <= offset + chapters.len() {",
        "if total <= (offset + chapters.len()) as i32 {",
    )
    content = re.sub(
        r"sort_by_key\(\|item\|\s*core::cmp::Reverse\(item\.chapter_number\)\)",
        "sort_by(|left, right| right.chapter_number.partial_cmp(&left.chapter_number)"
        ".unwrap_or(core::cmp::Ordering::Equal))",
        content,
    )
    content = re.sub(
        r"(?P<right>[A-Za-z_]\w*)\.(?P<field>chapter_number|volume_number)"
        r"\.total_cmp\(&(?P<left>[A-Za-z_]\w*)\.(?P=field)\)",
        r"\g<right>.\g<field>.partial_cmp(&\g<left>.\g<field>)"
        r".unwrap_or(core::cmp::Ordering::Equal)",
        content,
    )
    content = re.sub(r"\bhas_next\s*:", "has_next_page:", content)
    content = re.sub(
        r"let\s+pages\s*=\s*if\s+words\.is_empty\(\)\s*\{\s*contents\s*\}",
        "let pages: Vec<&ContentItem> = if words.is_empty() { contents.iter().collect() }",
        content,
    )
    if re.search(r"\blet\s+mut\s+[A-Za-z_]\w*\s*=\s*manga\s*;", content):
        content = re.sub(
            r"\blet\s+path\s*=\s*&manga\.key\s*;",
            "let path = manga.key.clone();",
            content,
        )
    return content


def _normalize_optional_model_shorthand(content: str) -> str:
    optional = {
        "Manga": {"url", "cover", "description"},
        "Chapter": {"url", "title"},
    }
    edits: list[tuple[int, int, bytes]] = []
    for node in RustInspection.from_content(content).nodes("struct_expression"):
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name_node is None or body is None:
            continue
        fields = optional.get(name_node.text.decode("utf-8", errors="replace"), set())
        for field in body.named_children:
            if field.type != "shorthand_field_initializer":
                continue
            field_name = field.text.decode("utf-8", errors="replace")
            if field_name in fields:
                edits.append(
                    (
                        field.start_byte,
                        field.end_byte,
                        f"{field_name}: Some({field_name})".encode(),
                    )
                )
    encoded = content.encode("utf-8")
    for begin, end, replacement in reversed(edits):
        encoded = encoded[:begin] + replacement + encoded[end:]
    return encoded.decode("utf-8")


def _normalize_pinned_model_fields(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    inspection = RustInspection.from_content(content)
    option_number_functions = {
        function.name
        for function in inspection.functions
        if re.search(r"->\s*(?:core::option::)?Option\s*<\s*f(?:32|64)\s*>", function.text)
    }
    option_f32_bindings = {
        function.node.start_byte: {
            match.group("name")
            for match in re.finditer(
                r"\blet\s+(?:mut\s+)?(?P<name>[A-Za-z_]\w*)\s*=\s*"
                r"[^;]{0,800}?\.parse\s*::\s*<\s*f32\s*>\s*\(\s*\)\s*"
                r"\.ok\s*\(\s*\)\s*;",
                function.text,
            )
        }
        for function in inspection.functions
    }
    local_option_bindings = {
        function.node.start_byte: {
            match.group("name")
            for match in re.finditer(
                r"\blet\s+(?:mut\s+)?(?P<name>[A-Za-z_]\w*)"
                r"(?P<annotation>\s*:\s*(?:core::option::)?Option\s*<[^;=]+>)?\s*=\s*"
                r"(?P<expression>[^;]{1,1200});",
                function.text,
            )
            if match.group("annotation")
            or (
                any(marker in match.group("expression") for marker in (".map(", ".and_then("))
                and ".unwrap_or" not in match.group("expression")
            )
        }
        for function in inspection.functions
    }
    for node in inspection.nodes("struct_expression"):
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name_node is None or body is None:
            continue
        type_name = name_node.text.decode("utf-8", errors="replace")
        if type_name not in {"Manga", "Chapter"}:
            if type_name == "MangaPageResult":
                original = node.text.decode("utf-8", errors="replace")
                normalized = re.sub(
                    r"(?m)^(?P<indent>\s*)has_next\s*,",
                    r"\g<indent>has_next_page: has_next,",
                    original,
                )
                if normalized != original:
                    replacements.append((original, normalized))
            continue
        for field in body.named_children:
            field_node = field.child_by_field_name("field")
            value_node = field.child_by_field_name("value")
            if field_node is None or value_node is None:
                continue
            field_name = field_node.text.decode("utf-8", errors="replace")
            value = value_node.text.decode("utf-8", errors="replace")
            replacement_value = value
            owner = node
            while owner.parent is not None and owner.type != "function_item":
                owner = owner.parent
            local_options = local_option_bindings.get(owner.start_byte, set())
            nested_local_option = re.fullmatch(r"Some\((?P<name>[A-Za-z_]\w*)\)", value)
            if type_name == "Manga" and field_name == "title" and value.endswith(".text()"):
                replacement_value = f"{value}.unwrap_or_default()"
            elif (
                field_name == "url"
                or (type_name == "Manga" and field_name in {"cover", "description"})
                or (type_name == "Chapter" and field_name == "title")
            ) and value in local_options:
                replacement_value = value
            elif (
                field_name == "url"
                or (type_name == "Manga" and field_name in {"cover", "description"})
                or (type_name == "Chapter" and field_name == "title")
            ) and (
                nested_local_option is not None
                and nested_local_option.group("name") in local_options
            ):
                replacement_value = nested_local_option.group("name")
            elif (
                field_name == "url"
                or (type_name == "Manga" and field_name in {"cover", "description"})
                or (type_name == "Chapter" and field_name == "title")
            ) and not value.startswith(("Some(", "None")):
                replacement_value = f"Some({value})"
            elif type_name == "Manga" and field_name == "status" and value.startswith("Some("):
                replacement_value = value[5:-1]
            elif type_name == "Chapter" and field_name in {"chapter_number", "volume_number"}:
                local_option_f32_bindings = option_f32_bindings.get(owner.start_byte, set())
                if value in local_option_f32_bindings:
                    continue
                direct_option_call = re.fullmatch(
                    r"(?:(?:Self::|self\.)?)(?P<name>[A-Za-z_]\w*)\([\s\S]*\)",
                    value,
                )
                if (
                    direct_option_call is not None
                    and direct_option_call.group("name") in option_number_functions
                ):
                    continue
                invalid_bound_cast = re.fullmatch(
                    r"Some\(\(\s*(?P<name>[A-Za-z_]\w*)\s*\)\s+as\s+f32\)", value
                )
                invalid_option_cast = re.fullmatch(
                    r"Some\(\((?P<value>[\s\S]+\.ok\(\))\)\s+as\s+f32\)", value
                )
                if (
                    invalid_bound_cast is not None
                    and invalid_bound_cast.group("name") in local_option_f32_bindings
                ):
                    replacement_value = invalid_bound_cast.group("name")
                elif invalid_option_cast is not None:
                    replacement_value = invalid_option_cast.group("value")
                else:
                    invalid_option_call = re.fullmatch(
                        r"Some\(\((?P<value>(?:(?:Self::|self\.)?)(?P<name>[A-Za-z_]\w*)"
                        r"\([\s\S]*\))\)\s+as\s+f32\)",
                        value,
                    )
                    if (
                        invalid_option_call is not None
                        and invalid_option_call.group("name") in option_number_functions
                    ):
                        replacement_value = invalid_option_call.group("value")
                    elif value.startswith(("Some(", "None")) or value.endswith(".ok()"):
                        continue
                    elif ".map(" in value and ".unwrap_or" not in value:
                        replacement_value = value.replace(" as f64", " as f32")
                    else:
                        replacement_value = f"Some(({value}) as f32)"
            if replacement_value != value:
                original = field.text.decode("utf-8", errors="replace")
                replacements.append((original, f"{field_name}: {replacement_value}"))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    return content


def _normalize_nested_optional_model_fields(content: str) -> str:
    inspection = RustInspection.from_content(content)
    parameter_types: dict[int, dict[str, str]] = {}
    for function in inspection.functions:
        types: dict[str, str] = {}
        parameters = function.node.child_by_field_name("parameters")
        for parameter in parameters.named_children if parameters is not None else ():
            if parameter.type != "parameter":
                continue
            pattern = parameter.child_by_field_name("pattern")
            type_node = parameter.child_by_field_name("type")
            if pattern is None or type_node is None or pattern.type != "identifier":
                continue
            type_match = re.fullmatch(
                r"&\s*(?:mut\s+)?(?P<borrowed>[A-Za-z_]\w*)|(?P<owned>[A-Za-z_]\w*)",
                type_node.text.decode("utf-8", errors="replace").strip(),
            )
            if type_match is not None:
                types[pattern.text.decode("utf-8", errors="replace")] = type_match.group(
                    "borrowed"
                ) or type_match.group("owned")
        parameter_types[function.node.start_byte] = types

    edits: list[tuple[int, int, bytes]] = []
    for call in inspection.nodes("call_expression"):
        callee = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        if (
            callee is None
            or callee.text != b"Some"
            or arguments is None
            or len(arguments.named_children) != 1
        ):
            continue
        argument = arguments.named_children[0]
        if argument.type != "field_expression":
            continue
        source = argument.child_by_field_name("value")
        field = argument.child_by_field_name("field")
        if source is None or field is None or source.type != "identifier":
            continue

        owner = call
        model_target = False
        function_node = None
        while owner.parent is not None:
            owner = owner.parent
            if owner.type == "field_initializer":
                struct = owner.parent
                while struct is not None and struct.type != "struct_expression":
                    struct = struct.parent
                name = struct.child_by_field_name("name") if struct is not None else None
                model_target = name is not None and name.text in {b"Manga", b"Chapter"}
            elif owner.type == "assignment_expression":
                left = owner.child_by_field_name("left")
                model_target = (
                    left is not None and re.match(rb"(?:manga|chapter)\.", left.text) is not None
                )
            if owner.type == "function_item":
                function_node = owner
                break
        if not model_target or function_node is None:
            continue
        source_name = source.text.decode("utf-8", errors="replace")
        source_type = parameter_types.get(function_node.start_byte, {}).get(source_name)
        field_name = field.text.decode("utf-8", errors="replace")
        field_type = inspection.struct_field_type(source_type or "", field_name)
        if field_type is None or re.match(r"(?:core::option::)?Option\s*<", field_type) is None:
            continue
        edits.append((call.start_byte, call.end_byte, argument.text))

    encoded = content.encode("utf-8")
    for begin, end, replacement in sorted(edits, reverse=True):
        encoded = encoded[:begin] + replacement + encoded[end:]
    return encoded.decode("utf-8")


def _normalize_base_url_provider(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        if function.name != "get_base_url":
            continue
        normalized = re.sub(r"->\s*String\s*\{", "-> Result<String> {", function.text, count=1)
        if normalized == function.text:
            continue
        tail = re.search(
            r"\n(?P<indent>\s*)(?P<expression>[^;\n]+)\s*\n\s*\}$",
            normalized,
        )
        if tail is not None and not tail.group("expression").lstrip().startswith("Ok("):
            normalized = (
                normalized[: tail.start()]
                + f"\n{tail.group('indent')}Ok({tail.group('expression').strip()})\n}}"
            )
        replacements.append((function.text, normalized))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    return content


def _normalize_comic_path_helper(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        if function.name not in {"url2comic_path", "extract_comic_path"}:
            continue
        if '.split("/comic/")' not in function.text or "unwrap_or" not in function.text:
            continue
        opening = function.text.find("{")
        argument = re.search(
            r"\(\s*(?P<name>[A-Za-z_]\w*)\s*:\s*&str\b",
            function.text[:opening],
        )
        if opening < 0 or argument is None:
            continue
        name = argument.group("name")
        replacement = (
            function.text[:opening].rstrip()
            + " {\n"
            + f'    if let Some((_, path)) = {name}.split_once("/comic/") {{\n'
            + "        path.to_string()\n"
            + f'    }} else if let Some((_, path)) = {name}.split_once("/comic2/") {{\n'
            + "        path.to_string()\n"
            + "    } else {\n"
            + f"        {name}.trim_start_matches('/').to_string()\n"
            + "    }\n"
            + "}"
        )
        replacements.append((function.text, replacement))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    return content


def _normalize_aidoku_api_paths(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for node in RustInspection.from_content(content).nodes("use_declaration"):
        original = node.text.decode("utf-8", errors="replace")
        if not re.match(r"use\s+aidoku::", original):
            continue
        normalized = original.replace("aidoku::source::", "aidoku::")
        compact = RustInspection.compact_node(node)
        if compact.startswith("useaidoku::{"):
            for name in ("Request", "Response"):
                normalized = re.sub(rf"\b{name}\s*,\s*", "", normalized)
                normalized = re.sub(rf",\s*\b{name}\b", "", normalized)
                normalized = re.sub(rf"\{{\s*{name}\s*\}}", "{}", normalized)
        normalized = re.sub(r"\bMangasPage\s*,\s*", "", normalized)
        normalized = re.sub(r",\s*MangasPage\b", "", normalized)
        marker = "source::{"
        while (start := normalized.find(marker)) >= 0:
            opening = start + len(marker) - 1
            depth = 0
            closing = None
            for index in range(opening, len(normalized)):
                if normalized[index] == "{":
                    depth += 1
                elif normalized[index] == "}":
                    depth -= 1
                    if depth == 0:
                        closing = index
                        break
            if closing is None:
                break
            inner = normalized[opening + 1 : closing]
            normalized = normalized[:start] + inner + normalized[closing + 1 :]
        normalized = re.sub(r",(?P<space>\s*),", r",\g<space>", normalized)
        if re.fullmatch(r"use\s+aidoku::\{\s*\}\s*;", normalized.strip()):
            normalized = ""
        if normalized != original:
            replacements.append((original, normalized))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    content = content.replace("error::AidokuError", "AidokuError")
    content = re.sub(r"\bMangasPage\b", "MangaPageResult", content)
    content = content.replace("aidoku::Request", "Request")
    content = content.replace("aidoku::Response", "Response")
    content = content.replace("uri::encode(", "uri::encode_uri(")
    content = re.sub(
        r"\b(?P<response>resp|response)\.code\(\)",
        r"\g<response>.status_code()",
        content,
    )
    content = content.replace(".get_body_string()", ".get_string()")
    content = content.replace("listing.kind", "listing.id.as_str()")
    content = content.replace("ListingKind::Popular", '"popular"')
    content = content.replace("ListingKind::Latest", '"latest"')
    if "match listing.id.as_str()" in content:
        content = re.sub(
            r'(?P<arm>"latest"\s*=>\s*[^,\n]+,)(?P<space>\s*)(?P<closing>\})',
            r"\g<arm>\g<space>_ => popular_url(page),\g<space>\g<closing>",
            content,
            count=1,
        )
    return content


def _normalize_raw_json_response_bindings(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        normalized = function.text
        for match in re.finditer(
            r"let\s+(?P<name>[A-Za-z_]\w*)\s*=\s*"
            r"(?P<response>[A-Za-z_]\w*)\.get_json_owned\(\)\?;",
            function.text,
        ):
            name = match.group("name")
            if not re.search(
                rf"(?:from_str|parse_[A-Za-z_]\w*)\s*\(\s*&{re.escape(name)}\b",
                function.text,
            ):
                continue
            normalized = normalized.replace(
                match.group(0),
                match.group(0).replace(".get_json_owned()", ".get_string()"),
                1,
            )
        if normalized != function.text:
            replacements.append((function.text, normalized))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    return content


def _aidoku_root_imported(content: str, name: str) -> bool:
    for node in RustInspection.from_content(content).nodes("use_declaration"):
        compact = RustInspection.compact_node(node)
        if compact.startswith(f"useaidoku::{name}"):
            return True
        if compact.startswith("useaidoku::{") and re.search(
            rf"(?:\{{|,)\s*{re.escape(name)}(?:\s*[,}}])",
            compact.removeprefix("useaidoku::"),
        ):
            return True
    return False


def _aidoku_to_string_imported(content: str) -> bool:
    return any(
        (compact := RustInspection.compact_node(node)).startswith("useaidoku::")
        and re.search(r"alloc::(?:\{[^}]*?)?string(?:::\{[^}]*?)?::ToString\b", compact) is not None
        for node in RustInspection.from_content(content).nodes("use_declaration")
    )


def _aidoku_alloc_string_imported(content: str) -> bool:
    for use in RustInspection.from_content(content).nodes("use_declaration"):
        stack = [use]
        while stack:
            node = stack.pop()
            stack.extend(reversed(node.children))
            if _rust_identifier(node) != "String":
                continue
            current = node.parent
            relative_alloc = False
            while current is not None and current != use:
                compact = RustInspection.compact_node(current)
                if compact.startswith("aidoku::alloc::"):
                    return True
                if current.type == "scoped_use_list" and compact.startswith("alloc::{"):
                    relative_alloc = True
                elif (
                    current.type == "scoped_use_list"
                    and compact.startswith("aidoku::{")
                    and relative_alloc
                ):
                    return True
                current = current.parent
    return False


def _aidoku_request_imported(content: str) -> bool:
    return any(
        (compact := RustInspection.compact_node(node)).startswith("useaidoku::")
        and (
            re.search(r"imports::net::Request(?:[;,}]|$)", compact) is not None
            or re.search(r"imports::net::\{[^}]*\bRequest\b", compact) is not None
            or re.search(r"imports::\{[^}]*net::\{?[^}]*Request", compact) is not None
        )
        for node in RustInspection.from_content(content).nodes("use_declaration")
    )


def _aidoku_response_imported(content: str) -> bool:
    return any(
        (compact := RustInspection.compact_node(node)).startswith("useaidoku::")
        and (
            re.search(r"imports::net::Response(?:[;,}]|$)", compact) is not None
            or re.search(r"imports::net::\{[^}]*\bResponse\b", compact) is not None
            or re.search(r"imports::\{[^}]*net::\{?[^}]*Response", compact) is not None
        )
        for node in RustInspection.from_content(content).nodes("use_declaration")
    )


def _aidoku_alloc_type_imported(content: str, name: str) -> bool:
    for use in RustInspection.from_content(content).nodes("use_declaration"):
        stack = [use]
        while stack:
            node = stack.pop()
            stack.extend(reversed(node.children))
            if _rust_identifier(node) != name:
                continue
            current = node.parent
            relative_alloc = False
            while current is not None and current != use:
                compact = RustInspection.compact_node(current)
                if compact.startswith("aidoku::alloc::"):
                    return True
                if current.type == "scoped_use_list" and compact.startswith("alloc::{"):
                    relative_alloc = True
                elif (
                    current.type == "scoped_use_list"
                    and compact.startswith("aidoku::{")
                    and relative_alloc
                ):
                    return True
                current = current.parent
    return False


def _aidoku_defaults_get_imported(content: str) -> bool:
    return any(
        "imports::defaults::defaults_get" in RustInspection.compact_node(node)
        for node in RustInspection.from_content(content).nodes("use_declaration")
    )


def _inject_import(content: str, statement: str) -> str:
    crate_attributes = re.match(r"(?:\s*#!\[[^\n]*\]\s*\n)+", content)
    if crate_attributes is None:
        return statement + "\n" + content.lstrip()
    boundary = crate_attributes.end()
    return content[:boundary] + statement + "\n" + content[boundary:]


def _inject_required_aidoku_imports(content: str) -> str:
    inspection = RustInspection.from_content(content)
    identifiers = {
        identifier
        for node in inspection.nodes()
        if (identifier := _rust_identifier(node)) is not None
    }
    declared = set(
        re.findall(
            r"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|type|trait)\s+"
            r"([A-Za-z_]\w*)",
            content,
        )
    )
    missing_root = sorted(
        name
        for name in identifiers & _AIDOKU_ROOT_NAMES - declared
        if re.search(rf"(?<!aidoku::)\b{re.escape(name)}\b", content)
        if not _aidoku_root_imported(content, name)
        and not (
            "register_source!" in content
            and name
            in {
                "BaseUrlProvider",
                "DeepLinkHandler",
                "DynamicFilters",
                "ImageRequestProvider",
                "ListingProvider",
            }
            and f"impl {name}" not in content
        )
    )
    if missing_root:
        content = _inject_import(content, f"use aidoku::{{{', '.join(missing_root)}}};")
    if re.search(r"(?<![:\w])String\b", content) and not _aidoku_alloc_string_imported(content):
        content = _inject_import(content, "use aidoku::alloc::string::String;")
    type_usage = re.sub(r"(?m)^\s*use\s+[^;]+;", "", content)
    for name, path in (("Box", "boxed::Box"), ("Vec", "vec::Vec")):
        if re.search(rf"(?<![:\w]){name}\b", type_usage) and not _aidoku_alloc_type_imported(
            content, name
        ):
            content = _inject_import(content, f"use aidoku::alloc::{path};")
    if re.search(r"(?<![:\w])Result\s*<", content) and not _aidoku_root_imported(content, "Result"):
        content = _inject_import(content, "use aidoku::Result;")
    request_usage = re.sub(r"(?m)^\s*use\s+[^;]+;", "", content)
    has_request_identifier = re.search(r"(?<![:\w])Request\b", request_usage) is not None
    if has_request_identifier and not _aidoku_request_imported(content):
        content = _inject_import(content, "use aidoku::imports::net::Request;")
    has_response_identifier = re.search(r"(?<![:\w])Response\b", request_usage) is not None
    if has_response_identifier and not _aidoku_response_imported(content):
        content = _inject_import(content, "use aidoku::imports::net::Response;")
    defaults_usage = re.sub(r"(?m)^\s*use\s+[^;]+;", "", content)
    if re.search(
        r"(?<![:\w])defaults_get(?:\s*::\s*<|\s*\()", defaults_usage
    ) and not _aidoku_defaults_get_imported(content):
        content = _inject_import(content, "use aidoku::imports::defaults::defaults_get;")
    if re.search(r"(?<![:\w])parse_date\s*\(", defaults_usage) and not any(
        "imports::std::parse_date" in RustInspection.compact_node(node)
        for node in RustInspection.from_content(content).nodes("use_declaration")
    ):
        content = _inject_import(content, "use aidoku::imports::std::parse_date;")
    if ".to_string()" in content and not _aidoku_to_string_imported(content):
        content = _inject_import(content, "use aidoku::alloc::string::ToString;")
    return content


def _normalize_legacy_request_errors(content: str) -> str:
    replacements: list[tuple[int, int, str]] = []
    start = 0
    while (marker := content.find("Result<", start)) >= 0:
        # The pinned Aidoku Result has one generic parameter. Leave qualified
        # result types (for example core::result::Result<T, E>) untouched.
        if marker > 0 and (content[marker - 1].isalnum() or content[marker - 1] in "_:"):
            start = marker + len("Result<")
            continue
        opening = marker + len("Result")
        depth = 0
        paren_depth = 0
        square_depth = 0
        brace_depth = 0
        closing = None
        comma = None
        for index in range(opening, len(content)):
            character = content[index]
            if character == "<":
                depth += 1
            elif character == ">":
                depth -= 1
                if depth == 0:
                    closing = index
                    break
            elif character == "(":
                paren_depth += 1
            elif character == ")":
                paren_depth -= 1
            elif character == "[":
                square_depth += 1
            elif character == "]":
                square_depth -= 1
            elif character == "{":
                brace_depth += 1
            elif character == "}":
                brace_depth -= 1
            elif (
                character == ","
                and depth == 1
                and paren_depth == 0
                and square_depth == 0
                and brace_depth == 0
            ):
                comma = index
        if closing is None:
            break
        if comma is not None:
            replacements.append((marker, closing + 1, content[marker:comma].rstrip() + ">"))
        start = closing + 1
    for begin, end, replacement in reversed(replacements):
        content = content[:begin] + replacement + content[end:]

    content = content.replace("RequestError::new(", "aidoku::AidokuError::message(")
    content = content.replace("RequestError::from(", "aidoku::AidokuError::message(")
    content = re.sub(
        r"\bRequestError::(?P<variant>[A-Za-z_]\w*)\b",
        lambda match: (
            'aidoku::AidokuError::message("request error: ' + match.group("variant") + '")'
        ),
        content,
    )
    if "RequestError" not in re.sub(r"(?m)^\s*use\s+[^;]+;", "", content):
        updated = []
        for node in RustInspection.from_content(content).nodes("use_declaration"):
            original = node.text.decode("utf-8", errors="replace")
            if "RequestError" not in original:
                continue
            normalized = re.sub(r"RequestError\s*,\s*", "", original)
            normalized = re.sub(r",\s*RequestError\b", "", normalized)
            normalized = re.sub(r"\bRequestError\b", "", normalized)
            normalized = re.sub(
                r"\b[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*::\{\s*\}\s*,?",
                "",
                normalized,
            )
            normalized = re.sub(r",(?P<space>\s*),", r",\g<space>", normalized)
            if re.fullmatch(r"use\s+aidoku(?:::[A-Za-z_]\w*)*::\s*;", normalized.strip()):
                normalized = ""
            if normalized != original:
                updated.append((original, normalized))
        for original, normalized in updated:
            content = content.replace(original, normalized, 1)
    return content


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


def _normalize_aidoku_result_errors(content: str) -> str:
    """Convert common std-style String errors into the pinned Aidoku error."""
    replacements: list[tuple[int, int, str]] = []
    inspection = RustInspection.from_content(content)
    for function in inspection.functions:
        signature = function.text[: function.text.find("{")]
        if re.search(r"(?<![:\w])Result\s*<", signature) is None:
            continue
        function_start = function.node.start_byte
        for node in RustInspection.from_content(function.text).nodes("call_expression"):
            callee = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            if callee is None or arguments is None:
                continue
            callee_text = callee.text.decode("utf-8", errors="replace")
            argument_nodes = arguments.named_children

            if callee_text == "Err" and len(argument_nodes) == 1:
                argument = argument_nodes[0].text.decode("utf-8", errors="replace")
                literal_into = re.fullmatch(
                    r'(?P<literal>(?:b|c)?"(?:\\.|[^"\\])*")\.into\(\)',
                    argument,
                )
                if literal_into is not None:
                    replacement = (
                        f"Err(aidoku::AidokuError::message({literal_into.group('literal')}))"
                    )
                elif re.fullmatch(r'(?:b|c)?"(?:\\.|[^"\\])*"', argument) or (
                    argument.startswith("format!(") and argument.endswith(")")
                ):
                    replacement = f"Err(aidoku::AidokuError::message({argument}))"
                else:
                    if re.fullmatch(r"[A-Za-z_]\w*", argument) is None:
                        continue
                    binding = re.search(
                        rf"\blet\s+{re.escape(argument)}(?:\s*:\s*String)?\s*=\s*"
                        r"(?P<value>[\s\S]{1,1200}?);",
                        function.text,
                    )
                    if binding is None or not any(
                        marker in binding.group("value")
                        for marker in ("format!", "String::", ".to_string()", ".join(")
                    ):
                        continue
                    replacement = f"Err(aidoku::AidokuError::message({argument}))"
                replacements.append(
                    (
                        function_start + node.start_byte,
                        function_start + node.end_byte,
                        replacement,
                    )
                )
                continue

            if callee.type != "field_expression" or len(argument_nodes) != 1:
                continue
            field = callee.child_by_field_name("field")
            receiver = callee.child_by_field_name("value")
            if field is None or receiver is None:
                continue
            field_name = field.text.decode("utf-8", errors="replace")
            argument = argument_nodes[0].text.decode("utf-8", errors="replace")
            if field_name == "map_err" and re.fullmatch(
                r"\|[A-Za-z_]\w*\|\s*format!\([\s\S]*\)", argument
            ):
                replacements.append(
                    (
                        function_start + node.start_byte,
                        function_start + node.end_byte,
                        receiver.text.decode("utf-8", errors="replace"),
                    )
                )
            elif field_name == "ok_or" and re.fullmatch(r'(?:b|c)?"(?:\\.|[^"\\])*"', argument):
                replacement = (
                    f"{receiver.text.decode('utf-8', errors='replace')}"
                    ".ok_or_else(|| aidoku::AidokuError::message("
                    f"{argument}))"
                )
                replacements.append(
                    (
                        function_start + node.start_byte,
                        function_start + node.end_byte,
                        replacement,
                    )
                )

    # Tree-sitter offsets are bytes. Generated Rust is overwhelmingly ASCII,
    # but slice encoded bytes so non-ASCII string literals remain safe.
    encoded = content.encode("utf-8")
    for begin, end, replacement in sorted(replacements, reverse=True):
        encoded = encoded[:begin] + replacement.encode("utf-8") + encoded[end:]
    return encoded.decode("utf-8")


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
        optional_fields = (
            {"url", "cover", "description"}
            if target.group("model") == "manga"
            else {"url", "title"}
        )
        if target.group("field") not in optional_fields:
            continue
        expression = right.text.decode("utf-8", errors="replace")
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


def _normalize_select_filter_constructors(content: str) -> str:
    content = re.sub(
        r"(?:aidoku::)?Filter::Header\s*\{\s*title\s*:\s*"
        r"(?P<title>[^,\n]+),?\s*\}",
        lambda match: f"Filter::note({match.group('title').strip()})",
        content,
    )
    content = re.sub(
        r"((?:aidoku::)?Filter::note\(\s*)"
        r'(?P<text>"(?:\\.|[^"\\])*")\.into\(\)(\s*,?\s*\))',
        r"\1\g<text>\3",
        content,
    )
    content = re.sub(
        r"aidoku::Filter::select\(\s*(?P<id>\"(?:\\.|[^\"\\])*\")\s*,\s*"
        r"(?P<title>\"(?:\\.|[^\"\\])*\")\s*,\s*"
        r"(?P<options>[A-Za-z_]\w*)\s*,\s*Some\((?P<ids>[A-Za-z_]\w*)\)\s*\)",
        r"aidoku::SelectFilter { id: \g<id>.into(), title: Some(\g<title>.into()), "
        r"options: \g<options>, ids: Some(\g<ids>), ..Default::default() }.into()",
        content,
    )
    replacements: list[tuple[str, str]] = []
    for node in RustInspection.from_content(content).nodes("call_expression"):
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if function is None or arguments is None or len(arguments.named_children) != 1:
            continue
        function_text = function.text.decode("utf-8", errors="replace")
        argument = arguments.named_children[0].text.decode("utf-8", errors="replace")
        if function_text == "SortFilterDefault::DefaultIndex":
            original = node.text.decode("utf-8", errors="replace")
            replacements.append(
                (original, f"SortFilterDefault {{ index: {argument}, ascending: false }}")
            )
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)

    replacements = []
    for node in RustInspection.from_content(content).nodes("struct_expression"):
        name = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name is None or body is None:
            continue
        if name.text.decode("utf-8", errors="replace").rsplit("::", 1)[-1] != ("SortFilterDefault"):
            continue
        fields = {
            field.text.decode("utf-8", errors="replace")
            for child in body.named_children
            if (field := child.child_by_field_name("field")) is not None
        }
        if "index" not in fields or "ascending" in fields:
            continue
        original = node.text.decode("utf-8", errors="replace")
        body_text = body.text.decode("utf-8", errors="replace")
        inner = body_text[1:-1].rstrip()
        separator = "" if inner.endswith(",") else ","
        replacement_body = "{" + inner + separator + " ascending: false }"
        replacements.append((original, original.replace(body_text, replacement_body, 1)))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)

    replacements = []
    legacy_types = {
        "Check": "CheckFilter",
        "Checkbox": "CheckFilter",
        "MultiSelect": "MultiSelectFilter",
        "Select": "SelectFilter",
        "Sort": "SortFilter",
    }
    for node in RustInspection.from_content(content).nodes("call_expression"):
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if function is None or arguments is None or len(arguments.named_children) != 1:
            continue
        function_text = function.text.decode("utf-8", errors="replace")
        match = re.fullmatch(r"(?:aidoku::)?Filter::([A-Za-z]+)", function_text)
        if match is None or match.group(1) not in legacy_types:
            continue
        argument = arguments.named_children[0].text.decode("utf-8", errors="replace")
        expected = legacy_types[match.group(1)]
        if re.match(rf"(?:aidoku::)?{expected}\s*\{{", argument) is None:
            continue
        original = node.text.decode("utf-8", errors="replace")
        replacements.append((original, f"Filter::from({argument})"))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    return content


def _normalize_legacy_filter_fields(content: str) -> str:
    content = content.replace("CheckboxFilter", "CheckFilter")
    content = content.replace("FilterValue::Checkbox", "FilterValue::Check")
    content = re.sub(
        r"\bCheckbox\s*\{(?P<body>[^}]*)\}",
        r"Check {\g<body>}",
        content,
    )
    content = re.sub(
        r"(?P<prefix>(?:FilterValue::)?Check\s*\{[^}]*\bid\s*,\s*)checked\b",
        r"\g<prefix>value",
        content,
    )
    content = re.sub(r"\*checked\b", "*value > 0", content)

    replacements: list[tuple[str, str]] = []
    for node in RustInspection.from_content(content).nodes("struct_expression"):
        name = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name is None or body is None:
            continue
        full_type_name = name.text.decode("utf-8", errors="replace")
        if full_type_name.startswith("DeepLinkResult::"):
            continue
        type_name = full_type_name.rsplit("::", 1)[-1]
        if type_name not in {"CheckFilter", "SortFilter", "SelectFilter"}:
            continue
        original = node.text.decode("utf-8", errors="replace")
        normalized = original
        field_values = {
            field.text.decode("utf-8", errors="replace"): value.text.decode(
                "utf-8", errors="replace"
            )
            for child in body.named_children
            if (field := child.child_by_field_name("field")) is not None
            and (value := child.child_by_field_name("value")) is not None
        }
        if type_name == "CheckFilter":
            normalized = re.sub(
                r"\bdefault\s*:\s*(?P<value>true|false)\s*,",
                r"default: Some(\g<value>),",
                normalized,
            )
        elif type_name == "SelectFilter":
            normalized = re.sub(r"\bdefault_id\s*:", "default:", normalized)
        else:
            normalized = re.sub(r"\bvalues\s*:", "options:", normalized)
            default = re.search(r"\bdefault_index\s*:\s*(?P<value>[^,\n]+)\s*,", normalized)
            ascending = re.search(
                r"\bascending\s*:\s*(?:Some\()?(?P<value>true|false)(?:\))?\s*,",
                normalized,
            )
            if default is not None:
                ascending_value = ascending.group("value") if ascending is not None else "false"
                normalized = normalized.replace(
                    default.group(0),
                    "default: Some(aidoku::SortFilterDefault { "
                    f"index: {default.group('value').strip()}, ascending: {ascending_value} }}),",
                    1,
                )
            if ascending is not None:
                normalized = normalized.replace(ascending.group(0), "", 1)
        id_value = field_values.get("id")
        if id_value is not None and not id_value.strip().endswith((".into()", ".to_string()")):
            normalized = normalized.replace(
                f"id: {id_value}",
                f"id: {id_value}.into()",
                1,
            )
        title_value = field_values.get("title")
        if title_value is not None:
            title = title_value.strip()
            if title not in {"None"}:
                if title.startswith("Some(") and title.endswith(")"):
                    inner = title[5:-1].strip()
                    if not inner.endswith((".into()", ".to_string()")):
                        title = f"Some({inner}.into())"
                elif not title.endswith((".into()", ".to_string()")):
                    title = f"Some({title}.into())"
                normalized = normalized.replace(f"title: {title_value}", f"title: {title}", 1)
        if normalized != original:
            replacements.append((original, normalized))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    return content


def _normalize_legacy_page_context(content: str) -> str:
    content = re.sub(
        r"(?m)^(?P<indent>[ \t]*)let\s+(?P<name>[A-Za-z_]\w*)\s*=\s*"
        r"serde_json::json!\(\s*\{\s*(?P<key>\"(?:\\.|[^\"\\])*\")\s*:\s*"
        r"(?P<value>[\s\S]{1,800}?),?\s*\}\s*\)\s*\.to_string\(\)\s*;",
        lambda match: (
            f"{match.group('indent')}let mut {match.group('name')} = PageContext::new();\n"
            f"{match.group('indent')}{match.group('name')}.insert("
            f"{match.group('key')}.into(), {match.group('value').strip()});"
        ),
        content,
    )
    return re.sub(
        r"(?P<indent>[ \t]*)if\s+let\s+Ok\([A-Za-z_]\w*\)\s*=\s*"
        r"serde_json::from_str::<serde_json::Value>\(&(?P<context>[A-Za-z_]\w*)\.0\)\s*\{\s*"
        r"if\s+let\s+Some\((?P<value>[A-Za-z_]\w*)\)\s*=\s*"
        r"[A-Za-z_]\w*\.get\((?P<key>\"(?:\\.|[^\"\\])*\")\)"
        r"\.and_then\(\|[A-Za-z_]\w*\|\s*[A-Za-z_]\w*\.as_str\(\)\)\s*\{"
        r"(?P<body>[\s\S]{1,600}?)\}\s*\}",
        lambda match: (
            f"{match.group('indent')}if let Some({match.group('value')}) = "
            f"{match.group('context')}.get({match.group('key')}) {{"
            f"{match.group('body')}\n{match.group('indent')}}}"
        ),
        content,
    )


def _normalize_page_url_context(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        normalized = function.text
        contexts = {
            match.group("context")
            for match in re.finditer(
                r"PageContext::from\(\[\(\s*\"referer\"\.into\(\),\s*"
                r"(?P<context>[A-Za-z_]\w*)\.clone\(\)\s*\)\]\)",
                normalized,
            )
        }
        for context in contexts:
            normalized = re.sub(
                rf"\blet\s+(?P<mut>mut\s+)?{re.escape(context)}\s*=\s*"
                r"(?P<owner>[A-Za-z_]\w*)\.url\.clone\(\)\s*;",
                lambda match, context=context: (
                    f"let {match.group('mut') or ''}{context} = "
                    f"{match.group('owner')}.url.clone().unwrap_or_else(|| "
                    f"{match.group('owner')}.key.clone());"
                ),
                normalized,
            )
        if normalized != function.text:
            replacements.append((function.text, normalized))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)

    replacements: list[tuple[int, int, str]] = []
    for node in RustInspection.from_content(content).nodes("call_expression"):
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if function is None or arguments is None or len(arguments.named_children) != 2:
            continue
        if function.text.decode("utf-8", errors="replace") != "PageContent::url_context":
            continue
        context = arguments.named_children[1]
        context_text = context.text.decode("utf-8", errors="replace")
        context_name = context_text.removesuffix(".clone()")
        if re.fullmatch(r"[A-Za-z_]\w*", context_name) is None:
            continue
        owner = node
        while owner.parent is not None and owner.type != "function_item":
            owner = owner.parent
        if owner.type != "function_item":
            continue
        function_text = owner.text.decode("utf-8", errors="replace")
        signature = function_text.split("{", 1)[0]
        string_context = (
            re.search(
                rf"\b{re.escape(context_name)}\s*:\s*(?:aidoku::alloc::)?String\b",
                signature,
            )
            is not None
        )
        if not string_context:
            binding = re.search(
                rf"\blet\s+(?:mut\s+)?{re.escape(context_name)}\s*=\s*"
                r"(?P<value>[^;]+);",
                function_text,
            )
            string_context = binding is not None and any(
                marker in binding.group("value")
                for marker in ("url", "format!", "String::", ".to_string()")
            )
        if not string_context:
            continue
        replacements.append(
            (
                context.start_byte,
                context.end_byte,
                f'PageContext::from([("referer".into(), {context_text})])',
            )
        )
    encoded = content.encode("utf-8")
    for begin, end, replacement in reversed(replacements):
        encoded = encoded[:begin] + replacement.encode("utf-8") + encoded[end:]
    return encoded.decode("utf-8")


def _normalize_image_request_result(content: str) -> str:
    content = re.sub(
        r"(?P<context>[A-Za-z_]\w*)\s*\.map\(\s*\|(?P<value>[A-Za-z_]\w*)\|\s*"
        r"(?P=value)\.referer\s*\)",
        lambda match: (
            f'{match.group("context")}.as_ref().and_then(|value| value.get("referer")).cloned()'
        ),
        content,
    )
    content = re.sub(
        r"let\s+(?P<referer>[A-Za-z_]\w*)\s*=\s*"
        r"(?P<context>[A-Za-z_]\w*)\.unwrap_or_else\(\|\|\s*"
        r"(?P<fallback>[A-Za-z_]\w*)\.into\(\)\);",
        lambda match: (
            f"let {match.group('referer')} = {match.group('context')}.as_ref()"
            '.and_then(|value| value.get("referer")).map(String::as_str)'
            f".unwrap_or({match.group('fallback')});"
        ),
        content,
    )
    edits: list[tuple[int, int, bytes]] = []
    for function in RustInspection.from_content(content).functions:
        if function.name != "get_image_request":
            continue
        for node in RustInspection.from_content(function.text).nodes("call_expression"):
            callee = node.child_by_field_name("function")
            if callee is None:
                continue
            field_expression = (
                callee.child_by_field_name("function")
                if callee.type == "generic_function"
                else callee
            )
            if field_expression is None or field_expression.type != "field_expression":
                continue
            field = field_expression.child_by_field_name("field")
            receiver = field_expression.child_by_field_name("value")
            if field is None or receiver is None:
                continue
            field_name = field.text.decode("utf-8", errors="replace")
            begin = function.node.start_byte + node.start_byte
            end = function.node.start_byte + node.end_byte
            if field_name == "send_error_type":
                statement = node.parent
                while statement is not None and statement.type != "expression_statement":
                    statement = statement.parent
                if statement is not None:
                    begin = function.node.start_byte + statement.start_byte
                    end = function.node.start_byte + statement.end_byte
                    edits.append((begin, end, b""))
            elif (
                field_name == "into"
                and node.parent is not None
                and node.parent.type == "block"
                and "Request::get" in receiver.text.decode("utf-8", errors="replace")
            ):
                edits.append((begin, end, b"Ok(" + receiver.text + b")"))
    encoded = content.encode("utf-8")
    for begin, end, replacement in sorted(edits, reverse=True):
        encoded = encoded[:begin] + replacement + encoded[end:]
    content = encoded.decode("utf-8")

    edits = []
    for function in RustInspection.from_content(content).functions:
        signature = function.text[: function.text.find("{")]
        if re.search(r"->\s*(?:aidoku::)?Result\s*<\s*Request\s*>", signature) is None:
            continue
        for node in RustInspection.from_content(function.text).nodes("try_expression"):
            expression = node.named_child(0)
            if expression is None or expression.type != "call_expression":
                continue
            callee = expression.child_by_field_name("function")
            if callee is None or callee.type != "field_expression":
                continue
            field = callee.child_by_field_name("field")
            if field is None or field.text != b"header":
                continue
            edits.append(
                (
                    function.node.start_byte + node.start_byte,
                    function.node.start_byte + node.end_byte,
                    expression.text,
                )
            )
    encoded = content.encode("utf-8")
    for begin, end, replacement in sorted(edits, reverse=True):
        encoded = encoded[:begin] + replacement + encoded[end:]
    return encoded.decode("utf-8")


def _normalize_result_request_tails(content: str) -> str:
    edits: list[tuple[int, int, bytes]] = []
    for function in RustInspection.from_content(content).functions:
        signature = function.text[: function.text.find("{")]
        if re.search(r"->\s*(?:aidoku::)?Result\s*<\s*Request\s*>", signature) is None:
            continue
        for call in RustInspection.from_content(function.text).nodes("call_expression"):
            if (
                call.parent is None
                or call.parent.type != "block"
                or not call.text.decode("utf-8", errors="replace").lstrip().startswith("Request::")
            ):
                continue
            edits.append(
                (
                    function.node.start_byte + call.start_byte,
                    function.node.start_byte + call.end_byte,
                    b"Ok(" + call.text + b")",
                )
            )
    encoded = content.encode("utf-8")
    for begin, end, replacement in sorted(edits, reverse=True):
        encoded = encoded[:begin] + replacement + encoded[end:]
    return encoded.decode("utf-8")


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


def _normalize_detail_partial_move(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        if function.name != "get_manga_update":
            continue
        local = RustInspection.from_content(function.text)
        details = None
        chapters = None
        for branch in local.nodes("if_expression"):
            text = branch.text.decode("utf-8", errors="replace")
            if (
                details is None
                and re.search(r"\bdetail\.[A-Za-z_]\w*", text)
                and re.search(r"chapters\s*=\s*manga\.chapters", text)
            ):
                details = branch
            elif chapters is None and "&detail" in text:
                chapters = branch
        if details is None or chapters is None:
            branches = list(local.nodes("if_expression"))
            for candidate in branches:
                candidate_text = candidate.text.decode("utf-8", errors="replace")
                moved = re.findall(
                    r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\.comic\b",
                    candidate_text,
                )
                if not moved:
                    continue
                borrowed = next(
                    (
                        later
                        for later in branches
                        if later.start_byte > candidate.start_byte
                        and "chapters" in later.text.decode("utf-8", errors="replace")
                        and any(
                            f"&{owner}" in later.text.decode("utf-8", errors="replace")
                            for owner in moved
                        )
                    ),
                    None,
                )
                if borrowed is not None:
                    details = candidate
                    chapters = borrowed
                    break
        if details is None or chapters is None or details.end_byte >= chapters.start_byte:
            continue
        encoded = function.text.encode("utf-8")
        between = encoded[details.end_byte : chapters.start_byte]
        normalized = (
            encoded[: details.start_byte]
            + encoded[chapters.start_byte : chapters.end_byte]
            + between
            + encoded[details.start_byte : details.end_byte]
            + encoded[chapters.end_byte :]
        ).decode("utf-8")
        replacements.append((function.text, normalized))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    return content


def _normalize_manga_replacement_chapters(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).named("get_manga_update"):
        if "c2a_chapters" in function.text:
            continue
        branches = list(RustInspection.from_content(function.text).nodes("if_expression"))
        chapters = next(
            (
                branch
                for branch in branches
                if re.search(
                    r"\bmanga\.chapters\s*=",
                    branch.text.decode("utf-8", errors="replace"),
                )
            ),
            None,
        )
        details = next(
            (
                branch
                for branch in branches
                if branch.start_byte > (chapters.start_byte if chapters is not None else -1)
                and re.search(
                    r"(?m)^[ \t]*manga\s*=\s*[^;]+;",
                    branch.text.decode("utf-8", errors="replace"),
                )
                and "chapters" not in branch.text.decode("utf-8", errors="replace")
            ),
            None,
        )
        if chapters is None or details is None:
            continue
        details_text = details.text.decode("utf-8", errors="replace")
        normalized_details = re.sub(
            r"(?m)^(?P<indent>[ \t]*)manga\s*=\s*(?P<value>[^;]+);",
            lambda match: (
                f"{match.group('indent')}let c2a_chapters = manga.chapters;\n"
                f"{match.group('indent')}manga = {match.group('value')};\n"
                f"{match.group('indent')}manga.chapters = c2a_chapters;"
            ),
            details_text,
            count=1,
        )
        replacements.append(
            (function.text, function.text.replace(details_text, normalized_details, 1))
        )
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    return content


def _normalize_deep_link_defaults(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for node in RustInspection.from_content(content).nodes("struct_expression"):
        name = node.child_by_field_name("name")
        if name is None or not name.text.decode("utf-8", errors="replace").startswith(
            "DeepLinkResult::"
        ):
            continue
        original = node.text.decode("utf-8", errors="replace")
        normalized = re.sub(r"(?m)^\s*\.\.Default::default\(\)\s*,?\s*\n?", "", original)
        if normalized != original:
            replacements.append((original, normalized))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    return content


def _normalize_request_builder_helpers(
    content: str,
    known_helpers: set[str] | None = None,
) -> str:
    helper_names = set(known_helpers or ())
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        text = function.text
        if "Request::get" not in text or ".header(" not in text:
            continue
        header = re.search(r"->\s*Request\s*\{", text)
        binding = re.search(
            r"let\s+mut\s+(?P<name>[A-Za-z_]\w*)\s*=\s*Request::get\((?P<url>[^;]+)\);",
            text,
        )
        if header is None or binding is None:
            continue
        normalized = text[: header.start()] + "-> Result<Request> {" + text[header.end() :]
        normalized = normalized.replace(binding.group(0), binding.group(0)[:-1] + "?;")
        variable = binding.group("name")
        normalized = re.sub(
            rf"(?m)^(?P<indent>[ \t]*){re.escape(variable)}\s*\n\}}$",
            rf"\g<indent>Ok({variable})\n}}",
            normalized,
        )
        if normalized != text:
            helper_names.add(function.name)
            replacements.append((text, normalized))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    for name in helper_names:
        content = re.sub(
            rf"(?P<call>\b{re.escape(name)}\([^;\n]+\))(?=\.send\()",
            r"\g<call>?",
            content,
        )
        content = re.sub(
            rf"(?P<prefix>=\s*)(?P<call>\b{re.escape(name)}\([^;\n]+\))(?P<suffix>\s*;)",
            r"\g<prefix>\g<call>?\g<suffix>",
            content,
        )
    return content


def _normalize_parse_date_option_patterns(content: str) -> str:
    content = re.sub(
        r"if\s+let\s+Ok\((?P<value>[A-Za-z_]\w*)\)\s*=\s*"
        r"(?P<call>(?:aidoku::)?imports::std::parse_(?:local_)?date\([^\n]+\))",
        r"if let Some(\g<value>) = \g<call>",
        content,
    )
    return re.sub(
        r"(?P<call>(?:aidoku::imports::std::)?parse_(?:local_)?date\([^;]{1,500}?\))"
        r"\s*\.or_else\(\s*\|_\|",
        r"\g<call>.or_else(||",
        content,
    )


def _normalize_optional_chapter_dates(content: str) -> str:
    content = re.sub(
        r"(?P<prefix>(?:aidoku::imports::std::)?parse_date\([^;]*),\s*None\s*,?\s*\)",
        r"\g<prefix>)",
        content,
    )
    replacements: list[tuple[str, str]] = []
    binding_pattern = re.compile(
        r"let\s+(?P<name>[A-Za-z_]\w*)\s*=\s*"
        r"(?P<call>(?:aidoku::imports::std::)?parse_date\([\s\S]{0,500}?\))\s*"
        r"\.ok_or_else\([\s\S]{0,500}?\)\?;"
    )
    for function in RustInspection.from_content(content).functions:
        normalized = function.text
        for match in binding_pattern.finditer(function.text):
            name = match.group("name")
            if (
                re.search(
                    rf"\bdate_uploaded\s*:\s*Some\(\s*{re.escape(name)}\s*\)",
                    function.text,
                )
                is None
            ):
                continue
            normalized = normalized.replace(
                match.group(0),
                f"let {name} = {match.group('call')};",
                1,
            )
            normalized = re.sub(
                rf"\bdate_uploaded\s*:\s*Some\(\s*{re.escape(name)}\s*\)",
                f"date_uploaded: {name}",
                normalized,
                count=1,
            )
        direct_option = re.compile(
            r"let\s+(?P<name>[A-Za-z_]\w*)\s*=\s*"
            r"(?P<call>(?:aidoku::imports::std::)?parse_date\([^;]{1,500}\))\?;"
        )
        for match in direct_option.finditer(function.text):
            name = match.group("name")
            if (
                re.search(
                    rf"\bdate_uploaded\s*:\s*Some\(\s*{re.escape(name)}\s*\)",
                    function.text,
                )
                is None
            ):
                continue
            normalized = normalized.replace(
                match.group(0), f"let {name} = {match.group('call')};", 1
            )
            normalized = re.sub(
                rf"\bdate_uploaded\s*:\s*Some\(\s*{re.escape(name)}\s*\)",
                f"date_uploaded: {name}",
                normalized,
                count=1,
            )
        if normalized != function.text:
            replacements.append((function.text, normalized))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    return content


def _normalize_chapter_group_scope(content: str) -> str:
    extension = re.search(
        r"(?P<collection>[A-Za-z_]\w*)\.extend\((?P<list>[A-Za-z_]\w*)\);",
        content,
    )
    if extension is not None and re.search(r"\blet\s+group_name\s*=", content):
        collection = extension.group("collection")
        list_name = extension.group("list")
        content = content.replace(
            extension.group(0),
            f"{collection}.extend({list_name}.into_iter()"
            ".map(|chapter| (chapter, group_name.clone())));",
            1,
        )
        content = re.sub(
            rf"{re.escape(collection)}\.sort_by_key\(\|item\|\s*"
            r"core::cmp::Reverse\(item\.index\)\)",
            f"{collection}.sort_by_key(|item| core::cmp::Reverse(item.0.index))",
            content,
        )
        content = re.sub(
            rf"(?P<prefix>{re.escape(collection)}\.into_iter\(\)\.enumerate\(\)"
            r"\.map\(\|\()(?P<index>[A-Za-z_]\w*)\s*,\s*"
            r"(?P<chapter>[A-Za-z_]\w*)(?P<suffix>\)\|)",
            r"\g<prefix>\g<index>, (\g<chapter>, group_name)\g<suffix>",
            content,
        )
    content = re.sub(
        r"(?P<prefix>\bif\s+(?:[A-Za-z_]\w*\.)?total)\s*>=\s*"
        r"(?P<right>(?:[A-Za-z_]\w*\.)?offset\s*\+\s*"
        r"(?:[A-Za-z_]\w*\.)?limit)(?P<suffix>\s*\{\s*break\s*;)",
        r"\g<prefix> <= \g<right>\g<suffix>",
        content,
    )
    return content


def _normalize_safe_std_paths(content: str, *, remove_extern_std: bool) -> str:
    """Project allocation/core-only std paths into the no_std Aidoku runtime."""
    if remove_extern_std:
        content = re.sub(r"(?m)^\s*extern\s+crate\s+std\s*;\s*\n?", "", content)
    content = re.sub(r"(?<!aidoku::)\balloc::vec!", "vec!", content)
    collection_aliases = {"HashMap": "BTreeMap", "HashSet": "BTreeSet"}
    for source, target in collection_aliases.items():
        marker = f"std::collections::{source}"
        if marker in content:
            content = content.replace(marker, f"aidoku::alloc::collections::{target}")
            content = re.sub(rf"\b{source}\b", target, content)
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


def _normalize_graphql_body_fragment(content: str) -> str:
    if "#{body}" not in content or "COMIC_BODY" not in content:
        return content

    def wrap(match: re.Match[str]) -> str:
        body = match.group("body")
        stripped = body.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            return match.group(0)
        return match.group("prefix") + "\n{\n" + body.strip("\n") + "\n}\n" + match.group("suffix")

    return re.sub(
        r'(?P<prefix>(?:pub\s+)?const\s+COMIC_BODY\s*:\s*&str\s*=\s*r#")'
        r'(?P<body>[\s\S]*?)(?P<suffix>"#\s*;)',
        wrap,
        content,
    )


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


def _normalize_utf8_slice_loops(content: str) -> str:
    """Iterate valid UTF-8 boundaries before slicing a string by an index."""
    return re.sub(
        r"(?m)^(?P<indent>[ \t]*)let\s+(?P<bytes>[A-Za-z_]\w*)\s*=\s*"
        r"(?P<text>[A-Za-z_]\w*)\.as_bytes\(\);\s*\n"
        r"(?P=indent)for\s+(?P<index>[A-Za-z_]\w*)\s+in\s+0\.\."
        r"(?P=bytes)\.len\(\)\s*\{",
        r"\g<indent>for (\g<index>, _) in \g<text>.char_indices() {",
        content,
    )


def _graphql_field_end(query: str, start: int) -> int | None:
    opening = query.find("{{", start)
    if opening < 0:
        return None
    depth = 0
    index = opening
    while index < len(query) - 1:
        token = query[index : index + 2]
        if token == "{{":
            depth += 1
            index += 2
            continue
        if token == "}}":
            depth -= 1
            index += 2
            if depth == 0:
                return index
            continue
        index += 1
    return None


def _split_graphql_manga_query(query: str) -> tuple[str, str] | None:
    detail_start = query.find("comicById")
    chapters_start = query.find("chaptersByComicId")
    if detail_start < 0 or chapters_start < 0:
        return None
    detail_end = _graphql_field_end(query, detail_start)
    chapters_end = _graphql_field_end(query, chapters_start)
    if detail_end is None or chapters_end is None:
        return None
    first_start = min(detail_start, chapters_start)
    last_end = max(detail_end, chapters_end)
    prefix = query[:first_start]
    suffix = query[last_end:]
    detail = prefix + query[detail_start:detail_end] + suffix
    chapters = prefix + query[chapters_start:chapters_end] + suffix
    return detail, chapters


def _normalize_graphql_manga_update_projection(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        if function.name != "manga_query" or "(true, false)" in function.text:
            continue
        query_match = re.search(
            r'format!\(\s*(?P<literal>"(?:\\.|[^"\\])*")\s*\)',
            function.text,
        )
        if query_match is None:
            continue
        try:
            query = json.loads(query_match.group("literal"))
        except json.JSONDecodeError:
            continue
        projections = _split_graphql_manga_query(query)
        if projections is None:
            continue
        details_query, chapters_query = projections
        signature = re.search(
            r"(?P<head>fn\s+manga_query\s*\((?P<params>[\s\S]*?)\))"
            r"(?P<return>\s*->\s*String)",
            function.text,
        )
        if signature is None or "needs_details" in signature.group("params"):
            continue
        params = signature.group("params").rstrip()
        separator = " " if params.endswith(",") else ", "
        new_signature = (
            f"fn manga_query({params}{separator}"
            f"needs_details: bool, needs_chapters: bool){signature.group('return')}"
        )
        combined_literal = json.dumps(query, ensure_ascii=False)
        details_literal = json.dumps(details_query, ensure_ascii=False)
        chapters_literal = json.dumps(chapters_query, ensure_ascii=False)
        projection = (
            "match (needs_details, needs_chapters) {\n"
            f"            (true, true) => format!({combined_literal}),\n"
            f"            (true, false) => format!({details_literal}),\n"
            f"            (false, true) => format!({chapters_literal}),\n"
            "            _ => String::new(),\n"
            "        }"
        )
        normalized = function.text.replace(signature.group(0), new_signature, 1)
        normalized = normalized.replace(query_match.group(0), projection, 1)
        replacements.append((function.text, normalized))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)

    replacements = []
    for function in RustInspection.from_content(content).named("get_manga_update"):
        if "needs_details" not in function.text or "needs_chapters" not in function.text:
            continue
        normalized = re.sub(
            r"self\.manga_query\((?P<args>[^(),\n]+)\)",
            lambda match: (
                f"self.manga_query({match.group('args').strip()}, needs_details, needs_chapters)"
            ),
            function.text,
        )
        if normalized != function.text:
            replacements.append((function.text, normalized))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    return content


def _normalize_graphql_request_body(content: str) -> str:
    content = re.sub(
        r"(?m)^(?P<indent>[ \t]*)let\s+mut\s+(?P<body>[A-Za-z_]\w*)\s*=\s*"
        r"PageContext::new\(\);\s*\n"
        r"(?P=indent)(?P=body)\.insert\("
        r"(?P<query_key>\"(?:\\.|[^\"\\])*\")\.into\(\),\s*"
        r"(?P<query>[A-Za-z_]\w*)\s*,\s*"
        r"(?P<variables_key>\"(?:\\.|[^\"\\])*\")\s*:\s*"
        r"(?P<variables>[A-Za-z_]\w*)\s*\);",
        lambda match: (
            f"{match.group('indent')}let {match.group('body')} = serde_json::json!({{ "
            f"{match.group('query_key')}: {match.group('query')}, "
            f"{match.group('variables_key')}: {match.group('variables')} }});"
        ),
        content,
    )
    return re.sub(
        r"Request::(?P<method>post|put|patch)\(\s*(?P<url>[^,\n()]+)\s*,\s*"
        r"(?P<body>body|payload)\s*\)\?",
        lambda match: (
            f"Request::{match.group('method')}({match.group('url').strip()})?"
            f".body({match.group('body')}.to_string().as_bytes())"
        ),
        content,
    )


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
        if full_type_name.startswith("DeepLinkResult::"):
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


def _normalize_partial_move_pagination(content: str) -> str:
    pattern = re.compile(
        r"(?P<indent>^[ \t]*)(?P<assignment>let\s+(?P<list>[A-Za-z_][A-Za-z0-9_]*)"
        r"(?:\s*:[^=;]+)?\s*=\s*(?P<result>[A-Za-z_][A-Za-z0-9_]*)\s*\n"
        r"[ \t]*\.list\s*\n[ \t]*\.into_iter\(\)[\s\S]{0,2500}?\.collect\(\);)"
        r"(?P<middle>[\s\S]{0,800}?)(?P<field>has_next_page:\s*)"
        r"(?P=result)\.has_next\(\)",
        re.MULTILINE,
    )

    def replace(match: re.Match[str]) -> str:
        variable = f"{match.group('list')}_has_next"
        return (
            f"{match.group('indent')}let {variable} = {match.group('result')}.has_next();\n"
            f"{match.group('indent')}{match.group('assignment')}"
            f"{match.group('middle')}{match.group('field')}{variable}"
        )

    nested = re.compile(
        r"(?P<indent>^[ \t]*)(?P<assignment>let\s+(?P<list>[A-Za-z_][A-Za-z0-9_]*)"
        r"(?:\s*:[^=;]+)?\s*=\s*(?P<base>[A-Za-z_][A-Za-z0-9_]*)"
        r"\s*\.\s*results\s*\.\s*list\s*\.\s*into_iter\(\)"
        r"[\s\S]{0,2500}?\.collect\(\);)(?P<middle>[\s\S]{0,800}?)"
        r"(?P<field>has_next_page:\s*)(?P=base)\.results\.has_next\(\)",
        re.MULTILINE,
    )

    def replace_nested(match: re.Match[str]) -> str:
        variable = f"{match.group('list')}_has_next"
        return (
            f"{match.group('indent')}let {variable} = {match.group('base')}.results.has_next();\n"
            f"{match.group('indent')}{match.group('assignment')}"
            f"{match.group('middle')}{match.group('field')}{variable}"
        )

    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        normalized = pattern.sub(replace, function.text)
        normalized = nested.sub(replace_nested, normalized)
        if normalized != function.text:
            replacements.append((function.text, normalized))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    return content


def _normalize_collection_len_after_move(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        normalized = function.text
        move = re.search(
            r"(?m)^(?P<indent>\s*)for\s+[^\n]+\s+in\s+"
            r"(?P<collection>[A-Za-z_]\w*)\s*\{",
            normalized,
        )
        if move is None:
            continue
        collection = move.group("collection")
        if f"{collection}.len()" not in normalized[move.end() :]:
            continue
        variable = f"{collection}_len"
        insertion = f"{move.group('indent')}let {variable} = {collection}.len();\n"
        normalized = normalized[: move.start()] + insertion + normalized[move.start() :]
        tail_start = move.start() + len(insertion)
        normalized = normalized[:tail_start] + normalized[tail_start:].replace(
            f"{collection}.len()", variable
        )
        replacements.append((function.text, normalized))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    return content


def _normalize_partial_move_loop_pagination(content: str) -> str:
    pattern = re.compile(
        r"(?P<indent>^[ \t]*)(?P<loop>for\s+[^\n]+\s+in\s+"
        r"(?P<result>[A-Za-z_][A-Za-z0-9_]*)\.list\s*\{[\s\S]{0,5000}?)"
        r"(?P<condition>if\s+!)"
        r"(?P=result)\.has_next\(\)",
        re.MULTILINE,
    )

    def replace(match: re.Match[str]) -> str:
        variable = f"{match.group('result')}_has_next"
        return (
            f"{match.group('indent')}let {variable} = {match.group('result')}.has_next();\n"
            f"{match.group('indent')}{match.group('loop')}"
            f"{match.group('condition')}{variable}"
        )

    return pattern.sub(replace, content)


def _normalize_moved_key_then_borrowed_url(content: str) -> str:
    return re.sub(
        r"(?P<field>\bkey:\s*)(?P<value>[A-Za-z_][A-Za-z0-9_]*)(?P<comma>,\s*\n"
        r"[ \t]*url:\s*Some\([^\n]{0,300}&(?P=value)\b)",
        r"\g<field>\g<value>.clone()\g<comma>",
        content,
    )


def _normalize_select_filter_structs(content: str) -> str:
    return re.sub(
        r"(?:aidoku::)?Filter::Select\s*\{"
        r"(?P<body>[\s\S]{0,3000}?\.\.Default::default\(\)\s*)\}",
        r"aidoku::SelectFilter {\g<body>}.into()",
        content,
    )


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


def _normalize_discarded_enumerate_index(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for node in RustInspection.from_content(content).nodes("for_expression"):
        pattern = node.child_by_field_name("pattern")
        value = node.child_by_field_name("value")
        body = node.child_by_field_name("body")
        if pattern is None or value is None or body is None:
            continue
        pattern_text = pattern.text.decode("utf-8", errors="replace")
        value_text = value.text.decode("utf-8", errors="replace")
        body_text = body.text.decode("utf-8", errors="replace")
        pair = re.fullmatch(
            r"\(\s*(?P<index>[A-Za-z_][A-Za-z0-9_]*)\s*,\s*"
            r"(?P<item>[A-Za-z_][A-Za-z0-9_]*)\s*\)",
            pattern_text,
        )
        if pair is None or not value_text.endswith(".enumerate()"):
            continue
        index = pair.group("index")
        if not index.startswith("_") and re.search(rf"\b{re.escape(index)}\b", body_text):
            continue
        original = node.text.decode("utf-8", errors="replace")
        replacement = (
            f"for {pair.group('item')} in {value_text.removesuffix('.enumerate()')} {body_text}"
        )
        replacements.append((original, replacement))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    iterator_pattern = re.compile(
        r"(?P<prefix>\.enumerate\(\)\s*\.map\(\|\(\s*"
        r"(?P<index>[A-Za-z_]\w*)\s*,\s*"
        r"(?P<item>\([^|]+\)|[A-Za-z_]\w*)\s*\)\|\s*\{)"
        r"(?P<body>[\s\S]{0,3000}?)(?P<suffix>\}\))"
    )

    def replace_iterator(match: re.Match[str]) -> str:
        if re.search(rf"\b{re.escape(match.group('index'))}\b", match.group("body")):
            return match.group(0)
        return f".map(|{match.group('item')}| {{{match.group('body')}{match.group('suffix')}"

    content = iterator_pattern.sub(replace_iterator, content)
    return content


def _normalize_identical_if_branches(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for node in RustInspection.from_content(content).nodes("if_expression"):
        condition = node.child_by_field_name("condition")
        consequence = node.child_by_field_name("consequence")
        alternative = node.child_by_field_name("alternative")
        if condition is None or consequence is None or alternative is None:
            continue
        alternative_blocks = [
            child for child in alternative.named_children if child.type == "block"
        ]
        if consequence.type != "block" or len(alternative_blocks) != 1:
            continue
        alternative_block = alternative_blocks[0]
        if RustInspection.compact_node(consequence) != RustInspection.compact_node(
            alternative_block
        ):
            continue
        original = node.text.decode("utf-8", errors="replace")
        condition_text = condition.text.decode("utf-8", errors="replace")
        branch = consequence.text.decode("utf-8", errors="replace")[1:-1].strip()
        replacements.append((original, f"{{ let _ = {condition_text}; {branch} }}"))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    return content


def _normalize_filter_match_predicate(content: str) -> str:
    return re.sub(
        r"find\(\|(?P<item>[A-Za-z_][A-Za-z0-9_]*)\|\s*match\s+(?P=item)\s*\{\s*"
        r"FilterValue::Select\s*\{\s*id:\s*(?P<found>[A-Za-z_][A-Za-z0-9_]*),\s*"
        r"value\s*\}\s*if\s*(?P=found)\s*==\s*(?P<wanted>[A-Za-z_][A-Za-z0-9_]*)"
        r"\s*=>\s*true,\s*_\s*=>\s*false,?\s*\}\)",
        r"find(|\g<item>| matches!(\g<item>, FilterValue::Select { id: \g<found>, .. } "
        r"if \g<found> == \g<wanted>))",
        content,
    )


def _normalize_prequeried_url_helpers(content: str, helpers: set[str] | None) -> str:
    for helper in helpers or set():
        function = rf"(?:[A-Za-z_][A-Za-z0-9_]*::)*{re.escape(helper)}\s*\("
        content = re.sub(
            rf'(?P<head>"\{{\}}(?:\\.|[^"\\])*?)\?'
            rf'(?P<tail>(?:\\.|[^"\\])*"\s*,\s*{function})',
            r"\g<head>&\g<tail>",
            content,
        )
    return content


def _normalize_public_absolute_url(content: str, public_base_url: str | None) -> str:
    if not public_base_url:
        return content
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).named("absolute_url"):
        if "get_api_base" not in function.text:
            continue
        opening = function.text.find("{")
        if opening < 0:
            continue
        argument = re.search(
            r"\(\s*(?:mut\s+)?(?P<name>[A-Za-z_]\w*)\s*:\s*&str\b",
            function.text[:opening],
        )
        if argument is None:
            continue
        base = public_base_url.rstrip("/")
        replacement = (
            function.text[:opening].rstrip()
            + " {\n"
            + f'    format!("{{}}/{{}}", {json.dumps(base)}, '
            + f"{argument.group('name')}.trim_start_matches('/'))\n"
            + "}"
        )
        replacements.append((function.text, replacement))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    base = public_base_url.rstrip("/")
    has_relative_model = False
    for node in RustInspection.from_content(content).nodes("struct_expression"):
        name = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name is None or body is None:
            continue
        type_name = name.text.decode("utf-8", errors="replace")
        fields = set()
        for child in body.named_children:
            field = child.child_by_field_name("field")
            if field is not None:
                fields.add(field.text.decode("utf-8", errors="replace"))
            elif child.type == "shorthand_field_initializer":
                fields.add(child.text.decode("utf-8", errors="replace"))
        if type_name in {"Manga", "Chapter", "aidoku::Manga", "aidoku::Chapter"} and (
            "key" in fields and "url" not in fields
        ):
            has_relative_model = True
            break
    has_absolute_helper = re.search(r"\bfn\s+absolute_url\s*\(", content) is not None
    if ("impl Source for" in content or has_relative_model) and not has_absolute_helper:
        content = (
            content.rstrip()
            + "\n\nfn absolute_url(relative: &str) -> String {\n"
            + f'    format!("{{}}/{{}}", {json.dumps(base)}, '
            + "relative.trim_start_matches('/'))\n}\n"
        )
        has_absolute_helper = True
    if has_absolute_helper:
        content = re.sub(
            r"(?m)^(?P<indent>[ \t]*)(?P<target>manga|chapter)\.key\s*=\s*"
            r"(?P<value>[^;\n]+);(?!\s*\n[ \t]*(?P=target)\.url\s*=)",
            lambda match: (
                match.group(0)
                + f"\n{match.group('indent')}{match.group('target')}.url = "
                + f"Some(absolute_url(&{match.group('target')}.key));"
            ),
            content,
        )
        struct_edits: list[tuple[int, int, bytes]] = []
        for node in RustInspection.from_content(content).nodes("struct_expression"):
            name = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name is None or body is None:
                continue
            type_name = name.text.decode("utf-8", errors="replace")
            if type_name not in {"Manga", "Chapter", "aidoku::Manga", "aidoku::Chapter"}:
                continue
            fields = {}
            for child in body.named_children:
                field_name = child.child_by_field_name("field")
                if field_name is not None:
                    fields[field_name.text.decode("utf-8", errors="replace")] = child
                elif child.type == "shorthand_field_initializer":
                    fields[child.text.decode("utf-8", errors="replace")] = child
            if "url" in fields or "key" not in fields:
                continue
            key = fields["key"]
            value = key.child_by_field_name("value")
            expression = (
                value.text.decode("utf-8", errors="replace")
                if value is not None
                else key.text.decode("utf-8", errors="replace")
            )
            key_value = (
                f"{expression}.clone()" if re.fullmatch(r"[A-Za-z_]\w*", expression) else expression
            )
            replacement = f"key: {key_value}, url: Some(absolute_url(&({expression})))"
            struct_edits.append((key.start_byte, key.end_byte, replacement.encode("utf-8")))
        encoded = content.encode("utf-8")
        for start, end, replacement in reversed(struct_edits):
            encoded = encoded[:start] + replacement + encoded[end:]
        content = encoded.decode("utf-8")
    return content


def _normalize_chapter_key_templates(
    content: str,
    chapter_key_templates: tuple[str, ...] | None,
) -> str:
    for template in chapter_key_templates or ():
        expected = template.replace("{comic_path}", "{}").replace("{chapter_id}", "{}")
        first_placeholder = expected.find("{}")
        if first_placeholder <= 0:
            continue
        expected_literal = json.dumps(expected)
        key_prefix = expected[:first_placeholder]
        shortened = expected[first_placeholder:]
        candidates = {shortened, "/" + shortened.lstrip("/")}
        shortened_literal = json.dumps(shortened)
        restored: list[tuple[str, str]] = []
        for node in RustInspection.from_content(content).nodes("if_expression"):
            condition = node.child_by_field_name("condition")
            consequence = node.child_by_field_name("consequence")
            alternative = node.child_by_field_name("alternative")
            if condition is None or consequence is None or alternative is None:
                continue
            alternative_blocks = [
                child for child in alternative.named_children if child.type == "block"
            ]
            condition_text = condition.text.decode("utf-8", errors="replace")
            if (
                consequence.type != "block"
                or len(alternative_blocks) != 1
                or re.search(
                    rf"\.starts_with\(\s*{re.escape(json.dumps(key_prefix))}\s*\)",
                    condition_text,
                )
                is None
                or RustInspection.compact_node(consequence)
                != RustInspection.compact_node(alternative_blocks[0])
            ):
                continue
            consequence_text = consequence.text.decode("utf-8", errors="replace")
            if expected_literal not in consequence_text:
                continue
            original = node.text.decode("utf-8", errors="replace")
            restored.append(
                (
                    original,
                    original.replace(
                        consequence_text,
                        consequence_text.replace(expected_literal, shortened_literal, 1),
                        1,
                    ),
                )
            )
        for original, replacement in restored:
            content = content.replace(original, replacement, 1)

        def replace_unguarded(
            match: re.Match[str],
            current_content: str = content,
            prefix_literal: str = key_prefix,
            replacement_literal: str = expected_literal,
        ) -> str:
            window = current_content[max(0, match.start() - 500) : match.start()]
            guard = re.search(
                rf"if\s+(?P<value>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)"
                rf"\.starts_with\(\s*{re.escape(json.dumps(prefix_literal))}\s*\)"
                r"\s*\{[^{}]*$",
                window,
            )
            arguments = current_content[match.end() : match.end() + 300]
            if guard is not None and re.match(
                rf"\s*,\s*{re.escape(guard.group('value'))}\s*,",
                arguments,
            ):
                return match.group(0)
            return match.group("prefix") + replacement_literal

        for candidate in candidates:
            if candidate == expected:
                continue
            content = re.sub(
                rf"(?P<prefix>\bformat!\(\s*){re.escape(json.dumps(candidate))}(?=\s*,)",
                replace_unguarded,
                content,
            )
    return content


def _normalize_preserved_cover_urls(content: str, preserve_cover_urls: bool) -> str:
    if not preserve_cover_urls:
        return content
    content = re.sub(
        r"(?P<receiver>\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
        r"\s*\.cover\s*\.as_deref\(\)\s*\.map\(\|\s*(?P<value>[A-Za-z_][A-Za-z0-9_]*)"
        r"\s*\|\s*(?:[A-Za-z_][A-Za-z0-9_]*::)*"
        r"[A-Za-z_][A-Za-z0-9_]*resolution[A-Za-z0-9_]*\(\s*(?P=value)\s*,[^)]*\)"
        r"\)\s*\.unwrap_or_default\(\)",
        r"\g<receiver>.cover.clone().unwrap_or_default()",
        content,
    )
    return re.sub(
        r"(?:[A-Za-z_][A-Za-z0-9_]*::)*[A-Za-z_][A-Za-z0-9_]*resolution"
        r"[A-Za-z0-9_]*\(\s*&(?P<receiver>[A-Za-z_][A-Za-z0-9_]*(?:\."
        r"[A-Za-z_][A-Za-z0-9_]*)*)\.cover\s*,[^)]*\)",
        r"\g<receiver>.cover.clone()",
        content,
    )


def _normalize_select_filter_import(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for node in RustInspection.from_content(content).nodes("use_declaration"):
        text = node.text.decode("utf-8", errors="replace")
        if not RustInspection.compact_node(node).startswith("useaidoku::{"):
            continue
        normalized = text.replace("std::filters::SelectFilter", "SelectFilter").replace(
            "filter::SelectFilter", "SelectFilter"
        )
        if normalized != text:
            replacements.append((text, normalized))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    return content


def _remove_macro_only_trait_imports(content: str) -> str:
    if "register_source!" not in content:
        return content
    traits = (
        "BaseUrlProvider",
        "DeepLinkHandler",
        "DynamicFilters",
        "DynamicListings",
        "ImageRequestProvider",
        "ListingProvider",
    )
    replacements: list[tuple[str, str]] = []
    for node in RustInspection.from_content(content).nodes("use_declaration"):
        text = node.text.decode("utf-8", errors="replace")
        compact = RustInspection.compact_node(node)
        if not compact.startswith("useaidoku::"):
            continue
        normalized = text
        for trait in traits:
            if f"impl {trait}" in content:
                continue
            if compact == f"useaidoku::{trait};":
                normalized = ""
                break
            normalized = re.sub(rf"\b{trait}\s*,\s*", "", normalized)
            normalized = re.sub(rf",\s*\b{trait}\b", "", normalized)
            normalized = re.sub(rf"\{{\s*{trait}\s*\}}", "{}", normalized)
        if re.fullmatch(r"use\s+aidoku::\{\s*\}\s*;", normalized.strip()):
            normalized = ""
        if normalized != text:
            replacements.append((text, normalized))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    return content


def _remove_unused_known_imports(content: str) -> str:
    inspection = RustInspection.from_content(content)
    declarations = [
        node.text.decode("utf-8", errors="replace") for node in inspection.nodes("use_declaration")
    ]
    usage = content
    for declaration in declarations:
        usage = usage.replace(declaration, "", 1)
    dto_candidates = {
        name
        for declaration in declarations
        for name in re.findall(r"\b([A-Za-z_]\w*Dto)\b", declaration)
    }
    candidates = (
        (_AIDOKU_ROOT_NAMES - {"Source"})
        | {
            "CheckFilter",
            "MultiSelectFilter",
            "Result",
            "SelectFilter",
            "SortFilter",
            "SortFilterDefault",
            "format",
            "parse_date",
            "vec",
        }
        | dto_candidates
    )
    replacements: list[tuple[str, str]] = []
    for declaration in declarations:
        normalized = declaration
        for name in candidates:
            unqualified_usage = re.sub(rf"\baidoku::{re.escape(name)}\b", "", usage)
            if re.search(rf"\b{re.escape(name)}\b", unqualified_usage):
                continue
            if re.fullmatch(
                rf"use\s+aidoku(?:::[A-Za-z_]\w*)*::(?:std::)?{re.escape(name)};",
                normalized.strip(),
            ) or (
                name in dto_candidates
                and re.fullmatch(
                    rf"use\s+crate(?:::[A-Za-z_]\w*)*::{re.escape(name)};",
                    normalized.strip(),
                )
            ):
                normalized = ""
                break
            token = rf"(?:std::)?{re.escape(name)}"
            normalized = re.sub(rf"\b{token}\s*,\s*", "", normalized)
            normalized = re.sub(rf",\s*\b{token}\b", "", normalized)
            normalized = re.sub(rf"\{{\s*{token}\s*\}}", "{}", normalized)
        normalized = re.sub(r"(?P<path>[A-Za-z_]\w*)::\{\s*\}", "", normalized)
        normalized = re.sub(r",(?P<space>\s*),", r",\g<space>", normalized)
        if re.fullmatch(r"use\s+(?:aidoku(?:::\{?\s*\}?)?)?\s*;", normalized.strip()):
            normalized = ""
        if normalized != declaration:
            replacements.append((declaration, normalized))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    return content


def _remove_duplicate_imports(content: str) -> str:
    content = re.sub(
        r"use\s+(?P<prefix>[A-Za-z_][\w:]*)::\{\s*(?P<name>[A-Za-z_]\w*)\s*\};",
        r"use \g<prefix>::\g<name>;",
        content,
    )
    declarations = [
        node.text.decode("utf-8", errors="replace")
        for node in RustInspection.from_content(content).nodes("use_declaration")
    ]
    grouped: set[tuple[str, str]] = set()
    for declaration in declarations:
        match = re.fullmatch(
            r"use\s+(?P<prefix>[A-Za-z_][\w:]*)::\{(?P<body>[\s\S]*)\};", declaration
        )
        if match is None:
            continue
        for item in match.group("body").split(","):
            name = item.strip()
            if re.fullmatch(r"[A-Za-z_]\w*", name):
                grouped.add((match.group("prefix"), name))
    for declaration in declarations:
        match = re.fullmatch(
            r"use\s+(?P<prefix>[A-Za-z_][\w:]*)::(?P<name>[A-Za-z_]\w*);",
            declaration,
        )
        if match is not None and (match.group("prefix"), match.group("name")) in grouped:
            content = content.replace(declaration, "", 1)
    return content


def _normalize_generated_setting_defaults(
    content: str,
    setting_defaults: Mapping[str, str] | None,
) -> str:
    for key, default in (setting_defaults or {}).items():
        if not default:
            continue
        key_literal = json.dumps(key, ensure_ascii=False)
        default_literal = json.dumps(default, ensure_ascii=False)
        rust_string = r'"(?:\\.|[^"\\])*"'
        key_constants = re.findall(
            rf"\bconst\s+([A-Za-z_]\w*)\s*:\s*&str\s*=\s*"
            rf"{re.escape(key_literal)}\s*;",
            content,
        )
        key_reference = (
            "(?:"
            + "|".join(
                [re.escape(key_literal), *(rf"\b{re.escape(name)}\b" for name in key_constants)]
            )
            + ")"
        )
        content = re.sub(
            rf"defaults_get(?:::<String>)?\(\s*{key_reference}\s*\)"
            rf"\s*(?:\.unwrap_or_default\(\)|\.unwrap_or_else\(\|\|\s*"
            rf"(?:String::from\(\s*{rust_string}\s*\)|{rust_string}\.into\(\)|"
            rf"{rust_string}\.to_string\(\))\s*\))",
            f"defaults_get::<String>({key_literal})"
            f".unwrap_or_else(|| String::from({default_literal}))",
            content,
        )
        content = re.sub(
            rf"(?P<prefix>defaults_get(?:::<String>)?\(\s*{key_reference}\s*\)"
            rf"[^;{{}}]{{0,500}}?\.unwrap_or_else\(\|\|\s*)"
            rf"{rust_string}\.to_string\(\)\s*\)",
            lambda match, literal=default_literal: (
                f"{match.group('prefix')}String::from({literal}))"
            ),
            content,
        )
        fallback_constants = set(
            re.findall(
                rf"defaults_get(?:::<String>)?\(\s*{key_reference}\s*\)"
                r"[^;{}]{0,500}?\.unwrap_or_else\(\|\|\s*"
                r"([A-Z][A-Z0-9_]*)\s*\.into\(\)\s*\)",
                content,
            )
        )
        for constant in fallback_constants:
            content = re.sub(
                rf"(?P<prefix>\bconst\s+{re.escape(constant)}\s*:\s*&str\s*=\s*)"
                rf"{rust_string}(?P<suffix>\s*;)",
                rf"\g<prefix>{default_literal}\g<suffix>",
                content,
            )
        if key.rsplit(".", 1)[-1] != "api_domain":
            continue
        function_replacements: list[tuple[str, str]] = []
        for function in RustInspection.from_content(content).functions:
            if (
                re.search(
                    rf"defaults_get(?:::<String>)?\(\s*{key_reference}\s*\)",
                    function.text,
                )
                is None
            ):
                continue
            normalized = re.sub(
                rf"(?P<prefix>\b_\s*=>\s*)"
                rf"(?:String::from\(\s*{rust_string}\s*\)|"
                rf"{rust_string}\.to_string\(\)|{rust_string}\.into\(\))",
                rf"\g<prefix>String::from({default_literal})",
                function.text,
            )
            if normalized != function.text:
                function_replacements.append((function.text, normalized))
        for original, normalized in function_replacements:
            content = content.replace(original, normalized, 1)
    return content


def _normalize_dynamic_api_base(
    content: str,
    setting_defaults: Mapping[str, str] | None,
) -> str:
    api_key = next(
        (key for key in (setting_defaults or {}) if key.rsplit(".", 1)[-1] == "api_domain"),
        None,
    )
    if api_key is None:
        return content
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        if function.name != "api_url" and not function.name.endswith("_api_url"):
            continue
        normalized = re.sub(
            r"String::from\(\s*[A-Z][A-Z0-9_]*\s*\)",
            'format!("https://{}", api_domain())',
            function.text,
            count=1,
        )
        if normalized != function.text:
            replacements.append((function.text, normalized))
    if not replacements:
        return content
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    if re.search(r"\bfn\s+api_domain\s*\(", content):
        return content
    default = (setting_defaults or {}).get(api_key, "")
    helper = (
        "fn api_domain() -> String {\n"
        f"    defaults_get::<String>({json.dumps(api_key)})\n"
        f"        .unwrap_or_else(|| String::from({json.dumps(default)}))\n"
        "}"
    )
    return content.rstrip() + "\n\n" + helper + "\n"


def _normalize_generated_setting_key_aliases(
    content: str,
    setting_defaults: Mapping[str, str] | None,
    setting_keys: tuple[str, ...] | None = None,
) -> str:
    keys = tuple(dict.fromkeys([*(setting_keys or ()), *(setting_defaults or {}).keys()]))
    suffixes: dict[str, list[str]] = {}
    for key in keys:
        suffixes.setdefault(key.rsplit(".", 1)[-1].casefold(), []).append(key)
    aliases = {
        suffix: matches[0]
        for suffix, matches in suffixes.items()
        if len(matches) == 1 and suffix != matches[0]
    }
    for alias, key in aliases.items():
        alias_literal = json.dumps(alias, ensure_ascii=False)
        key_literal = json.dumps(key, ensure_ascii=False)
        content = re.sub(
            rf"(?P<prefix>defaults_get(?:::<[^>]+>)?\(\s*)"
            rf"{re.escape(alias_literal)}(?P<suffix>\s*\))",
            rf"\g<prefix>{key_literal}\g<suffix>",
            content,
        )
    canonical = {suffix: matches[0] for suffix, matches in suffixes.items() if len(matches) == 1}
    for literal in set(re.findall(r'"(?:\\.|[^"\\])*"', content)):
        try:
            value = json.loads(literal)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, str):
            continue
        prefix, separator, suffix = value.rpartition(".")
        suffix = suffix.casefold()
        if not separator or not suffix.startswith("http_"):
            continue
        normalized = suffix.removeprefix("http_")
        key = canonical.get(normalized)
        if key is not None and key.rpartition(".")[0].casefold() == prefix.casefold():
            content = content.replace(literal, json.dumps(key, ensure_ascii=False))
    return content


_PLATFORM_PROTOCOL_VALUES: Mapping[str, str | None] = {
    "platform.none": None,
    "platform.blank": " ",
    "platform.one": "1",
    "platform.two": "2",
    "platform.three": "3",
    "platform.four": "4",
    "platform.five": "5",
}


def _normalize_resolution_setting(
    content: str,
    setting_defaults: Mapping[str, str] | None,
    setting_values: Mapping[str, tuple[str, ...]] | None,
) -> str:
    defaults = setting_defaults or {}
    for key, values in (setting_values or {}).items():
        if (
            key.rsplit(".", 1)[-1] != "resolution"
            or not values
            or not all(re.fullmatch(r"resolution\.r[1-9][0-9]*", value) for value in values)
        ):
            continue
        default = defaults.get(key, values[-1]).rsplit(".r", 1)[-1]
        arms = "\n".join(
            f"Some({json.dumps(value)}) => String::from({json.dumps(value.rsplit('.r', 1)[-1])}),"
            for value in values
        )
        replacement = (
            f"match defaults_get::<String>({json.dumps(key)}).as_deref() {{\n"
            f"        {arms}\n"
            f"        _ => String::from({json.dumps(default)}),\n"
            "    }"
        )
        rust_string = r'"(?:\\.|[^"\\])*"'
        content = re.sub(
            rf"defaults_get(?:::<String>)?\(\s*{re.escape(json.dumps(key))}\s*\)"
            rf"\s*\.unwrap_or_else\(\|\|\s*String::from\(\s*{rust_string}\s*\)\s*\)",
            lambda _match, value=replacement: value,
            content,
        )
    return content


def _normalize_platform_header_setting(
    content: str,
    setting_defaults: Mapping[str, str] | None,
    setting_values: Mapping[str, tuple[str, ...]] | None,
) -> str:
    """Translate recovered enum storage keys before sending the platform header."""
    defaults = setting_defaults or {}
    candidates = {
        key: values
        for key, values in (setting_values or {}).items()
        if key.rsplit(".", 1)[-1] == "platform"
        and values
        and set(values).issubset(_PLATFORM_PROTOCOL_VALUES)
        and "platform.one" in values
    }
    if not candidates:
        return content

    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        function_text = function.text
        key = next(
            (candidate for candidate in candidates if json.dumps(candidate) in function_text),
            None,
        )
        if (
            key is not None
            and '"platform"' in function_text
            and "Option<" not in function_text.split("{", 1)[0]
        ):
            rust_string = r'"(?:\\.|[^"\\])*"'
            binding = re.search(
                rf"(?P<indent>^[ \t]*)let\s+(?P<variable>[A-Za-z_]\w*)"
                rf"(?:\s*:\s*String)?\s*=\s*"
                rf"(?:aidoku::imports::defaults::)?defaults_get(?:::<String>)?\(\s*"
                rf"{re.escape(json.dumps(key))}\s*\)\s*\.unwrap_or_else\(\|\|\s*"
                rf"String::from\(\s*{rust_string}\s*\)\s*\)\s*;",
                function_text,
                re.MULTILINE,
            )
            if (
                binding is not None
                and f"match {binding.group('variable')}.as_str()" not in function_text
                and re.search(
                    rf'"platform"[\s\S]{{0,160}}\b{re.escape(binding.group("variable"))}\b',
                    function_text,
                )
            ):
                indent = binding.group("indent")
                arms = []
                for stored in candidates[key]:
                    protocol_value = _PLATFORM_PROTOCOL_VALUES[stored]
                    expression = (
                        "String::new()"
                        if protocol_value is None
                        else f"String::from({json.dumps(protocol_value)})"
                    )
                    arms.append(f"{indent}    Some({json.dumps(stored)}) => {expression},")
                default = defaults.get(key, "platform.one")
                protocol_default = _PLATFORM_PROTOCOL_VALUES.get(default, "1")
                default_expression = (
                    "String::new()"
                    if protocol_default is None
                    else f"String::from({json.dumps(protocol_default)})"
                )
                arms.append(f"{indent}    _ => {default_expression},")
                replacement = (
                    f"{indent}let {binding.group('variable')} = "
                    "match aidoku::imports::defaults::defaults_get::<String>"
                    f"({json.dumps(key)}).as_deref() {{\n" + "\n".join(arms) + f"\n{indent}}};"
                )
                normalized = (
                    function_text[: binding.start()] + replacement + function_text[binding.end() :]
                )
                replacements.append((function_text, normalized))
                continue
        if (
            key is None
            or '"platform"' not in function_text
            or ".map(" not in function_text
            or "Option<" not in function_text.split("{", 1)[0]
        ):
            continue
        opening = function_text.find("{")
        if opening < 0:
            continue
        default = defaults.get(key, "platform.one")
        arms = []
        for stored in candidates[key]:
            protocol_value = _PLATFORM_PROTOCOL_VALUES[stored]
            if protocol_value is None:
                arms.append(f"        {json.dumps(stored)} => None,")
            else:
                arms.append(
                    f'        {json.dumps(stored)} => Some(("platform", '
                    f"String::from({json.dumps(protocol_value)}))),"
                )
        arms.append("        _ => None,")
        replacement = (
            function_text[:opening].rstrip()
            + " {\n"
            + "    let platform = aidoku::imports::defaults::defaults_get::<String>"
            + f"({json.dumps(key)})\n"
            + f"        .unwrap_or_else(|| String::from({json.dumps(default)}));\n"
            + "    match platform.as_str() {\n"
            + "\n".join(arms)
            + "\n    }\n}"
        )
        replacements.append((function_text, replacement))
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
) -> str:
    """Apply small type-safe compatibility rewrites for the pinned Aidoku/Rust APIs."""
    content = _normalize_safe_std_paths(content, remove_extern_std=remove_extern_std)
    content = _normalize_graphql_request_body(content)
    content = _normalize_aidoku_api_paths(content)
    content = _normalize_generic_deserialize(content)
    content = _normalize_graphql_body_fragment(content)
    content = _normalize_html_element_text(content)
    content = _normalize_utf8_slice_loops(content)
    content = _normalize_graphql_manga_update_projection(content)
    content = _normalize_image_request_result(content)
    content = _normalize_result_request_tails(content)
    content = _normalize_detail_partial_move(content)
    content = _normalize_manga_replacement_chapters(content)
    content = _normalize_legacy_request_errors(content)
    content = _normalize_defaults_get_bindings(content)
    content = _normalize_owned_setting_routes(content)
    content = _normalize_defaults_set_string_values(content)
    content = _normalize_rsa_bootstrap_diagnostics(content)
    content = _normalize_aidoku_result_errors(content)
    content = _normalize_raw_json_response_bindings(content)
    content = _normalize_request_builder_helpers(content, request_builder_helpers)
    content = _inject_source_new(content)
    content = _normalize_mutated_aidoku_models(content)
    content = _normalize_default_model_assignments(content)
    content = _normalize_page_index_fields(content)
    content = _normalize_legacy_filter_fields(content)
    content = _normalize_select_filter_constructors(content)
    content = _normalize_legacy_page_context(content)
    content = _normalize_page_url_context(content)
    content = _normalize_deep_link_defaults(content)
    content = _normalize_parse_date_option_patterns(content)
    content = _normalize_optional_chapter_dates(content)
    content = _normalize_chapter_group_scope(content)
    if allow_dead_code and not re.search(
        r"#!\[allow\([^\]]*\bdead_code\b",
        content,
    ):
        content = "#![allow(dead_code)]\n" + content.lstrip()
    content = content.replace("aidoku::std::filters::SelectFilter", "aidoku::SelectFilter")
    content = content.replace("aidoku::filters::SelectFilter", "aidoku::SelectFilter")
    content = content.replace("aidoku::filter::SelectFilter", "aidoku::SelectFilter")
    content = _normalize_select_filter_import(content)
    content = _remove_macro_only_trait_imports(content)
    if re.search(r"let\s+host\s*=\s*absolute\b", content):
        content = content.replace("Request::get(absolute)?", "Request::get(absolute.clone())?")
    content = re.sub(
        r'"(?:\\.|[^"\\])*"',
        lambda match: match.group(0).replace("}//chapters", "}/chapters"),
        content,
    )
    content = _normalize_idempotent_get_retry(content)
    content = _normalize_pinned_model_shapes(content)
    content = _normalize_optional_model_shorthand(content)
    content = _normalize_pinned_model_fields(content)
    content = _normalize_nested_optional_model_fields(content)
    content = _normalize_base_url_provider(content)
    content = _normalize_comic_path_helper(content)
    content = _normalize_struct_expression_defaults(content)
    content = _normalize_pagination_result_impls(content)
    content = _normalize_partial_move_pagination(content)
    content = _normalize_partial_move_loop_pagination(content)
    content = _normalize_collection_len_after_move(content)
    content = _normalize_moved_key_then_borrowed_url(content)
    content = _normalize_select_filter_structs(content)
    content = _normalize_resolution_regex(content)
    content = _normalize_discarded_enumerate_index(content)
    content = _normalize_filter_match_predicate(content)
    content = _normalize_prequeried_url_helpers(content, prequeried_url_helpers)
    content = _normalize_public_absolute_url(content, public_base_url)
    content = _normalize_chapter_key_templates(content, chapter_key_templates)
    content = _normalize_identical_if_branches(content)
    content = _normalize_preserved_cover_urls(content, preserve_cover_urls)
    content = re.sub(
        r"\blet\s+domain\s*=\s*defaults_get\(",
        "let domain: String = defaults_get(",
        content,
    )
    content = content.replace(
        "let domain: String = defaults_get(",
        "let domain: String = defaults_get::<String>(",
    )
    content = re.sub(
        r"(?P<prefix>\b(?:id|title):\s*(?:Some\()?)"
        r'(?P<literal>"(?:\\.|[^"\\])*")\.to_string\(\)',
        r"\g<prefix>\g<literal>.into()",
        content,
    )
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
    content = _normalize_dynamic_api_base(content, setting_defaults)
    content = _normalize_generated_setting_key_aliases(content, setting_defaults, setting_keys)
    content = _normalize_generated_setting_defaults(content, setting_defaults)
    content = _normalize_resolution_setting(content, setting_defaults, setting_values)
    content = _normalize_platform_header_setting(content, setting_defaults, setting_values)
    content = _inject_no_std_macro_imports(content)
    content = _inject_required_aidoku_imports(content)
    content = _remove_macro_only_trait_imports(content)
    content = _remove_duplicate_imports(content)
    content = _remove_unused_known_imports(content)
    content = re.sub(
        r"(\bparse_(?:local_)?date\s*\([^;]{0,800}?\))\s*\.ok\(\)",
        r"\1",
        content,
    )
    content = re.sub(
        r"\b(?P<items>[A-Za-z_]\w*)\.sort_by\(\|(?P<left>[A-Za-z_]\w*),\s*"
        r"(?P<right>[A-Za-z_]\w*)\|\s*(?P=right)\.(?P<field>[A-Za-z_]\w*)"
        r"\.cmp\(&(?P=left)\.(?P=field)\)\);",
        lambda match: (
            f"{match.group('items')}.sort_by_key(|item| "
            f"core::cmp::Reverse(item.{match.group('field')}));"
        ),
        content,
    )
    return content


def render_generated_lib_rs(
    source_struct: str,
    implemented_traits: list[str],
    generated_paths: set[str],
) -> str:
    """Own the crate entry point once AI has separated its source implementation."""
    modules = set()
    for path in generated_paths:
        parts = PurePosixPath(path).parts
        if len(parts) < 2 or parts[0] != "src":
            continue
        module = parts[1] if len(parts) > 2 else PurePosixPath(parts[1]).stem
        if module not in {"lib", "generated_smoke"}:
            modules.add(module)
    declarations = "\n".join(f"mod {module};" for module in sorted(modules))
    trait_arguments = "".join(f",\n    {trait}" for trait in implemented_traits)
    return (
        "#![no_std]\n\n"
        "use aidoku::{Source, prelude::register_source};\n\n"
        f"{declarations}\n\n"
        f"pub use source::{source_struct};\n\n"
        f"register_source!(\n    {source_struct}{trait_arguments}\n);\n"
    )


def _prequeried_url_helpers(manifest: GenerationManifest) -> set[str]:
    helpers: set[str] = set()
    for generated in manifest.files:
        if not generated.path.endswith(".rs"):
            continue
        for function in RustInspection.from_content(generated.content).functions:
            if re.search(r'"(?:\\.|[^"\\])*\?(?:\\.|[^"\\])*"', function.text):
                helpers.add(function.name)
    return helpers


def _request_builder_helpers(manifest: GenerationManifest) -> set[str]:
    helpers: set[str] = set()
    for generated in manifest.files:
        if not generated.path.endswith(".rs"):
            continue
        for function in RustInspection.from_content(generated.content).functions:
            if "Request::get" in function.text and ".header(" in function.text:
                helpers.add(function.name)
    return helpers


def _skip_unused_decompiled_dto_fields(
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    rust_content = "\n".join(
        generated.content for generated in files if generated.path.endswith(".rs")
    )
    declared_types = set(
        re.findall(
            r"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|type|trait)\s+"
            r"([A-Za-z_]\w*)",
            rust_content,
        )
    )
    known_types = (
        declared_types
        | _AIDOKU_ROOT_NAMES
        | {
            "BTreeMap",
            "Option",
            "Result",
            "String",
            "Value",
            "Vec",
        }
    )
    updated = []
    for generated in files:
        if not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        raw = bytearray(generated.content.encode())
        edits: list[tuple[int, int, bytes]] = []
        for struct in RustInspection.from_content(generated.content).structs:
            sibling = struct.node.prev_named_sibling
            attributes = []
            while sibling is not None and sibling.type == "attribute_item":
                attributes.append(sibling.text.decode("utf-8", errors="replace"))
                sibling = sibling.prev_named_sibling
            if not any("Deserialize" in attribute for attribute in attributes):
                continue
            for field in struct.fields:
                if re.search(
                    rf"\.\s*{re.escape(field.name)}\b(?!\s*\()",
                    rust_content,
                ):
                    continue
                sibling = field.node.prev_named_sibling
                has_skip = False
                while sibling is not None and sibling.type == "attribute_item":
                    if "skip_deserializing" in sibling.text.decode("utf-8", errors="replace"):
                        has_skip = True
                        break
                    sibling = sibling.prev_named_sibling
                type_node = field.node.child_by_field_name("type")
                type_names = {
                    name for name in re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", field.type_text)
                }
                unresolved = type_names - known_types
                if type_node is not None:
                    if unresolved:
                        replacement = b"Option<serde_json::Value>"
                        edits.append((type_node.start_byte, type_node.end_byte, replacement))
                    elif not field.type_text.startswith("Option<"):
                        replacement = f"Option<{field.type_text}>".encode()
                        edits.append((type_node.start_byte, type_node.end_byte, replacement))
                if not has_skip:
                    line_start = generated.content.rfind("\n", 0, field.node.start_byte) + 1
                    prefix = generated.content[line_start : field.node.start_byte]
                    indent = prefix if not prefix.strip() else "    "
                    attribute = f"#[serde(skip_deserializing)]\n{indent}".encode()
                    edits.append((field.node.start_byte, field.node.start_byte, attribute))
        for start, end, replacement in sorted(edits, reverse=True):
            raw[start:end] = replacement
        content = raw.decode()
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


def _project_recovered_nested_dto_aliases(
    ir: SourceIR,
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    source_shapes = {shape.name: shape for shape in decompiled_dto_shapes(ir.files)}
    if not source_shapes:
        return files
    rust = RustInspection(
        generated.content for generated in files if generated.path.endswith(".rs")
    )

    def nested_source_type(java_type: str) -> str | None:
        candidates = re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", java_type)
        matches = [candidate for candidate in candidates if candidate in source_shapes]
        return matches[0] if len(matches) == 1 else None

    def nested_rust_type(rust_type: str) -> str | None:
        candidates = re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", rust_type)
        matches = [candidate for candidate in candidates if rust.struct_named(candidate)]
        return matches[0] if len(matches) == 1 else None

    rust_primitives = {
        "String": "String",
        "int": "i32",
        "Integer": "i32",
        "long": "i64",
        "Long": "i64",
        "boolean": "bool",
        "Boolean": "bool",
    }
    aliases: set[tuple[str, str, str]] = set()
    for source_owner in source_shapes.values():
        rust_owner = rust.struct_named(source_owner.name)
        if rust_owner is None:
            continue
        for source_field in source_owner.fields:
            expected_type = nested_source_type(source_field.java_type)
            rust_field = next(
                (
                    field
                    for field in rust_owner.fields
                    if field.name == source_field.name
                    or field.serialized_name == source_field.serialized_name
                ),
                None,
            )
            if expected_type is None or rust_field is None:
                continue
            actual_type = nested_rust_type(rust_field.type_text)
            if actual_type is None or actual_type == expected_type:
                continue
            expected_shape = source_shapes[expected_type]
            actual_shape = rust.struct_named(actual_type)
            assert actual_shape is not None
            if {field.serialized_name for field in expected_shape.fields} & {
                field.serialized_name for field in actual_shape.fields
            }:
                continue
            compatible = [
                (expected, actual)
                for expected in expected_shape.fields
                for actual in actual_shape.fields
                if rust_primitives.get(expected.java_type) == actual.type_text
                and expected.serialized_name != actual.serialized_name
            ]
            if len(compatible) == 1:
                expected, actual = compatible[0]
                aliases.add((actual_type, actual.name, expected.serialized_name))
    if not aliases:
        return files

    updated = []
    for generated in files:
        if not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        raw = bytearray(generated.content.encode())
        edits: list[tuple[int, bytes]] = []
        inspection = RustInspection.from_content(generated.content)
        for struct_name, field_name, alias in aliases:
            field = inspection.struct_field(struct_name, field_name)
            if field is None or any(
                f'alias = "{alias}"' in attribute.text.decode("utf-8", errors="replace")
                for attribute in field.attributes
            ):
                continue
            line_start = generated.content.rfind("\n", 0, field.node.start_byte) + 1
            prefix = generated.content[line_start : field.node.start_byte]
            indent = prefix if not prefix.strip() else "    "
            edits.append(
                (
                    field.node.start_byte,
                    f'#[serde(alias = "{alias}")]\n{indent}'.encode(),
                )
            )
        for position, replacement in sorted(edits, reverse=True):
            raw[position:position] = replacement
        content = raw.decode()
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


def _project_recovered_nullable_dto_defaults(
    ir: SourceIR,
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    nullable = decompiled_nullable_dto_fields(ir.files)
    if not nullable:
        return files
    updated = []
    for generated in files:
        if not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        encoded = generated.content.encode()
        raw = bytearray(encoded)
        edits: list[tuple[int, bytes]] = []
        inspection = RustInspection.from_content(generated.content)
        for struct_name, serialized_name in nullable:
            struct = inspection.struct_named(struct_name)
            if struct is None:
                continue
            field = next(
                (
                    candidate
                    for candidate in struct.fields
                    if candidate.name == serialized_name
                    or candidate.serialized_name == serialized_name
                ),
                None,
            )
            if field is None or any(
                re.search(r"\bserde\s*\([^)]*\bdefault\b", attribute.text.decode())
                for attribute in field.attributes
            ):
                continue
            line_start = encoded.rfind(b"\n", 0, field.node.start_byte) + 1
            prefix = encoded[line_start : field.node.start_byte].decode()
            indent = prefix if not prefix.strip() else "    "
            edits.append((field.node.start_byte, f"#[serde(default)]\n{indent}".encode()))
        for position, replacement in sorted(edits, reverse=True):
            raw[position:position] = replacement
        content = raw.decode()
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


def _normalize_generated_module_topology(files: list[GeneratedFile]) -> list[GeneratedFile]:
    modules = {
        PurePosixPath(generated.path).stem: generated.path
        for generated in files
        if generated.path.startswith("src/")
        and generated.path.endswith(".rs")
        and generated.path not in {"src/lib.rs", "src/generated_smoke.rs"}
        and len(PurePosixPath(generated.path).parts) == 2
    }
    definitions: dict[str, str] = {}
    for generated in files:
        module = PurePosixPath(generated.path).stem
        if module not in modules:
            continue
        for name in re.findall(
            r"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?"
            r"(?:fn|struct|enum|type|const|static)\s+([A-Za-z_]\w*)",
            generated.content,
        ):
            definitions.setdefault(name, module)

    contents = {generated.path: generated.content for generated in files}
    for path in list(contents):
        if not path.endswith(".rs") or path == "src/lib.rs":
            continue
        content = contents[path]
        inspection = RustInspection.from_content(content)
        register_edits = []
        for node in inspection.nodes("macro_invocation"):
            macro = node.child_by_field_name("macro")
            if _last_rust_identifier(macro) == "register_source":
                end = node.end_byte
                encoded = content.encode("utf-8")
                while end < len(encoded) and encoded[end : end + 1] in {b" ", b"\t"}:
                    end += 1
                if encoded[end : end + 1] == b";":
                    end += 1
                if encoded[end : end + 1] == b"\n":
                    end += 1
                register_edits.append((node.start_byte, end))
        if register_edits:
            encoded = content.encode("utf-8")
            for start, end in reversed(register_edits):
                encoded = encoded[:start] + encoded[end:]
            content = encoded.decode("utf-8")
        for module in modules:
            if module == PurePosixPath(path).stem:
                continue
            content = re.sub(
                rf"(?m)^\s*mod\s+{re.escape(module)}\s*;\s*\n?",
                "",
                content,
            )
            content = re.sub(
                rf"(?m)^(?P<indent>\s*)use\s+{re.escape(module)}::",
                rf"\g<indent>use crate::{module}::",
                content,
            )
        for symbol in re.findall(r"(?m)^\s*use\s+crate::([A-Za-z_]\w*)\s*;", content):
            owner = definitions.get(symbol)
            if owner is None or owner == PurePosixPath(path).stem:
                continue
            content = re.sub(
                rf"(?m)^(?P<indent>\s*)use\s+crate::{re.escape(symbol)}\s*;",
                rf"\g<indent>use crate::{owner}::{symbol};",
                content,
            )
            owner_path = modules[owner]
            owner_content = contents[owner_path]
            owner_content = re.sub(
                rf"(?m)^(?P<indent>\s*)(?!pub\b)(?P<kind>fn|struct|enum|type|const|static)"
                rf"\s+{re.escape(symbol)}\b",
                rf"\g<indent>pub(crate) \g<kind> {symbol}",
                owner_content,
                count=1,
            )
            contents[owner_path] = owner_content
        usage_without_imports = re.sub(r"(?m)^\s*use\s+[^;]+;", "", content)
        current_module = PurePosixPath(path).stem
        for symbol, owner in definitions.items():
            if (
                owner == current_module
                or not re.fullmatch(r"[A-Z][A-Z0-9_]*", symbol)
                or re.search(rf"\b{re.escape(symbol)}\b", usage_without_imports) is None
                or re.search(rf"\b(?:crate::)?{re.escape(owner)}::{re.escape(symbol)}\b", content)
                is not None
                or re.search(rf"use\s+crate::{re.escape(owner)}::{re.escape(symbol)}", content)
                is not None
            ):
                continue
            content = _inject_import(content, f"use crate::{owner}::{symbol};")
            owner_path = modules[owner]
            owner_content = contents[owner_path]
            owner_content = re.sub(
                rf"(?m)^(?P<indent>\s*)(?!pub\b)(?P<kind>const|static)\s+"
                rf"{re.escape(symbol)}\b",
                rf"\g<indent>pub(crate) \g<kind> {symbol}",
                owner_content,
                count=1,
            )
            contents[owner_path] = owner_content
        contents[path] = content

    return [
        generated.model_copy(update={"content": contents[generated.path]})
        if contents[generated.path] != generated.content
        else generated
        for generated in files
    ]


def _request_header_lines(headers: Mapping[str, str], *, indent: str) -> list[str]:
    return [
        f"{indent}request = request.header({json.dumps(name)}, {json.dumps(value)});"
        for name, value in headers.items()
    ]


def _recovered_request_builder(
    ir: SourceIR,
    *,
    setting_defaults: Mapping[str, str],
    setting_values: Mapping[str, tuple[str, ...]],
) -> str | None:
    profiles = [profile for profile in ir.request_header_profiles if profile.headers]
    if not profiles and not ir.shared_request_headers:
        return None
    api_key = next(
        (key for key in setting_defaults if key.rsplit(".", 1)[-1] == "api_domain"), None
    )
    default_domain = setting_defaults.get(api_key, "") if api_key else ""
    default_profile = next(
        (profile for profile in profiles if default_domain in profile.domains),
        profiles[0] if profiles else None,
    )
    lines = [
        "fn c2a_request(url: &str) -> aidoku::Result<aidoku::imports::net::Request> {",
        "    let mut request = aidoku::imports::net::Request::get(url)?;",
    ]
    conditional = [
        profile for profile in profiles if profile is not default_profile and profile.domains
    ]
    for index, profile in enumerate(conditional):
        condition = " || ".join(f"url.contains({json.dumps(domain)})" for domain in profile.domains)
        lines.append(("    if " if index == 0 else "    else if ") + condition + " {")
        lines.extend(_request_header_lines(profile.headers, indent="        "))
        lines.append("    }")
    if default_profile is not None:
        if conditional:
            lines.append("    else {")
            lines.extend(_request_header_lines(default_profile.headers, indent="        "))
            lines.append("    }")
        else:
            lines.extend(_request_header_lines(default_profile.headers, indent="    "))
    lines.extend(_request_header_lines(ir.shared_request_headers, indent="    "))

    platform_key = next(
        (
            key
            for key, values in setting_values.items()
            if key.rsplit(".", 1)[-1] == "platform"
            and values
            and set(values).issubset(_PLATFORM_PROTOCOL_VALUES)
        ),
        None,
    )
    if platform_key is not None:
        default = setting_defaults.get(platform_key, "platform.one")
        lines.extend(
            [
                "    let platform = match "
                "aidoku::imports::defaults::defaults_get::<aidoku::alloc::String>"
                f"({json.dumps(platform_key)}).as_deref() {{",
            ]
        )
        for stored in setting_values[platform_key]:
            protocol = _PLATFORM_PROTOCOL_VALUES[stored]
            rendered = "None" if protocol is None else f"Some({json.dumps(protocol)})"
            lines.append(f"        Some({json.dumps(stored)}) => {rendered},")
        default_protocol = _PLATFORM_PROTOCOL_VALUES.get(default, "1")
        rendered_default = (
            "None" if default_protocol is None else f"Some({json.dumps(default_protocol)})"
        )
        lines.extend(
            [
                f"        _ => {rendered_default},",
                "    };",
                "    if let Some(platform) = platform {",
                '        request = request.header("platform", &platform);',
                "    }",
            ]
        )

    user_agent_key = next(
        (key for key in setting_defaults if key.rsplit(".", 1)[-1] == "user_agent"),
        None,
    )
    if user_agent_key is not None and "User-Agent" in ir.header_names:
        lines.extend(
            [
                "    let user_agent = "
                "aidoku::imports::defaults::defaults_get::<aidoku::alloc::String>"
                f"({json.dumps(user_agent_key)}).unwrap_or_default();",
                '    if user_agent != "none" {',
                '        if user_agent.is_empty() || user_agent == "desktop" '
                '|| user_agent == "mobile" || user_agent == "app" {',
                '            request = request.header("User-Agent", '
                f"{json.dumps(DEFAULT_BROWSER_USER_AGENT)});",
                "        } else {",
                '            request = request.header("User-Agent", &user_agent);',
                "        }",
                "    }",
            ]
        )
    lines.extend(["    Ok(request)", "}"])
    return "\n".join(lines)


def _project_recovered_request_headers(
    ir: SourceIR,
    files: list[GeneratedFile],
    *,
    setting_defaults: Mapping[str, str],
    setting_values: Mapping[str, tuple[str, ...]],
) -> list[GeneratedFile]:
    files = _project_shared_request_headers(ir.shared_request_headers, files)
    builder = _recovered_request_builder(
        ir,
        setting_defaults=setting_defaults,
        setting_values=setting_values,
    )
    if builder is None:
        return files
    updated = []
    projected = False
    for generated in files:
        content = generated.content
        if projected or not generated.path.endswith(".rs") or "fn c2a_request(" in content:
            updated.append(generated)
            continue
        replacement = None
        for function in RustInspection.from_content(content).functions:
            if (
                "Request::get" in function.text
                and ".send()" in function.text
                and re.search(r"\burl\s*:\s*&str\b", function.text)
                and "Response" in function.text.split("{", 1)[0]
                and ".header(" not in function.text
            ):
                header = function.text.split("{", 1)[0].rstrip()
                replacement = (
                    header
                    + "{\n"
                    + "    let response = match c2a_request(url)?.send() {\n"
                    + "        Ok(response) => response,\n"
                    + "        Err(_) => c2a_request(url)?.send()?,\n"
                    + "    };\n"
                    + "    Ok(response)\n"
                    + "}"
                )
                content = content.replace(function.text, replacement, 1)
                content = content.rstrip() + "\n\n" + builder + "\n"
                projected = True
                break
        updated.append(
            generated.model_copy(update={"content": content})
            if replacement is not None
            else generated
        )
    return updated


def _project_shared_request_headers(
    headers: Mapping[str, str],
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    if not headers:
        return files
    updated: list[GeneratedFile] = []
    for generated in files:
        content = generated.content
        if not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        replacements: list[tuple[str, str]] = []
        for function in RustInspection.from_content(content).functions:
            if "Request::get" not in function.text:
                continue
            missing = [
                (name, value)
                for name, value in headers.items()
                if re.search(
                    rf"\.header\(\s*{re.escape(json.dumps(name))}\s*,",
                    function.text,
                    re.IGNORECASE,
                )
                is None
            ]
            if not missing:
                continue
            local = RustInspection.from_content(function.text)
            projected_function = None
            for call in local.nodes("call_expression"):
                callee = call.child_by_field_name("function")
                if callee is None or callee.type != "field_expression":
                    continue
                field = callee.child_by_field_name("field")
                receiver = callee.child_by_field_name("value")
                if (
                    field is None
                    or field.text != b"send"
                    or receiver is None
                    or "Request::get" not in receiver.text.decode("utf-8", errors="replace")
                ):
                    continue
                request = receiver.text.decode("utf-8", errors="replace")
                projected_request = request + "".join(
                    f".header({json.dumps(name)}, {json.dumps(value)})" for name, value in missing
                )
                projected_function = function.text.replace(request, projected_request, 1)
                break
            if projected_function is not None:
                replacements.append((function.text, projected_function))
                continue
            for call in local.nodes("call_expression"):
                callee = call.child_by_field_name("function")
                arguments = call.child_by_field_name("arguments")
                if (
                    callee is None
                    or callee.text != b"Ok"
                    or arguments is None
                    or len(arguments.named_children) != 1
                ):
                    continue
                argument = arguments.named_children[0].text.decode("utf-8", errors="replace")
                if "Request::get" not in argument and not re.fullmatch(r"[A-Za-z_]\w*", argument):
                    continue
                projected = argument + "".join(
                    f".header({json.dumps(name)}, {json.dumps(value)})" for name, value in missing
                )
                replacements.append((function.text, function.text.replace(argument, projected, 1)))
                break
        for original, normalized in replacements:
            content = content.replace(original, normalized, 1)
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


def _project_recovered_detail_api_envelope(
    ir: SourceIR,
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    if not decompiled_detail_uses_api_envelope(ir.files):
        return files
    inspection = RustInspection(
        generated.content for generated in files if generated.path.endswith(".rs")
    )
    envelope = inspection.struct_named("ApiResponse")
    if envelope is None or inspection.struct_field_type("ApiResponse", "results") != "T":
        return files

    updated: list[GeneratedFile] = []
    for generated in files:
        content = generated.content
        if not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        replacements: list[tuple[str, str]] = []
        for function in RustInspection.from_content(content).functions:
            signature = function.text.split("{", 1)[0]
            result = re.search(
                r"->\s*(?:aidoku::)?Result\s*<\s*"
                r"(?P<detail>(?:[A-Za-z_]\w*::)*(?:Comic)?DetailResult)\s*>",
                signature,
            )
            if (
                result is None
                or "detail" not in function.name.lower()
                or "comic2" not in function.text
                or "ApiResponse<" in function.text
            ):
                continue
            for call in RustInspection.from_content(function.text).nodes("call_expression"):
                callee = call.child_by_field_name("function")
                if (
                    callee is None
                    or not callee.text.decode("utf-8", errors="replace").endswith("get_json")
                    or call.parent is None
                    or call.parent.type != "block"
                    or call.parent.parent is None
                    or call.parent.parent.type != "function_item"
                ):
                    continue
                original = call.text.decode("utf-8", errors="replace")
                indent = " " * call.start_point.column
                inner = indent + "    "
                replacement = (
                    "{\n"
                    f"{inner}let response: ApiResponse<{result.group('detail')}> = {original}?;\n"
                    f"{inner}Ok(response.results)\n"
                    f"{indent}}}"
                )
                replacements.append(
                    (function.text, function.text.replace(original, replacement, 1))
                )
                break
        for original, replacement in replacements:
            content = content.replace(original, replacement, 1)
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


def _project_recovered_rank_item_wrapper(
    ir: SourceIR,
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    rust_content = "\n".join(
        generated.content for generated in files if generated.path.endswith(".rs")
    )
    if not decompiled_rank_list_wraps_comic(ir.files) or "struct C2aRankItem" in rust_content:
        return files

    inspection = RustInspection(
        generated.content for generated in files if generated.path.endswith(".rs")
    )
    updated: list[GeneratedFile] = []
    projected = False
    for generated in files:
        content = generated.content
        if projected or not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        for function in RustInspection.from_content(content).named("get_search_manga_list"):
            if "/ranks" not in function.text:
                continue
            response = re.search(
                r"(?m)^(?P<indent>[ \t]*)let\s+(?P<response>[A-Za-z_]\w*)\s*:\s*"
                r"(?P<envelope>[A-Za-z_]\w*)\s*<\s*"
                r"(?P<inner>[A-Za-z_]\w*(?:\s*<\s*[A-Za-z_]\w*\s*>)?)\s*>\s*=\s*"
                r"(?P<fetch>self\.[A-Za-z_]\w*\(\s*(?P<url>[A-Za-z_]\w*)\s*\)\?)\s*;",
                function.text,
            )
            if response is None:
                continue
            generic = re.fullmatch(
                r"[A-Za-z_]\w*\s*<\s*(?P<comic>[A-Za-z_]\w*)\s*>",
                response.group("inner"),
            )
            comic = generic.group("comic") if generic is not None else None
            if comic is None:
                list_type = inspection.struct_field_type(response.group("inner"), "list")
                list_item = (
                    re.fullmatch(r"Vec\s*<\s*(?P<comic>[A-Za-z_]\w*)\s*>", list_type)
                    if list_type is not None
                    else None
                )
                comic = list_item.group("comic") if list_item is not None else None
            if comic is None:
                continue
            direct_mapper = re.search(
                rf"\b{re.escape(response.group('response'))}\.results\.list\s*"
                r"\.into_iter\(\)\s*\.map\(\s*"
                r"(?P<mapper>[A-Za-z_]\w*::[A-Za-z_]\w*)\s*\)",
                function.text,
            )
            mapper = direct_mapper.group("mapper") if direct_mapper is not None else None
            if mapper is None:
                converter = next(
                    (
                        candidate
                        for candidate in inspection.functions
                        if re.search(
                            rf"\(\s*(?:(?P<self>&self)\s*,\s*)?[A-Za-z_]\w*\s*:\s*"
                            rf"{re.escape(comic)}\s*\)\s*->\s*Manga\b",
                            candidate.text.split("{", 1)[0],
                        )
                    ),
                    None,
                )
                if converter is not None:
                    signature = converter.text.split("{", 1)[0]
                    mapper = (
                        f"self.{converter.name}"
                        if "&self" in signature
                        else f"Self::{converter.name}"
                    )
            if mapper is None:
                continue
            indent = response.group("indent")
            inner = indent + "    "
            result = response.group("response")
            branch = (
                f'{indent}if {response.group("url")}.contains("/ranks") {{\n'
                f"{inner}let {result}: {response.group('envelope')}<C2aRankResult> = "
                f"{response.group('fetch')};\n"
                f"{inner}let has_next_page = {result}.results.total >= "
                f"{result}.results.offset + {result}.results.limit;\n"
                f"{inner}return Ok(MangaPageResult {{\n"
                f"{inner}    entries: {result}.results.list.into_iter()\n"
                f"{inner}        .map(|item| {mapper}(item.comic))\n"
                f"{inner}        .collect(),\n"
                f"{inner}    has_next_page,\n"
                f"{inner}}});\n"
                f"{indent}}}\n"
            )
            begin = function.node.start_byte + response.start()
            item = (
                "#[derive(aidoku::serde::Deserialize)]\n"
                f"struct C2aRankItem {{ comic: {comic} }}\n\n"
                "#[derive(aidoku::serde::Deserialize)]\n"
                "struct C2aRankResult {\n"
                "    #[serde(default)]\n"
                "    list: aidoku::alloc::Vec<C2aRankItem>,\n"
                "    #[serde(default)]\n"
                "    limit: i32,\n"
                "    #[serde(default)]\n"
                "    offset: i32,\n"
                "    #[serde(default)]\n"
                "    total: i32,\n"
                "}"
            )
            content = content[:begin] + branch + content[begin:]
            content = content.rstrip() + "\n\n" + item + "\n"
            projected = True
            break
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


def _variant_profile_domains(ir: SourceIR, variant_name: str) -> tuple[str, ...]:
    variant_token = re.sub(r"[^a-z0-9]", "", variant_name.lower())
    domains: list[str] = []
    for profile in ir.request_header_profiles:
        profile_token = re.sub(r"[^a-z0-9]", "", profile.name.lower())
        if variant_token and variant_token in profile_token:
            domains.extend(profile.domains)
    return tuple(dict.fromkeys(domains))


def _project_recovered_chapter_page_variants(
    ir: SourceIR,
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    rules: list[tuple[str, str, str, tuple[str, ...]]] = []
    for route in ir.chapter_page_routes:
        default = next((variant for variant in route.variants if variant.is_default), None)
        if default is None or len(default.replacements) != 1:
            continue
        replacement = default.replacements[0]
        for variant in route.variants:
            domains = _variant_profile_domains(ir, variant.name)
            if (
                variant.is_default
                or not domains
                or variant.strip_prefix != default.strip_prefix
                or variant.replacements
            ):
                continue
            helper = "c2a_is_" + re.sub(r"[^a-z0-9]+", "_", variant.name.lower()).strip("_")
            rules.append((replacement.old, replacement.new, helper + "_domain", domains))
    if not rules:
        return files

    updated: list[GeneratedFile] = []
    for generated in files:
        content = generated.content
        if not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        for old, new, helper, domains in rules:
            if f"fn {helper}(" in content:
                continue
            old_literal = json.dumps(old)
            new_literal = json.dumps(new)
            pattern = re.compile(
                rf"(?m)^(?P<indent>[ \t]*)let\s+(?P<variable>[A-Za-z_]\w*)\s*=\s*"
                rf"(?P<base>[A-Za-z_]\w*[^;]{{0,700}}?)"
                rf"\.replace\(\s*{re.escape(old_literal)}\s*,\s*"
                rf"{re.escape(new_literal)}\s*\)\s*;"
            )
            match = pattern.search(content)
            if match is None:
                continue
            indent = match.group("indent")
            inner = indent + "    "
            variable = match.group("variable")
            base = match.group("base").rstrip()
            replacement = (
                f"{indent}let c2a_chapter_key = {base};\n"
                f"{indent}let {variable} = if {helper}(&api_domain()) {{\n"
                f"{inner}aidoku::alloc::String::from(c2a_chapter_key)\n"
                f"{indent}}} else {{\n"
                f"{inner}c2a_chapter_key.replace({old_literal}, {new_literal})\n"
                f"{indent}}};"
            )
            domain_patterns = " | ".join(json.dumps(domain) for domain in domains)
            helper_content = (
                f"fn {helper}(domain: &str) -> bool {{\n    matches!(domain, {domain_patterns})\n}}"
            )
            content = (
                content[: match.start()]
                + replacement
                + content[match.end() :].rstrip()
                + "\n\n"
                + helper_content
                + "\n"
            )
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


def _project_recovered_chapter_image_resolution(
    ir: SourceIR,
    files: list[GeneratedFile],
    *,
    setting_defaults: Mapping[str, str],
    setting_values: Mapping[str, tuple[str, ...]],
) -> list[GeneratedFile]:
    policy = ir.image_url_policy
    if policy is None or policy.chapter_resolution_regex != r"\d+(?=x\.(?:jpg|webp)$)":
        return files
    rust_content = "\n".join(
        generated.content for generated in files if generated.path.endswith(".rs")
    )
    if "c2a_translate_chapter_resolution" in rust_content or (
        "resolution" in rust_content
        and 'ends_with(".jpg")' in rust_content
        and 'ends_with(".webp")' in rust_content
    ):
        return files
    resolution_key = next(
        (
            key
            for key, values in setting_values.items()
            if key.rsplit(".", 1)[-1] == "resolution"
            and values
            and all(re.fullmatch(r"resolution\.r[1-9][0-9]*", value) for value in values)
        ),
        None,
    )
    if resolution_key is None:
        return files
    values = setting_values[resolution_key]
    default = setting_defaults.get(resolution_key, values[-1])
    arms = "\n".join(
        f"        Some({json.dumps(value)}) => {json.dumps(value.rsplit('.r', 1)[-1])},"
        for value in values
    )
    helper = (
        "fn c2a_translate_chapter_resolution(url: aidoku::alloc::String) "
        "-> aidoku::alloc::String {\n"
        "    let resolution = match "
        "aidoku::imports::defaults::defaults_get::<aidoku::alloc::String>"
        f"({json.dumps(resolution_key)}).as_deref() {{\n"
        f"{arms}\n"
        f"        _ => {json.dumps(default.rsplit('.r', 1)[-1])},\n"
        "    };\n"
        '    let suffix_start = if url.ends_with(".jpg") {\n'
        "        Some(url.len() - 4)\n"
        '    } else if url.ends_with(".webp") {\n'
        "        Some(url.len() - 5)\n"
        "    } else {\n"
        "        None\n"
        "    };\n"
        "    if let Some(suffix_start) = suffix_start {\n"
        "        let before_suffix = &url[..suffix_start];\n"
        "        if let Some(x_pos) = before_suffix.rfind('x') {\n"
        "            let before_x = &before_suffix[..x_pos];\n"
        "            let digits_start = before_x\n"
        "                .rfind(|character: char| !character.is_ascii_digit())\n"
        "                .map_or(0, |position| position + 1);\n"
        "            if digits_start < x_pos {\n"
        '                return aidoku::alloc::format!("{}{}{}", '
        "&url[..digits_start], resolution, &url[x_pos..]);\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "    url\n"
        "}"
    )

    updated: list[GeneratedFile] = []
    helper_added = False
    for generated in files:
        content = generated.content
        if not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        edits: list[tuple[int, int, bytes]] = []
        inspection = RustInspection.from_content(content)
        for function in inspection.functions:
            if "page" not in function.name.lower():
                continue
            for call in RustInspection.from_content(function.text).nodes("call_expression"):
                callee = call.child_by_field_name("function")
                arguments = call.child_by_field_name("arguments")
                if callee is None or arguments is None or not arguments.named_children:
                    continue
                if callee.text.decode("utf-8", errors="replace") not in {
                    "PageContent::url",
                    "PageContent::url_context",
                }:
                    continue
                url = arguments.named_children[0]
                url_text = url.text.decode("utf-8", errors="replace")
                if "c2a_translate_chapter_resolution" in url_text:
                    continue
                edits.append(
                    (
                        function.node.start_byte + url.start_byte,
                        function.node.start_byte + url.end_byte,
                        f"c2a_translate_chapter_resolution({url_text})".encode(),
                    )
                )
        encoded = content.encode("utf-8")
        for begin, end, replacement in sorted(edits, reverse=True):
            encoded = encoded[:begin] + replacement + encoded[end:]
        content = encoded.decode("utf-8")
        if edits and not helper_added:
            content = content.rstrip() + "\n\n" + helper + "\n"
            helper_added = True
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


def _rust_filter_expression(spec: SourceFilterSpec) -> str:
    base = (
        f"id: {json.dumps(spec.id)}.into(), "
        f"title: Some({json.dumps(spec.title, ensure_ascii=False)}.into()), "
    )
    if spec.kind == "check":
        return (
            "aidoku::CheckFilter { " + base + "default: Some(false), ..Default::default() }.into()"
        )
    if spec.kind == "text":
        return "aidoku::TextFilter { " + base + "..Default::default() }.into()"
    options = ", ".join(
        f"{json.dumps(item.title, ensure_ascii=False)}.into()" for item in spec.options
    )
    ids = ", ".join(f"{json.dumps(item.value, ensure_ascii=False)}.into()" for item in spec.options)
    common = base + f"options: aidoku::alloc::vec![{options}], "
    if spec.kind == "sort":
        return (
            "aidoku::SortFilter { "
            + common
            + "default: Some(aidoku::SortFilterDefault { "
            + f"index: {spec.default_index}, ascending: "
            + str(bool(spec.default_ascending)).lower()
            + " }), ..Default::default() }.into()"
        )
    return (
        "aidoku::SelectFilter { "
        + common
        + f"ids: Some(aidoku::alloc::vec![{ids}]), "
        + f"default: Some({json.dumps(spec.options[spec.default_index].value)}.into()), "
        + "..Default::default() }.into()"
    )


def _synthesize_recovered_dynamic_filters(
    ir: SourceIR,
    files: list[GeneratedFile],
    *,
    source_struct: str,
    implemented_traits: list[str],
) -> tuple[list[GeneratedFile], list[str]]:
    endpoint = decompiled_dynamic_filter_endpoint(ir.files)
    inspection = RustInspection(
        generated.content for generated in files if generated.path.endswith(".rs")
    )
    if (
        Capability.DYNAMIC_FILTERS not in ir.capabilities
        or "DynamicFilters" in implemented_traits
        or endpoint is None
        or inspection.has_function("get_dynamic_filters")
    ):
        return files, implemented_traits
    envelope = next(
        (
            item.name
            for item in inspection.structs
            if inspection.struct_field_type(item.name, "results") == "T"
        ),
        None,
    )
    json_helper = next(
        (
            function.name
            for function in inspection.functions
            if ".get_json_owned()" in function.text
            and re.search(r"Result\s*<\s*T\s*>", function.text.split("{", 1)[0])
        ),
        None,
    )
    api_helper = next(
        (
            function.name
            for function in inspection.functions
            if function.name == "api_url" or function.name.endswith("_api_url")
        ),
        None,
    )
    if envelope is None or json_helper is None or api_helper is None:
        return files, implemented_traits
    routes = {route for function in inspection.functions for route in function.route_literals}
    if any(route.startswith("/api/v3/") for route in routes) and not endpoint.startswith(
        "/api/v3/"
    ):
        endpoint = "/api/v3" + endpoint
    implementation = f"""
#[derive(aidoku::serde::Deserialize)]
struct C2aDynamicFilterTheme {{
    #[serde(default)]
    name: aidoku::alloc::String,
    #[serde(default, rename = "path_word")]
    path_word: aidoku::alloc::String,
}}

#[derive(aidoku::serde::Deserialize)]
struct C2aDynamicFilterResult {{
    #[serde(default, rename = "list")]
    themes: aidoku::alloc::Vec<C2aDynamicFilterTheme>,
}}

impl DynamicFilters for {source_struct} {{
    fn get_dynamic_filters(&self) -> Result<aidoku::alloc::Vec<Filter>> {{
        let response: {envelope}<C2aDynamicFilterResult> =
            self.{json_helper}(Self::{api_helper}({json.dumps(endpoint)}))?;
        let mut options = aidoku::alloc::vec!["全部".into()];
        let mut ids = aidoku::alloc::vec!["".into()];
        for theme in response.results.themes {{
            if !theme.path_word.is_empty() {{
                options.push(theme.name.into());
                ids.push(theme.path_word.into());
            }}
        }}
        Ok(aidoku::alloc::vec![aidoku::SelectFilter {{
            id: "theme".into(),
            title: Some("題材".into()),
            options,
            ids: Some(ids),
            default: Some("".into()),
            ..Default::default()
        }}.into()])
    }}
}}
""".strip()

    updated: list[GeneratedFile] = []
    projected = False
    owner_pattern = re.compile(rf"\bimpl\s+{re.escape(source_struct)}\s*\{{")
    for generated in files:
        content = generated.content
        if not projected and generated.path.endswith(".rs") and owner_pattern.search(content):
            content = content.rstrip() + "\n\n" + implementation + "\n"
            projected = True
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    if not projected:
        return files, implemented_traits
    return updated, [*implemented_traits, "DynamicFilters"]


def _project_recovered_dynamic_filters(
    ir: SourceIR,
    files: list[GeneratedFile],
    *,
    implemented_traits: list[str],
) -> list[GeneratedFile]:
    if not ir.filter_specs or "DynamicFilters" not in implemented_traits:
        return files
    updated: list[GeneratedFile] = []
    projected = False
    for generated in files:
        content = generated.content
        if projected or not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        for function in RustInspection.from_content(content).functions:
            if function.name != "get_dynamic_filters":
                continue
            missing = [
                spec
                for spec in ir.filter_specs
                if re.search(rf"\bid\s*:\s*{re.escape(json.dumps(spec.id))}", function.text) is None
            ]
            if not missing:
                projected = True
                break
            for call in RustInspection.from_content(function.text).nodes("call_expression"):
                callee = call.child_by_field_name("function")
                arguments = call.child_by_field_name("arguments")
                if (
                    callee is None
                    or callee.text.decode("utf-8", errors="replace") != "Ok"
                    or arguments is None
                    or len(arguments.named_children) != 1
                    or call.parent is None
                    or call.parent.type != "block"
                ):
                    continue
                original = call.text.decode("utf-8", errors="replace")
                current = arguments.named_children[0].text.decode("utf-8", errors="replace")
                indent = " " * call.start_point.column
                inner = indent + "    "
                pushes = "\n".join(
                    f"{inner}c2a_filters.push({_rust_filter_expression(spec)});" for spec in missing
                )
                replacement = (
                    "{\n"
                    f"{inner}let mut c2a_filters = {current};\n"
                    f"{pushes}\n"
                    f"{inner}Ok(c2a_filters)\n"
                    f"{indent}}}"
                )
                content = content.replace(original, replacement, 1)
                projected = True
                break
            break
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


def _project_recovered_dynamic_filter_queries(
    ir: SourceIR,
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    endpoint = decompiled_dynamic_filter_endpoint(ir.files)
    if endpoint is None or "theme" not in endpoint:
        return files
    updated: list[GeneratedFile] = []
    for generated in files:
        content = generated.content
        if not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        replacements: list[tuple[str, str]] = []
        for function in RustInspection.from_content(content).named("get_search_manga_list"):
            if '"theme"' in function.text:
                continue
            selector = re.search(
                r"(?P<selector>(?:Self::|self\.)?[A-Za-z_]\w*)"
                r"\(\s*&filters\s*,\s*\"[^\"]+\"\s*\)"
                r"\.unwrap_or\(\s*\"\"\s*\)",
                function.text,
            )
            update = re.search(
                r"(?m)^(?P<indent>[ \t]*)(?P<url>[A-Za-z_]\w*)\.push_str\("
                r'"&_update=true"\);',
                function.text,
            )
            opening = function.text.find("{")
            if selector is None or update is None or opening < 0:
                continue
            indent = update.group("indent")
            selection = (
                f"\n{indent}let c2a_theme = {selector.group('selector')}"
                '(&filters, "theme").unwrap_or("");'
            )
            normalized = function.text[: opening + 1] + selection + function.text[opening + 1 :]
            update_statement = update.group(0)
            query = (
                f"{indent}if !c2a_theme.is_empty() {{\n"
                f"{indent}    {update.group('url')}.push_str(&format!("
                '"&theme={}", c2a_theme));\n'
                f"{indent}}}\n"
                f"{update_statement}"
            )
            normalized = normalized.replace(update_statement, query, 1)
            replacements.append((function.text, normalized))
        for original, normalized in replacements:
            content = content.replace(original, normalized, 1)
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


_CHECK_FILTER_HELPER = """
fn c2a_check_value(filters: &[aidoku::FilterValue], id: &str) -> bool {
    filters.iter().any(|filter| {
        matches!(
            filter,
            aidoku::FilterValue::Check { id: filter_id, value }
                if filter_id == id && *value > 0
        )
    })
}
""".strip()


def _project_recovered_check_filter_mappings(
    ir: SourceIR,
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    check_ids = [spec.id for spec in ir.filter_specs if spec.kind == "check"]
    if not check_ids:
        return files
    updated: list[GeneratedFile] = []
    for generated in files:
        content = generated.content
        if not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        normalized = content
        for filter_id in check_ids:
            literal = re.escape(json.dumps(filter_id))
            normalized = re.sub(
                rf"(?:Self::|self\.)?[A-Za-z_]\w*\(\s*"
                rf"(?P<filters>&?[A-Za-z_]\w*)\s*,\s*{literal}\s*\)\.is_some\(\)",
                lambda match, filter_id=filter_id: (
                    f"c2a_check_value({match.group('filters')}, {json.dumps(filter_id)})"
                ),
                normalized,
            )
        if normalized != content and "fn c2a_check_value(" not in normalized:
            normalized = normalized.rstrip() + "\n\n" + _CHECK_FILTER_HELPER + "\n"
        updated.append(
            generated.model_copy(update={"content": normalized})
            if normalized != content
            else generated
        )
    return updated


def normalize_generation_manifest(
    ir: SourceIR,
    manifest: GenerationManifest,
) -> GenerationManifest:
    """Project deterministic Rust compatibility and recovered behavior into a manifest."""
    resources = GeneratedResources(manifest)
    setting_defaults = resources.setting_defaults()
    setting_keys = resources.setting_keys()
    setting_values = resources.setting_values()
    prequeried_url_helpers = _prequeried_url_helpers(manifest)
    request_builder_helpers = _request_builder_helpers(manifest)
    preserve_cover_urls = bool(ir.image_url_policy and ir.image_url_policy.preserve_cover_urls)
    implemented_traits = list(manifest.implemented_traits)
    seeded_files, implemented_traits = _synthesize_recovered_dynamic_filters(
        ir,
        list(manifest.files),
        source_struct=manifest.source_struct,
        implemented_traits=implemented_traits,
    )
    seeded_files = _project_recovered_rank_item_wrapper(ir, seeded_files)
    files = []
    changed = any(
        before.content != after.content
        for before, after in zip(manifest.files, seeded_files, strict=True)
    ) or implemented_traits != list(manifest.implemented_traits)
    for generated in seeded_files:
        content = generated.content
        if generated.path.endswith(".rs"):
            content = normalize_pinned_aidoku_rust(
                content,
                allow_dead_code=generated.path != "src/lib.rs",
                setting_defaults=setting_defaults,
                setting_keys=setting_keys,
                setting_values=setting_values,
                prequeried_url_helpers=prequeried_url_helpers,
                preserve_cover_urls=preserve_cover_urls,
                public_base_url=ir.metadata.base_url if ir.relative_url_keys else None,
                chapter_key_templates=tuple(
                    route.chapter_key_template for route in ir.chapter_page_routes
                ),
                request_builder_helpers=request_builder_helpers,
            )
        changed |= content != generated.content
        files.append(generated.model_copy(update={"content": content}))
    if ir.source_format == "decompiled_apk":
        optionalized = _skip_unused_decompiled_dto_fields(files)
        changed |= any(
            before.content != after.content
            for before, after in zip(files, optionalized, strict=True)
        )
        files = optionalized
        aliased = _project_recovered_nested_dto_aliases(ir, files)
        changed |= any(
            before.content != after.content for before, after in zip(files, aliased, strict=True)
        )
        files = aliased
        defaulted = _project_recovered_nullable_dto_defaults(ir, files)
        changed |= any(
            before.content != after.content for before, after in zip(files, defaulted, strict=True)
        )
        files = defaulted
    header_projected = _project_recovered_request_headers(
        ir,
        files,
        setting_defaults=setting_defaults,
        setting_values=setting_values,
    )
    changed |= any(
        before.content != after.content
        for before, after in zip(files, header_projected, strict=True)
    )
    files = header_projected
    envelope_projected = _project_recovered_detail_api_envelope(ir, files)
    changed |= any(
        before.content != after.content
        for before, after in zip(files, envelope_projected, strict=True)
    )
    files = envelope_projected
    route_projected = _project_recovered_chapter_page_variants(ir, files)
    changed |= any(
        before.content != after.content
        for before, after in zip(files, route_projected, strict=True)
    )
    files = route_projected
    resolution_projected = _project_recovered_chapter_image_resolution(
        ir,
        files,
        setting_defaults=setting_defaults,
        setting_values=setting_values,
    )
    changed |= any(
        before.content != after.content
        for before, after in zip(files, resolution_projected, strict=True)
    )
    files = resolution_projected
    filters_projected = _project_recovered_dynamic_filters(
        ir,
        files,
        implemented_traits=implemented_traits,
    )
    changed |= any(
        before.content != after.content
        for before, after in zip(files, filters_projected, strict=True)
    )
    files = filters_projected
    query_projected = _project_recovered_dynamic_filter_queries(ir, files)
    changed |= any(
        before.content != after.content
        for before, after in zip(files, query_projected, strict=True)
    )
    files = query_projected
    check_projected = _project_recovered_check_filter_mappings(ir, files)
    changed |= any(
        before.content != after.content
        for before, after in zip(files, check_projected, strict=True)
    )
    files = check_projected
    topologized = _normalize_generated_module_topology(files)
    changed |= any(
        before.content != after.content for before, after in zip(files, topologized, strict=True)
    )
    files = topologized
    return (
        manifest.model_copy(update={"files": files, "implemented_traits": implemented_traits})
        if changed
        else manifest
    )


def _environment() -> Environment:
    template_dir = resource_files("convert2aidoku").joinpath("resources", "templates")
    return Environment(
        loader=__import__("jinja2").FileSystemLoader(str(template_dir)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )


def _dependency_context(names: set[str]) -> Mapping[str, PinnedDependency]:
    evaluation = evaluate_dependency_policy(names)
    if evaluation.disallowed:
        noun = "dependency" if len(evaluation.disallowed) == 1 else "dependencies"
        raise SecurityError(
            f"generated source requested disallowed {noun}: " + ", ".join(evaluation.disallowed)
        )
    return evaluation.cargo_dependencies


def _write_cargo(
    destination: Path,
    ir: SourceIR,
    dependency_names: set[str],
    *,
    invalidate_lock: bool = False,
) -> None:
    template = _environment().get_template("Cargo.toml.j2")
    package = re.sub(r"[^a-zA-Z0-9_-]+", "_", ir.metadata.package_name)
    cargo_path = destination / "Cargo.toml"
    rendered = template.render(
        package_name=package,
        aidoku_repository=AIDOKU_RS_REPOSITORY,
        aidoku_rev=AIDOKU_RS_REV,
        dependencies=_dependency_context(dependency_names),
    )
    previous = cargo_path.read_text(encoding="utf-8") if cargo_path.is_file() else None
    if invalidate_lock or (previous is not None and previous != rendered):
        (destination / "Cargo.lock").unlink(missing_ok=True)
    cargo_path.write_text(rendered, encoding="utf-8")


def create_scaffold(destination: Path, ir: SourceIR, resolved: ResolvedSource) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    (destination / ".cargo").mkdir()
    (destination / "src").mkdir()
    (destination / "res").mkdir()

    config = _environment().get_template("config.toml.j2").render()
    (destination / ".cargo" / "config.toml").write_text(config, encoding="utf-8")
    _write_cargo(destination, ir, set())
    GeneratedSourceMetadata.from_source_ir(ir).write(destination)
    create_aidoku_icon(
        find_icon(resolved.module_path),
        destination / "res" / "icon.png",
        initials=ir.metadata.name,
    )
    copied_license = copy_input_license(resolved, destination / "LICENSE.input")
    provenance = [
        "# Generated source provenance",
        "",
        f"- Input: `{ir.input_ref}`",
        f"- Input commit: `{ir.commit or 'unknown'}`",
        f"- Input license copied: `{copied_license or 'not found'}`",
        "- This output may be a derivative work. Verify redistribution rights before publishing.",
        "",
    ]
    (destination / "PROVENANCE.md").write_text("\n".join(provenance), encoding="utf-8")


def _safe_destination(root: Path, relative: str) -> Path:
    safe = validate_generated_path(relative)
    destination = root.joinpath(*safe.split("/"))
    root_resolved = root.resolve()
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    current = root
    for part in Path(safe).parent.parts:
        current = current / part
        if current.is_symlink():
            raise SecurityError(f"refusing to write through a symbolic link: {relative}")
    resolved_parent = parent.resolve()
    if root_resolved != resolved_parent and root_resolved not in resolved_parent.parents:
        raise SecurityError(f"generated path escapes staging directory: {relative}")
    return destination


def apply_generation_manifest(
    destination: Path,
    ir: SourceIR,
    manifest: GenerationManifest,
    *,
    query: str | None,
) -> list[str]:
    manifest = normalize_generation_manifest(ir, manifest)
    dependency_names = {item.name for item in manifest.dependencies}
    resources = GeneratedResources(manifest)
    setting_defaults = resources.setting_defaults()
    setting_values = resources.setting_values()
    _write_cargo(destination, ir, dependency_names)

    manifest_paths = {item.path for item in manifest.files}
    for current in (destination / "src").rglob("*.rs"):
        relative = current.relative_to(destination).as_posix()
        if relative != "src/generated_smoke.rs" and relative not in manifest_paths:
            current.unlink()
    for optional in ("res/filters.json", "res/settings.json"):
        if optional not in manifest_paths:
            (destination / optional).unlink(missing_ok=True)

    generated_paths: list[str] = []
    for generated in manifest.files:
        content = generated.content
        if generated.path == "src/lib.rs":
            content = _remove_reserved_smoke_marker(content)
            if not re.match(r"\s*#!\[no_std\]", content):
                content = "#![no_std]\n\n" + content.lstrip()
        if generated.path.endswith(".rs"):
            content = normalize_pinned_aidoku_rust(
                content,
                allow_dead_code=generated.path != "src/lib.rs",
                setting_defaults=setting_defaults,
                setting_values=setting_values,
                prequeried_url_helpers=_prequeried_url_helpers(manifest),
                preserve_cover_urls=bool(
                    ir.image_url_policy and ir.image_url_policy.preserve_cover_urls
                ),
                public_base_url=ir.metadata.base_url if ir.relative_url_keys else None,
                chapter_key_templates=tuple(
                    route.chapter_key_template for route in ir.chapter_page_routes
                ),
                request_builder_helpers=_request_builder_helpers(manifest),
            )
        validate_generated_content(generated.path, content)
        target = _safe_destination(destination, generated.path)
        target.write_text(content.rstrip() + "\n", encoding="utf-8")
        generated_paths.append(generated.path)

    GeneratedSourceMetadata.load(destination).with_manifest_requirements(manifest).write(
        destination
    )

    lib_path = destination / "src" / "lib.rs"
    lib = lib_path.read_text(encoding="utf-8")
    smoke_module = "#[cfg(test)]\nmod generated_smoke;"
    lib_path.write_text(lib.rstrip() + "\n\n" + smoke_module + "\n", encoding="utf-8")

    smoke = (
        _environment()
        .get_template("smoke.rs.j2")
        .render(
            source_struct=manifest.source_struct,
            image_request_provider="ImageRequestProvider" in manifest.implemented_traits,
            listing_provider="ListingProvider" in manifest.implemented_traits,
            dynamic_filters="DynamicFilters" in manifest.implemented_traits,
            deep_link_handler="DeepLinkHandler" in manifest.implemented_traits,
            popular_listing=Capability.POPULAR in ir.capabilities,
            latest_listing=Capability.LATEST in ir.capabilities,
            query_expression=(
                f"Some({json.dumps(query, ensure_ascii=False)}.into())" if query else "None"
            ),
            static_filter_cases=GeneratedResources(manifest).static_filter_cases(),
        )
    )
    (destination / "src" / "generated_smoke.rs").write_text(smoke, encoding="utf-8")
    return sorted(generated_paths + ["src/generated_smoke.rs"])


def read_generated_files(destination: Path) -> list[dict[str, str]]:
    result = []
    for path in sorted((destination / "src").rglob("*.rs")):
        if path.name == "generated_smoke.rs":
            continue
        result.append(
            {
                "path": path.relative_to(destination).as_posix(),
                "content": _remove_reserved_smoke_marker(path.read_text(encoding="utf-8")),
            }
        )
    for name in ("filters.json", "settings.json"):
        path = destination / "res" / name
        if path.exists():
            result.append(
                {
                    "path": path.relative_to(destination).as_posix(),
                    "content": path.read_text(encoding="utf-8"),
                }
            )
    return result
