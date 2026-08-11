from __future__ import annotations

import re
from collections.abc import Callable

from .constants import AIDOKU_ROOT_NAMES as _AIDOKU_ROOT_NAMES
from .normalization_trace import NormalizationTrace
from .rust_inspection import RustInspection
from .rust_inspection import rust_identifier as _rust_identifier


def _alloc_macro_is_imported(content: str, name: str) -> bool:
    for use in RustInspection.from_content(content).nodes("use_declaration"):
        stack = [use]
        while stack:
            node = stack.pop()
            stack.extend(reversed(node.children))
            if _rust_identifier(node) != name:
                continue
            parent = node.parent
            if (
                parent is not None
                and parent.type == "scoped_identifier"
                and parent.parent == use
                and RustInspection.compact_node(parent) == f"aidoku::alloc::{name}"
            ):
                return True
            if parent is None or parent.type != "use_list":
                continue
            current = parent.parent
            relative_alloc = False
            while current is not None and current != use:
                compact = RustInspection.compact_node(current)
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


_AIDOKU_ROOT_REEXPORT_MODULES = ("error", "filters", "model", "source", "traits")


def _flatten_grouped_use_namespace(content: str, namespace: str) -> str:
    marker = re.compile(rf"\b{re.escape(namespace)}\s*::\s*\{{")
    while match := marker.search(content):
        opening = match.end() - 1
        depth = 0
        closing = None
        for index in range(opening, len(content)):
            if content[index] == "{":
                depth += 1
            elif content[index] == "}":
                depth -= 1
                if depth == 0:
                    closing = index
                    break
        if closing is None:
            break
        content = content[: match.start()] + content[opening + 1 : closing] + content[closing + 1 :]
    return re.sub(rf"\b{re.escape(namespace)}\s*::\s*(?=[A-Z])", "", content)


def remove_grouped_use_pattern(content: str, item_pattern: str) -> str:
    pattern = re.compile(
        rf"(?P<prefix>\{{|,)\s*{item_pattern}\s*(?P<comma>,)?",
        re.MULTILINE,
    )

    def remove(match: re.Match[str]) -> str:
        if match.group("prefix") == "{":
            return "{"
        return "," if match.group("comma") else ""

    return pattern.sub(remove, content)


def _normalize_aidoku_api_paths(content: str) -> str:
    content = content.replace("aidoku::net::", "aidoku::imports::net::")
    content = content.replace("aidoku::imports::serde_json", "serde_json")
    content = content.replace("aidoku::serde_json", "serde_json")
    content = content.replace("aidoku::alloc::Cow", "aidoku::alloc::borrow::Cow")
    content = re.sub(
        r"(?m)^\s*use\s+aidoku::imports::net::request\s*;\s*\n?",
        "",
        content,
    )
    replacements: list[tuple[str, str]] = []
    for node in RustInspection.from_content(content).nodes("use_declaration"):
        original = node.text.decode("utf-8", errors="replace")
        if not re.match(r"use\s+aidoku::", original):
            continue
        normalized = original
        for namespace in _AIDOKU_ROOT_REEXPORT_MODULES:
            normalized = _flatten_grouped_use_namespace(normalized, namespace)
        normalized = re.sub(
            r"(?<![:\w])defaults\s*::\s*defaults_get\b",
            "imports::defaults::defaults_get",
            normalized,
        )
        normalized = re.sub(
            r"\bimports\s*::\s*\{\s*imports\s*::",
            "imports::{",
            normalized,
        )
        compact = RustInspection.compact_node(node)
        if compact.startswith("useaidoku::{"):
            for name in ("Request", "Response"):
                normalized = re.sub(rf"\b{name}\s*,\s*", "", normalized)
                normalized = re.sub(rf",\s*\b{name}\b", "", normalized)
                normalized = re.sub(rf"\{{\s*{name}\s*\}}", "{}", normalized)
        normalized = re.sub(r"\bMangasPage\s*,\s*", "", normalized)
        normalized = re.sub(r",\s*MangasPage\b", "", normalized)
        normalized = re.sub(r",(?P<space>\s*),", r",\g<space>", normalized)
        if re.fullmatch(r"use\s+aidoku::\{\s*\}\s*;", normalized.strip()):
            normalized = ""
        if normalized != original:
            replacements.append((original, normalized))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    content = content.replace("error::AidokuError", "AidokuError")
    content = content.replace("aidoku::imports::json::Json", "serde_json::Value")
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
    content = content.replace(".body_string()", ".get_string()")
    content = re.sub(
        r"\b(?P<response>response|resp)\.text\(\)",
        r"\g<response>.get_string()",
        content,
    )
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
    for use in RustInspection.from_content(content).nodes("use_declaration"):
        compact_use = RustInspection.compact_node(use)
        if (
            compact_use.startswith("useaidoku::")
            and "alloc::" in compact_use
            and "string::ToString" in compact_use
        ):
            return True
        stack = [use]
        while stack:
            node = stack.pop()
            stack.extend(reversed(node.children))
            if _rust_identifier(node) != "ToString":
                continue
            current = node.parent
            relative_string = False
            relative_alloc = False
            while current is not None and current != use:
                compact = RustInspection.compact_node(current)
                if compact.startswith("aidoku::alloc::string::"):
                    return True
                if current.type == "scoped_use_list" and compact.startswith("string::{"):
                    relative_string = True
                elif (
                    current.type == "scoped_use_list"
                    and compact.startswith("alloc::{")
                    and relative_string
                ):
                    relative_alloc = True
                elif (
                    current.type == "scoped_use_list"
                    and compact.startswith("aidoku::{")
                    and relative_alloc
                ):
                    return True
                current = current.parent
    return False


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
        if re.search(rf"(?<![:\w]){re.escape(name)}\b", content)
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


def _normalize_shadowed_known_imports(content: str) -> str:
    if re.search(r"(?m)^\s*(?:pub\s+)?fn\s+parse_date\s*\(", content):
        content = re.sub(
            r"(?m)^\s*use\s+aidoku::imports::std::parse_date\s*;\s*\n?",
            "",
            content,
        )
        replacements: list[tuple[str, str]] = []
        for node in RustInspection.from_content(content).nodes("use_declaration"):
            original = node.text.decode("utf-8", errors="replace")
            if "std::parse_date" not in original:
                continue
            normalized = remove_grouped_use_pattern(
                original,
                r"std\s*::\s*parse_date",
            )
            normalized = remove_grouped_use_pattern(
                normalized,
                r"imports\s*::\s*\{\s*\}",
            )
            normalized = re.sub(r"\{\s*,", "{", normalized)
            normalized = re.sub(r",\s*}", "}", normalized)
            if re.fullmatch(r"use\s+aidoku::\{\s*\}\s*;", normalized.strip()):
                normalized = ""
            if re.fullmatch(r"use\s+aidoku::imports::\{\s*\}\s*;", normalized.strip()):
                normalized = ""
            if normalized != original:
                replacements.append((original, normalized))
        for original, normalized in replacements:
            content = content.replace(original, normalized, 1)
    if "use aidoku::imports::defaults::defaults_get;" in content:
        usage = re.sub(r"(?m)^\s*use\s+[^;]+;", "", content)
        if re.search(r"\bDefaultValue\b", usage) is None:
            content = re.sub(
                r"(?m)^\s*defaults::DefaultValue\s*,\s*\n?",
                "",
                content,
            )
        content = re.sub(
            r"defaults::\{(?P<body>[^{}]*)\}",
            lambda match: (
                "defaults::{"
                + ", ".join(
                    item.strip()
                    for item in match.group("body").split(",")
                    if item.strip()
                    and item.strip() != "defaults_get"
                    and not (
                        item.strip() == "DefaultValue"
                        and re.search(r"\bDefaultValue\b", usage) is None
                    )
                )
                + "}"
            ),
            content,
        )
        content = re.sub(r"\bimports::defaults::defaults_get\s*,", "", content)
        content = re.sub(r",\s*imports::defaults::defaults_get\b", "", content)
        content = re.sub(r"\{\s*imports::defaults::defaults_get\s*\}", "{}", content)
        content = content.replace("defaults::{},", "")
        replacements: list[tuple[str, str]] = []
        for node in RustInspection.from_content(content).nodes("use_declaration"):
            original = node.text.decode("utf-8", errors="replace")
            if original.strip() == "use aidoku::imports::defaults::defaults_get;":
                continue
            normalized = re.sub(r"\bdefaults::defaults_get\s*,\s*", "", original)
            normalized = re.sub(r",\s*defaults::defaults_get\b", "", normalized)
            normalized = re.sub(r"\{\s*defaults::defaults_get\s*\}", "{}", normalized)
            if normalized != original:
                replacements.append((original, normalized))
        for original, normalized in replacements:
            content = content.replace(original, normalized, 1)
    return content


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
            "DefaultValue",
            "MultiSelectFilter",
            "PageResult",
            "Result",
            "SelectFilter",
            "SelectSetting",
            "Setting",
            "SettingGroup",
            "SettingItem",
            "SortFilter",
            "SortFilterDefault",
            "TextSetting",
            "Value",
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
    return re.sub(
        r"use\s+(?P<prefix>[A-Za-z_][\w:]*)::\{\s*(?P<name>[A-Za-z_]\w*)\s*};",
        r"use \g<prefix>::\g<name>;",
        content,
    )


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
    direct_to_string = "use aidoku::alloc::string::ToString;"
    if direct_to_string in declarations and any(
        declaration != direct_to_string and _aidoku_to_string_imported(declaration)
        for declaration in declarations
    ):
        content = re.sub(
            r"(?m)^\s*use\s+aidoku::alloc::string::ToString\s*;\s*\n?",
            "",
            content,
        )
    direct_vec = "use aidoku::alloc::vec;"
    if direct_vec in declarations and any(
        declaration != direct_vec and _alloc_macro_is_imported(declaration, "vec")
        for declaration in declarations
    ):
        content = re.sub(
            r"(?m)^\s*use\s+aidoku::alloc::vec\s*;\s*\n?",
            "",
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


def normalize_aidoku_api_paths(content: str, *, trace: NormalizationTrace) -> str:
    return trace.apply("normalize_aidoku_api_paths", content, _normalize_aidoku_api_paths)


def normalize_aidoku_registration_imports(
    content: str,
    *,
    trace: NormalizationTrace,
) -> str:
    for rewrite in (_normalize_select_filter_import, _remove_macro_only_trait_imports):
        content = trace.apply(rewrite.__name__.removeprefix("_"), content, rewrite)
    return content


def finalize_aidoku_imports(content: str, *, trace: NormalizationTrace) -> str:
    rewrites: tuple[Callable[[str], str], ...] = (
        _inject_no_std_macro_imports,
        _inject_required_aidoku_imports,
        _normalize_shadowed_known_imports,
        _remove_macro_only_trait_imports,
        _remove_duplicate_imports,
        _remove_unused_known_imports,
    )
    for rewrite in rewrites:
        content = trace.apply(rewrite.__name__.removeprefix("_"), content, rewrite)
    return content
