from __future__ import annotations

import re

from .normalization_trace import NormalizationTrace
from .rust_inspection import RustInspection


def _normalize_parse_date_option_patterns(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for node in RustInspection.from_content(content).nodes("call_expression"):
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if function is None or arguments is None or len(arguments.named_children) != 1:
            continue
        function_text = function.text.decode("utf-8", errors="replace")
        if function_text != "aidoku::imports::std::parse_date":
            continue
        argument = arguments.named_children[0].text.decode("utf-8", errors="replace")
        replacements.append(
            (
                node.text.decode("utf-8", errors="replace"),
                f'{function_text}({argument}, "yyyy-MM-dd HH:mm:ss")',
            )
        )
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
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


def _normalize_parse_date_result(content: str) -> str:
    return re.sub(
        r"(\bparse_(?:local_)?date\s*\([^;]{0,800}?\))\s*\.ok\(\)",
        r"\1",
        content,
    )


def _normalize_descending_sort_key(content: str) -> str:
    return re.sub(
        r"\b(?P<items>[A-Za-z_]\w*)\.sort_by\(\|(?P<left>[A-Za-z_]\w*),\s*"
        r"(?P<right>[A-Za-z_]\w*)\|\s*(?P=right)\.(?P<field>[A-Za-z_]\w*)"
        r"\.cmp\(&(?P=left)\.(?P=field)\)\);",
        lambda match: (
            f"{match.group('items')}.sort_by_key(|item| "
            f"core::cmp::Reverse(item.{match.group('field')}));"
        ),
        content,
    )


def normalize_chapter_date_compatibility(content: str, *, trace: NormalizationTrace) -> str:
    for rewrite in (
        _normalize_parse_date_option_patterns,
        _normalize_optional_chapter_dates,
        _normalize_chapter_group_scope,
    ):
        content = trace.apply(rewrite.__name__.removeprefix("_"), content, rewrite)
    return content


def finalize_chapter_date_compatibility(content: str, *, trace: NormalizationTrace) -> str:
    content = trace.apply("parse_date_result", content, _normalize_parse_date_result)
    return trace.apply("descending_sort_key", content, _normalize_descending_sort_key)
