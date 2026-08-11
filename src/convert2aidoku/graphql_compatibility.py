from __future__ import annotations

import json
import re

from .normalization_trace import NormalizationTrace
from .rust_inspection import RustInspection


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


def _split_raw_graphql_manga_query(query: str) -> tuple[str, str] | None:
    """Split line-oriented raw GraphQL detail/chapter fields at operation depth one."""
    lines = query.splitlines(keepends=True)
    detail_lines = [index for index, line in enumerate(lines) if "comicById(" in line]
    chapter_lines = [index for index, line in enumerate(lines) if "chaptersByComicId(" in line]
    if len(detail_lines) != 1 or len(chapter_lines) != 1:
        return None
    detail_index = detail_lines[0]
    chapter_start = chapter_lines[0]
    depth = 0
    chapter_end = None
    for index in range(chapter_start, len(lines)):
        line = lines[index].replace("#{body}", "")
        if index == chapter_start and "{" not in line:
            return None
        depth += line.count("{") - line.count("}")
        if depth == 0:
            chapter_end = index + 1
            break
    if chapter_end is None:
        return None
    details = "".join(lines[:chapter_start] + lines[chapter_end:])
    chapters = "".join(lines[:detail_index] + lines[detail_index + 1 :])
    return details, chapters


def _normalize_graphql_manga_update_projection(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        if function.name != "manga_query" or "(true, false)" in function.text:
            continue
        query_match = re.search(
            r'format!\(\s*(?P<literal>"(?:\\.|[^"\\])*")\s*\)',
            function.text,
        )
        raw_match = re.search(
            r'build_query\(\s*(?P<literal>r(?P<hashes>#+)"(?P<query>[\s\S]*?)"(?P=hashes))'
            r"\s*,?\s*\)",
            function.text,
        )
        if query_match is not None:
            try:
                query = json.loads(query_match.group("literal"))
            except json.JSONDecodeError:
                continue
            projections = _split_graphql_manga_query(query)
            if projections is None:
                continue
            details_query, chapters_query = projections
            combined_expression = query_match.group(0)
            details_expression = f"format!({json.dumps(details_query, ensure_ascii=False)})"
            chapters_expression = f"format!({json.dumps(chapters_query, ensure_ascii=False)})"
            default_expression = "String::new()"
            expression = query_match.group(0)
        elif raw_match is not None:
            query = raw_match.group("query")
            projections = _split_raw_graphql_manga_query(query)
            if projections is None:
                continue
            details_query, chapters_query = projections
            hashes = raw_match.group("hashes")

            def raw_literal(value: str, hashes: str = hashes) -> str:
                return f'r{hashes}"{value}"{hashes}'

            combined_expression = raw_match.group("literal")
            details_expression = raw_literal(details_query)
            chapters_expression = raw_literal(chapters_query)
            default_expression = '""'
            expression = raw_match.group("literal")
        else:
            continue
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
        projection = (
            "match (needs_details, needs_chapters) {\n"
            f"            (true, true) => {combined_expression},\n"
            f"            (true, false) => {details_expression},\n"
            f"            (false, true) => {chapters_expression},\n"
            f"            _ => {default_expression},\n"
            "        }"
        )
        normalized = function.text.replace(signature.group(0), new_signature, 1)
        normalized = normalized.replace(expression, projection, 1)
        replacements.append((function.text, normalized))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)

    replacements = []
    for function in RustInspection.from_content(content).named("get_manga_update"):
        if "needs_details" not in function.text or "needs_chapters" not in function.text:
            continue
        normalized = re.sub(
            r"(?P<prefix>self\.)?manga_query\((?P<args>[^(),\n]+)\)",
            lambda match: (
                f"{match.group('prefix') or ''}manga_query("
                f"{match.group('args').strip()}, needs_details, needs_chapters)"
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
        r"(?P<body>body|payload)\s*\)\??",
        lambda match: (
            f"Request::{match.group('method')}({match.group('url').strip()})?"
            f".body({match.group('body')}.to_string().as_bytes())"
        ),
        content,
    )


def normalize_graphql_request_compatibility(content: str, *, trace: NormalizationTrace) -> str:
    return trace.apply("normalize_graphql_request_body", content, _normalize_graphql_request_body)


def normalize_graphql_fragment_compatibility(content: str, *, trace: NormalizationTrace) -> str:
    return trace.apply("normalize_graphql_body_fragment", content, _normalize_graphql_body_fragment)


def normalize_graphql_projection_compatibility(content: str, *, trace: NormalizationTrace) -> str:
    return trace.apply(
        "normalize_graphql_manga_update_projection",
        content,
        _normalize_graphql_manga_update_projection,
    )
