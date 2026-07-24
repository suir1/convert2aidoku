from __future__ import annotations

import json
import re
from collections.abc import Mapping
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined

from .constants import MAX_GENERATED_FILE_CHARS
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
_RUST_IDENTIFIER_NODES = {"identifier", "raw_identifier"}


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
            return relative_import and compact.startswith("aidoku::{")
        elif current.type in {"use_declaration", "source_file"}:
            break
        current = current.parent
    return False


def _validate_generated_rust_ast(path: str, content: str) -> None:
    """Reject compile-time I/O and ways to bypass the tool-owned smoke tests."""
    inspection = RustInspection.from_content(content)
    for node in inspection.nodes():
        identifier = _rust_identifier(node)
        if identifier == "std" and not _is_aidoku_imports_std(node):
            raise SecurityError(f"generated Rust uses std, which is forbidden: {path}")
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
    content = re.sub(
        r"(?<!::)\balloc::borrow::Cow\b",
        "aidoku::alloc::borrow::Cow",
        content,
    )
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        normalized = function.text
        for field, value in (("authors", "author"), ("artists", "artist")):
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
                rf"\b{field}:\s*Some\({value}\),",
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
    if re.search(r"\blet\s+mut\s+[A-Za-z_]\w*\s*=\s*manga\s*;", content):
        content = re.sub(
            r"\blet\s+path\s*=\s*&manga\.key\s*;",
            "let path = manga.key.clone();",
            content,
        )
    return content


def _normalize_struct_expression_defaults(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for node in RustInspection.from_content(content).nodes("struct_expression"):
        name = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name is None or body is None:
            continue
        type_name = name.text.decode("utf-8", errors="replace")
        text = node.text.decode("utf-8", errors="replace")
        if type_name not in {"Manga", "Chapter", "Page"} or "..Default::default()" in text:
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


def _normalize_resolution_regex(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).named("translate_resolution"):
        if "regex::Regex" not in function.text:
            continue
        replacement = """pub fn translate_resolution(url: &str, resolution: &str) -> String {
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
        replacements.append((function.text, replacement))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    return content


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


def _normalize_preserved_cover_urls(content: str, preserve_cover_urls: bool) -> str:
    if not preserve_cover_urls:
        return content
    return re.sub(
        r"(?P<receiver>\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
        r"\s*\.cover\s*\.as_deref\(\)\s*\.map\(\|\s*(?P<value>[A-Za-z_][A-Za-z0-9_]*)"
        r"\s*\|\s*(?:[A-Za-z_][A-Za-z0-9_]*::)*"
        r"[A-Za-z_][A-Za-z0-9_]*resolution[A-Za-z0-9_]*\(\s*(?P=value)\s*,[^)]*\)"
        r"\)\s*\.unwrap_or_default\(\)",
        r"\g<receiver>.cover.clone().unwrap_or_default()",
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
        if not RustInspection.compact_node(node).startswith("useaidoku::{"):
            continue
        normalized = text
        for trait in traits:
            if f"impl {trait}" in content:
                continue
            normalized = re.sub(rf"\b{trait}\s*,\s*", "", normalized)
            normalized = re.sub(rf",\s*\b{trait}\b", "", normalized)
        if normalized != text:
            replacements.append((text, normalized))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
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
        content = re.sub(
            rf"defaults_get::<String>\(\s*{re.escape(key_literal)}\s*\)"
            rf"\s*(?:\.unwrap_or_default\(\)|\.unwrap_or_else\(\|\|\s*"
            rf"(?:String::from\(\s*{rust_string}\s*\)|{rust_string}\.into\(\))\s*\))",
            f"defaults_get::<String>({key_literal})"
            f".unwrap_or_else(|| String::from({default_literal}))",
            content,
        )
        content = re.sub(
            rf"(?P<prefix>defaults_get(?:::<String>)?\(\s*{re.escape(key_literal)}\s*\)"
            rf"[^;]{{0,500}}?\.unwrap_or_else\(\|\|\s*){rust_string}\.to_string\(\)\s*\)",
            lambda match, literal=default_literal: (
                f"{match.group('prefix')}String::from({literal}))"
            ),
            content,
        )
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
            + f"    let platform = defaults_get::<String>({json.dumps(key)})\n"
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
    setting_values: Mapping[str, tuple[str, ...]] | None = None,
    prequeried_url_helpers: set[str] | None = None,
    preserve_cover_urls: bool = False,
) -> str:
    """Apply small type-safe compatibility rewrites for the pinned Aidoku/Rust APIs."""
    if allow_dead_code and not re.search(
        r"#!\[allow\([^\]]*\bdead_code\b",
        content,
    ):
        content = "#![allow(dead_code)]\n" + content.lstrip()
    content = content.replace("aidoku::std::filters::SelectFilter", "aidoku::SelectFilter")
    content = content.replace("aidoku::filter::SelectFilter", "aidoku::SelectFilter")
    content = _normalize_select_filter_import(content)
    content = _remove_macro_only_trait_imports(content)
    content = content.replace("RequestError::new(", "aidoku::AidokuError::message(")
    if content.count("RequestError") == 1:
        content = re.sub(
            r"use\s+aidoku::imports::net::\{\s*Request\s*,\s*RequestError\s*\};",
            "use aidoku::imports::net::Request;",
            content,
        )
    if re.search(r"let\s+host\s*=\s*absolute\b", content):
        content = content.replace("Request::get(absolute)?", "Request::get(absolute.clone())?")
    content = re.sub(
        r'"(?:\\.|[^"\\])*"',
        lambda match: match.group(0).replace("}//chapters", "}/chapters"),
        content,
    )
    content = _normalize_idempotent_get_retry(content)
    content = _normalize_pinned_model_shapes(content)
    content = _normalize_struct_expression_defaults(content)
    content = _normalize_resolution_regex(content)
    content = _normalize_prequeried_url_helpers(content, prequeried_url_helpers)
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
    content = _normalize_generated_setting_defaults(content, setting_defaults)
    content = _normalize_resolution_setting(content, setting_defaults, setting_values)
    content = _normalize_platform_header_setting(content, setting_defaults, setting_values)
    content = _inject_no_std_macro_imports(content)
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


def _prequeried_url_helpers(manifest: GenerationManifest) -> set[str]:
    helpers: set[str] = set()
    for generated in manifest.files:
        if not generated.path.endswith(".rs"):
            continue
        for function in RustInspection.from_content(generated.content).functions:
            if re.search(r'"(?:\\.|[^"\\])*\?(?:\\.|[^"\\])*"', function.text):
                helpers.add(function.name)
    return helpers


def _optionalize_unused_decompiled_dto_strings(
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    rust_content = "\n".join(
        generated.content for generated in files if generated.path.endswith(".rs")
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
                if field.type_text != "String" or re.search(
                    rf"\.\s*{re.escape(field.name)}\b", rust_content
                ):
                    continue
                type_node = field.node.child_by_field_name("type")
                if type_node is not None:
                    edits.append((type_node.start_byte, type_node.end_byte, b"Option<String>"))
        for start, end, replacement in sorted(edits, reverse=True):
            raw[start:end] = replacement
        content = raw.decode()
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
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
    setting_values = resources.setting_values()
    prequeried_url_helpers = _prequeried_url_helpers(manifest)
    preserve_cover_urls = bool(ir.image_url_policy and ir.image_url_policy.preserve_cover_urls)
    files = []
    changed = False
    for generated in manifest.files:
        content = generated.content
        if generated.path.endswith(".rs"):
            content = normalize_pinned_aidoku_rust(
                content,
                allow_dead_code=generated.path != "src/lib.rs",
                setting_defaults=setting_defaults,
                setting_values=setting_values,
                prequeried_url_helpers=prequeried_url_helpers,
                preserve_cover_urls=preserve_cover_urls,
            )
        changed |= content != generated.content
        files.append(generated.model_copy(update={"content": content}))
    if ir.source_format == "decompiled_apk":
        optionalized = _optionalize_unused_decompiled_dto_strings(files)
        changed |= any(
            before.content != after.content
            for before, after in zip(files, optionalized, strict=True)
        )
        files = optionalized
    return manifest.model_copy(update={"files": files}) if changed else manifest


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
            query_expression=(f"Some({json.dumps(query)}.into())" if query else "None"),
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
