from __future__ import annotations

import re

from .aidoku_import_compatibility import remove_grouped_use_pattern
from .normalization_trace import NormalizationTrace
from .rust_inspection import RustInspection


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
    content = re.sub(
        r"(?<![A-Za-z0-9_:])Filter::(?:Check|Checkbox|MultiSelect|Select|Sort)\("
        r"(?P<value>[A-Za-z_][A-Za-z0-9_]*)\)",
        r"Filter::from(\g<value>)",
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
    edits: list[tuple[int, int, bytes]] = []
    for node in RustInspection.from_content(content).nodes("struct_expression"):
        name = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if (
            name is None
            or body is None
            or name.text.decode("utf-8", errors="replace").rsplit("::", 1)[-1] != "SelectSetting"
        ):
            continue
        for child in body.named_children:
            field = child.child_by_field_name("field")
            value = child.child_by_field_name("value")
            if field is None or field.text != b"titles" or value is None:
                continue
            value_text = value.text.decode("utf-8", errors="replace")
            if value_text.startswith(("Some(", "None")):
                continue
            edits.append((value.start_byte, value.end_byte, f"Some({value_text})".encode()))
    if edits:
        encoded = content.encode("utf-8")
        for begin, end, replacement in sorted(edits, reverse=True):
            encoded = encoded[:begin] + replacement + encoded[end:]
        content = encoded.decode("utf-8")
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
        if re.search(r"(?:^|::)DeepLinkResult::", full_type_name):
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


def _normalize_legacy_group_filters(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    item_builders: set[str] = set()
    for node in RustInspection.from_content(content).nodes("call_expression"):
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if (
            function is None
            or arguments is None
            or function.text.decode("utf-8", errors="replace")
            not in {"Filter::Group", "aidoku::Filter::Group"}
            or len(arguments.named_children) != 1
        ):
            continue
        group = arguments.named_children[0]
        group_name = group.child_by_field_name("name")
        body = group.child_by_field_name("body")
        if (
            group.type != "struct_expression"
            or group_name is None
            or body is None
            or group_name.text.decode("utf-8", errors="replace").rsplit("::", 1)[-1]
            != "FilterGroup"
        ):
            continue
        owner = node
        while owner.parent is not None and owner.type != "function_item":
            owner = owner.parent
        if owner.type != "function_item":
            continue
        owner_text = owner.text.decode("utf-8", errors="replace")
        if not all(
            re.search(rf"\blet\s+(?:mut\s+)?{name}\b", owner_text) for name in ("options", "ids")
        ):
            continue
        fields: dict[str, str] = {}
        for child in body.named_children:
            field = child.child_by_field_name("field")
            value = child.child_by_field_name("value")
            if field is not None and value is not None:
                fields[field.text.decode("utf-8", errors="replace")] = value.text.decode(
                    "utf-8", errors="replace"
                )
            elif child.type == "shorthand_field_initializer":
                name = child.text.decode("utf-8", errors="replace")
                fields[name] = name
        if not {"id", "title", "items"} <= fields.keys():
            continue
        replacement = (
            "aidoku::MultiSelectFilter { "
            f"id: {fields['id']}, title: {fields['title']}, "
            "options, ids: Some(ids), ..Default::default() }.into()"
        )
        replacements.append((node.text.decode("utf-8", errors="replace"), replacement))
        if re.fullmatch(r"[A-Za-z_]\w*", fields["items"]):
            item_builders.add(fields["items"])
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)

    builder_edits: list[tuple[int, int, bytes]] = []
    for function in RustInspection.from_content(content).functions:
        local = RustInspection.from_content(function.text)
        for builder in item_builders:
            declarations = [
                node
                for node in local.nodes("let_declaration")
                if re.match(
                    rf"let(?:mut)?{re.escape(builder)}=",
                    RustInspection.compact_node(node),
                )
            ]
            loops = [
                node
                for node in local.nodes("for_expression")
                if re.search(
                    rf"\b{re.escape(builder)}\s*\.\s*push\s*\(",
                    node.text.decode("utf-8", errors="replace"),
                )
            ]
            if len(declarations) != 1 or len(loops) != 1:
                continue
            masked = bytearray(function.text.encode("utf-8"))
            for node in (declarations[0], loops[0]):
                masked[node.start_byte : node.end_byte] = b" " * (node.end_byte - node.start_byte)
            if re.search(rf"\b{re.escape(builder)}\b", masked.decode("utf-8")):
                continue
            builder_edits.extend(
                (
                    function.node.start_byte + node.start_byte,
                    function.node.start_byte + node.end_byte,
                    b"",
                )
                for node in (declarations[0], loops[0])
            )
    encoded = content.encode("utf-8")
    for begin, end, replacement in sorted(builder_edits, reverse=True):
        encoded = encoded[:begin] + replacement + encoded[end:]
    content = encoded.decode("utf-8")

    value_edits: list[tuple[int, int, bytes]] = []
    for arm in RustInspection.from_content(content).nodes("match_arm"):
        pattern = arm.child_by_field_name("pattern")
        value = arm.child_by_field_name("value")
        if pattern is None or value is None:
            continue
        pattern_text = pattern.text.decode("utf-8", errors="replace")
        if (
            re.search(
                r"(?:aidoku::)?FilterValue::Group\s*\{\s*id\s*,\s*items\s*\}",
                pattern_text,
            )
            is None
        ):
            continue
        targets = set(
            re.findall(
                r"\b([A-Za-z_]\w*)\s*\.\s*push\s*\(",
                value.text.decode("utf-8", errors="replace"),
            )
        )
        if len(targets) != 1:
            continue
        projected_pattern = re.sub(
            r"(?P<prefix>(?:aidoku::)?FilterValue::)Group\s*\{\s*id\s*,\s*items\s*\}",
            r"\g<prefix>MultiSelect { id, included, .. }",
            pattern_text,
            count=1,
        )
        target = next(iter(targets))
        replacement = f"{projected_pattern} => {{ {target}.extend(included.iter().cloned()); }}"
        value_edits.append((arm.start_byte, arm.end_byte, replacement.encode()))
    encoded = content.encode("utf-8")
    for begin, end, replacement in sorted(value_edits, reverse=True):
        encoded = encoded[:begin] + replacement + encoded[end:]
    content = encoded.decode("utf-8")

    import_replacements: list[tuple[str, str]] = []
    for node in RustInspection.from_content(content).nodes("use_declaration"):
        original = node.text.decode("utf-8", errors="replace")
        normalized = original
        for name in ("FilterGroup", "GroupFilter"):
            normalized = remove_grouped_use_pattern(normalized, re.escape(name))
        normalized = re.sub(r"\{\s*,", "{", normalized)
        normalized = re.sub(r",\s*}", "}", normalized)
        normalized = re.sub(
            r"use\s+aidoku::\{\s*(?P<item>[A-Za-z_]\w*)\s*};",
            r"use aidoku::\g<item>;",
            normalized,
        )
        if re.fullmatch(r"use\s+aidoku::\{\s*\}\s*;", normalized.strip()):
            normalized = ""
        if normalized != original:
            import_replacements.append((original, normalized))
    for original, normalized in import_replacements:
        content = content.replace(original, normalized, 1)
    return content


def _normalize_select_filter_structs(content: str) -> str:
    return re.sub(
        r"(?:aidoku::)?Filter::Select\s*\{"
        r"(?P<body>[\s\S]{0,3000}?\.\.Default::default\(\)\s*)\}",
        r"aidoku::SelectFilter {\g<body>}.into()",
        content,
    )


def _normalize_filter_match_predicate(content: str) -> str:
    content = re.sub(
        r"find\(\|(?P<item>[A-Za-z_][A-Za-z0-9_]*)\|\s*match\s+(?P=item)\s*\{\s*"
        r"FilterValue::Select\s*\{\s*id:\s*(?P<found>[A-Za-z_][A-Za-z0-9_]*),\s*"
        r"value\s*\}\s*if\s*(?P=found)\s*==\s*(?P<wanted>[A-Za-z_][A-Za-z0-9_]*)"
        r"\s*=>\s*true,\s*_\s*=>\s*false,?\s*\}\)",
        r"find(|\g<item>| matches!(\g<item>, FilterValue::Select { id: \g<found>, .. } "
        r"if \g<found> == \g<wanted>))",
        content,
    )
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).named("selected_value"):
        return_type = function.node.child_by_field_name("return_type")
        if return_type is None or "Option<" not in return_type.text.decode(
            "utf-8", errors="replace"
        ):
            continue
        edits: list[tuple[int, int, str]] = []
        for arm in RustInspection.from_content(function.text).nodes("match_arm"):
            ancestor = arm.parent
            inside_loop = False
            while ancestor is not None:
                if ancestor.type == "for_expression":
                    inside_loop = True
                    break
                ancestor = ancestor.parent
            value = arm.child_by_field_name("value")
            if not inside_loop or value is None:
                continue
            if value.type == "call_expression":
                value_text = value.text.decode("utf-8", errors="replace")
                if value_text.startswith("Some("):
                    edits.append((value.start_byte, value.end_byte, f"return {value_text}"))
                continue
            if value.type == "block" and value.named_children:
                tail = value.named_children[-1]
                if tail.type == "expression_statement" and tail.named_child_count == 1:
                    tail = tail.named_child(0)
                tail_text = tail.text.decode("utf-8", errors="replace")
                if (
                    tail.type == "call_expression"
                    and tail_text.startswith("Some(")
                    or tail.type == "if_expression"
                    and "Some(" in tail_text
                ):
                    edits.append((tail.start_byte, tail.end_byte, f"return {tail_text};"))
        if not edits:
            continue
        encoded = function.text.encode("utf-8")
        for begin, end, replacement in sorted(edits, reverse=True):
            encoded = encoded[:begin] + replacement.encode("utf-8") + encoded[end:]
        replacements.append((function.text, encoded.decode("utf-8")))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    return content


def _normalize_select_filter_paths(content: str) -> str:
    content = content.replace("aidoku::std::filters::SelectFilter", "aidoku::SelectFilter")
    content = content.replace("aidoku::filters::SelectFilter", "aidoku::SelectFilter")
    return content.replace("aidoku::filter::SelectFilter", "aidoku::SelectFilter")


def normalize_legacy_dynamic_filters(content: str, *, trace: NormalizationTrace) -> str:
    for rewrite in (
        _normalize_legacy_filter_fields,
        _normalize_legacy_group_filters,
        _normalize_select_filter_constructors,
    ):
        content = trace.apply(rewrite.__name__.removeprefix("_"), content, rewrite)
    return content


def normalize_select_filter_path_compatibility(content: str, *, trace: NormalizationTrace) -> str:
    return trace.apply("select_filter_paths", content, _normalize_select_filter_paths)


def normalize_select_filter_struct_compatibility(content: str, *, trace: NormalizationTrace) -> str:
    return trace.apply("normalize_select_filter_structs", content, _normalize_select_filter_structs)


def normalize_filter_predicate_compatibility(content: str, *, trace: NormalizationTrace) -> str:
    return trace.apply(
        "normalize_filter_match_predicate", content, _normalize_filter_match_predicate
    )
