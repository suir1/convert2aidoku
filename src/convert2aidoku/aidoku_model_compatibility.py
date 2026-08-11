from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .normalization_trace import NormalizationTrace
from .rust_inspection import RustInspection
from .rust_inspection import last_rust_identifier as _last_rust_identifier


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


def _normalize_pinned_model_shapes(content: str) -> str:
    """Repair unambiguous model/request shapes for the pinned Aidoku revision."""
    content = re.sub(r"(?<![A-Za-z0-9_])Manga::new\(\)", "Manga::default()", content)
    content = re.sub(
        r"\b(?P<owner>manga|chapter)\s*\.\s*key\s*\.\s*ok_or_else\(\s*"
        r"\|\|\s*AidokuError::message\([^)]*\)\s*\)\?",
        r"\g<owner>.key",
        content,
    )
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
                    rf"\b(?:{field}|{field.removesuffix('s')}):\s*"
                    rf"(?:Some\()?{value}(?:\))?,",
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
        r"\b(?P<owner>[A-Za-z_]\w*)\.artists\s*=\s*Some\("
        r"(?P<value>[A-Za-z_]\w*\.title\.clone\(\))\s*\);",
        r"\g<owner>.artists = Some(vec![\g<value>]);",
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
    edits: list[tuple[int, int, bytes]] = []
    for statement in RustInspection.from_content(content).nodes("expression_statement"):
        if len(statement.named_children) != 1:
            continue
        expression = statement.named_children[0]
        if expression.type != "call_expression":
            continue
        callee = expression.child_by_field_name("function")
        if callee is None or callee.type != "field_expression":
            continue
        receiver = callee.child_by_field_name("value")
        field = callee.child_by_field_name("field")
        if (
            receiver is None
            or receiver.type != "identifier"
            or field is None
            or field.text != b"header"
        ):
            continue
        request = receiver.text.decode("utf-8", errors="replace")
        replacement = f"{request} = {expression.text.decode('utf-8', errors='replace')};"
        edits.append((statement.start_byte, statement.end_byte, replacement.encode()))
    encoded = content.encode("utf-8")
    for begin, end, replacement in sorted(edits, reverse=True):
        encoded = encoded[:begin] + replacement + encoded[end:]
    content = encoded.decode("utf-8")
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
    content = re.sub(
        r"Err\(_\)\s*=>\s*(?P<call>(?:self\.)?[A-Za-z_]\w*\([^;\n]*\)\?"
        r"\.send\(\))\s*,",
        r"Err(_) => Ok(\g<call>?),",
        content,
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


def _enclosing_impl(node: Any) -> Any | None:
    parent = node.parent
    while parent is not None:
        if parent.type == "impl_item":
            return parent
        parent = parent.parent
    return None


def _normalize_pinned_trait_impls(content: str) -> str:
    """Pin generated optional-trait receivers to the selected Aidoku revision."""
    edits: list[tuple[int, int, bytes]] = []
    inspection = RustInspection.from_content(content)
    for function in inspection.named("get_manga_list"):
        implementation = _enclosing_impl(function.node)
        trait = implementation.child_by_field_name("trait") if implementation is not None else None
        if _last_rust_identifier(trait) != "ListingProvider":
            continue
        parameters = function.node.child_by_field_name("parameters")
        receiver = next(
            (
                parameter
                for parameter in (parameters.named_children if parameters is not None else ())
                if parameter.type == "self_parameter"
            ),
            None,
        )
        if receiver is not None and RustInspection.compact_node(receiver) == "&mutself":
            edits.append((receiver.start_byte, receiver.end_byte, b"&self"))
    encoded = content.encode("utf-8")
    for begin, end, replacement in sorted(edits, reverse=True):
        encoded = encoded[:begin] + replacement + encoded[end:]
    return encoded.decode("utf-8")


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
    content = re.sub(
        r"\b(?P<field>author|artist)\s*:\s*"
        r"(?:Some\()?(?P<value>[^,\n]{1,800}\.collect\(\))(?:\))?,",
        lambda match: f"{match.group('field')}s: Some({match.group('value')}),",
        content,
    )
    content = re.sub(
        r"\bstatus\s*:\s*(?:aidoku::)?MangaStatus::from_i32\((?P<value>[^,\n]{1,500})\)"
        r"\.unwrap_or_default\(\)\s*,",
        lambda match: (
            f"status: match {match.group('value')} {{ "
            "1 => aidoku::MangaStatus::Ongoing, "
            "2 => aidoku::MangaStatus::Completed, "
            "3 => aidoku::MangaStatus::Cancelled, "
            "4 => aidoku::MangaStatus::Hiatus, "
            "_ => aidoku::MangaStatus::Unknown },"
        ),
        content,
    )
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
            wrapped_required = re.fullmatch(r"Some\((?P<value>[\s\S]+)\)", value)
            if (
                (type_name == "Manga" and field_name in {"key", "title"})
                or (type_name == "Chapter" and field_name == "key")
            ) and wrapped_required is not None:
                replacement_value = wrapped_required.group("value")
            elif type_name == "Manga" and field_name == "title" and value.endswith(".text()"):
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
                if re.search(r"\bas\s+f64\b", value):
                    replacement_value = re.sub(r"\bas\s+f64\b", "as f32", value)
                    original = field.text.decode("utf-8", errors="replace")
                    replacements.append((original, f"{field_name}: {replacement_value}"))
                    continue
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

    inspection = RustInspection.from_content(content)
    numeric_status_bindings: dict[int, set[str]] = {}
    for function in inspection.functions:
        numeric_status_bindings[function.node.start_byte] = set()
        for match in re.finditer(
            r"\blet\s+(?P<name>[A-Za-z_]\w*)\s*=\s*match\s+[^\{;]+\{"
            r"(?P<body>[\s\S]{1,2000}?)\}\s*;",
            function.text,
        ):
            arm_values = re.findall(r"=>\s*([^,\s]+)", match.group("body"))
            if len(arm_values) >= 2 and all(re.fullmatch(r"[0-4]", value) for value in arm_values):
                numeric_status_bindings[function.node.start_byte].add(match.group("name"))

    enum_edits: list[tuple[int, int, bytes]] = []
    encoded = content.encode("utf-8")
    for node in inspection.nodes("struct_expression"):
        name = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name is None or name.text != b"Manga" or body is None:
            continue
        owner = node
        while owner.parent is not None and owner.type != "function_item":
            owner = owner.parent
        integer_status = numeric_status_bindings.get(owner.start_byte, set())
        for field in body.named_children:
            if (
                field.type == "shorthand_field_initializer"
                and field.text == b"status"
                and "status" in integer_status
            ):
                replacement = (
                    "status: match status { "
                    "1 => aidoku::MangaStatus::Ongoing, "
                    "2 => aidoku::MangaStatus::Completed, "
                    "3 => aidoku::MangaStatus::Cancelled, "
                    "4 => aidoku::MangaStatus::Hiatus, "
                    "_ => aidoku::MangaStatus::Unknown }"
                )
                enum_edits.append((field.start_byte, field.end_byte, replacement.encode()))
                continue
            field_node = field.child_by_field_name("field")
            value_node = field.child_by_field_name("value")
            if field_node is None or value_node is None:
                continue
            field_name = field_node.text.decode("utf-8", errors="replace")
            value = value_node.text.decode("utf-8", errors="replace")
            if field_name == "id":
                begin = field.start_byte
                line_start = encoded.rfind(b"\n", 0, begin) + 1
                if not encoded[line_start:begin].strip():
                    begin = line_start
                end = field.end_byte
                while end < len(encoded) and encoded[end : end + 1] in {b" ", b"\t"}:
                    end += 1
                if encoded[end : end + 1] == b",":
                    end += 1
                if begin == line_start:
                    while end < len(encoded) and encoded[end : end + 1] in {b" ", b"\t"}:
                        end += 1
                    if encoded[end : end + 1] == b"\n":
                        end += 1
                enum_edits.append((begin, end, b""))
            elif field_name == "status" and (
                re.fullmatch(r"[0-4]", value) or value in integer_status
            ):
                replacement = (
                    f"status: match {value} {{ "
                    "1 => aidoku::MangaStatus::Ongoing, "
                    "2 => aidoku::MangaStatus::Completed, "
                    "3 => aidoku::MangaStatus::Cancelled, "
                    "4 => aidoku::MangaStatus::Hiatus, "
                    "_ => aidoku::MangaStatus::Unknown }"
                )
                enum_edits.append((field.start_byte, field.end_byte, replacement.encode()))
            elif field_name == "viewer" and re.fullmatch(r"[0-4]", value):
                variants = {
                    "0": "Unknown",
                    "1": "LeftToRight",
                    "2": "RightToLeft",
                    "3": "Vertical",
                    "4": "Webtoon",
                }
                replacement = f"viewer: aidoku::Viewer::{variants[value]}"
                enum_edits.append((field.start_byte, field.end_byte, replacement.encode()))
            elif field_name == "nsfw" and re.fullmatch(r"[01]", value):
                variant = "Unknown" if value == "0" else "NSFW"
                replacement = f"content_rating: aidoku::ContentRating::{variant}"
                enum_edits.append((field.start_byte, field.end_byte, replacement.encode()))
    for begin, end, replacement in sorted(enum_edits, reverse=True):
        encoded = encoded[:begin] + replacement + encoded[end:]
    content = encoded.decode("utf-8")

    string_helpers: set[str] = set()
    for function in RustInspection.from_content(content).functions:
        parameters = function.node.child_by_field_name("parameters")
        first = next(
            (
                parameter
                for parameter in (parameters.named_children if parameters is not None else ())
                if parameter.type == "parameter"
            ),
            None,
        )
        type_node = first.child_by_field_name("type") if first is not None else None
        if type_node is not None and re.fullmatch(rb"&\s*str", type_node.text):
            string_helpers.add(function.name)
    for helper in string_helpers:
        content = re.sub(
            rf"\b{re.escape(helper)}\(\s*&(?P<owner>manga|chapter)\.url\s*\)",
            lambda match, helper=helper: (
                f"{helper}({match.group('owner')}.url.as_deref()"
                f".unwrap_or(&{match.group('owner')}.key))"
            ),
            content,
        )
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
        field_expression = argument
        if argument.type == "call_expression":
            method = argument.child_by_field_name("function")
            method_arguments = argument.child_by_field_name("arguments")
            method_name = method.child_by_field_name("field") if method is not None else None
            if (
                method is None
                or method.type != "field_expression"
                or method_name is None
                or method_name.text != b"clone"
                or method_arguments is None
                or method_arguments.named_children
            ):
                continue
            field_expression = method.child_by_field_name("value")
        if field_expression is None or field_expression.type != "field_expression":
            continue
        source = field_expression.child_by_field_name("value")
        field = field_expression.child_by_field_name("field")
        if source is None or field is None or source.type not in {"identifier", "self"}:
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
        if source.type == "self":
            implementation = _enclosing_impl(function_node)
            type_node = (
                implementation.child_by_field_name("type") if implementation is not None else None
            )
            source_type = _last_rust_identifier(type_node)
        else:
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


def _normalize_aidoku_model_string_into(content: str) -> str:
    return re.sub(
        r"(?P<prefix>\b(?:id|title):\s*(?:Some\()?)"
        r'(?P<literal>"(?:\\.|[^"\\])*")\.to_string\(\)',
        r"\g<prefix>\g<literal>.into()",
        content,
    )


def normalize_aidoku_model_construction(content: str, *, trace: NormalizationTrace) -> str:
    """Normalize mutable and default-built Aidoku model construction."""
    for rewrite in (
        _normalize_mutated_aidoku_models,
        _normalize_default_model_assignments,
        _normalize_page_index_fields,
    ):
        content = trace.apply(rewrite.__name__.removeprefix("_"), content, rewrite)
    return content


def normalize_aidoku_struct_defaults(content: str, *, trace: NormalizationTrace) -> str:
    """Fill omitted fields in Aidoku models and generated filter structs."""
    return trace.apply(
        "normalize_struct_expression_defaults",
        content,
        _normalize_struct_expression_defaults,
    )


def normalize_aidoku_model_string_literals(content: str, *, trace: NormalizationTrace) -> str:
    """Use the pinned model field conversion for generated string literals."""
    return trace.apply(
        "aidoku_model_string_into",
        content,
        _normalize_aidoku_model_string_into,
    )


def normalize_aidoku_models(content: str, *, trace: NormalizationTrace) -> str:
    """Apply pinned Aidoku model compatibility with stable trace rule IDs."""

    def apply(rewrite: Callable[[str], str]) -> None:
        nonlocal content
        content = trace.apply(
            rewrite.__name__.removeprefix("_"),
            content,
            rewrite,
        )

    apply(_normalize_pinned_trait_impls)
    apply(_normalize_pinned_model_shapes)
    apply(_normalize_optional_model_shorthand)
    apply(_normalize_pinned_model_fields)
    apply(_normalize_nested_optional_model_fields)
    return content
