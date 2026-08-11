from __future__ import annotations

import re
from collections.abc import Callable

from .normalization_trace import NormalizationTrace
from .rust_inspection import RustInspection


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
    borrowed_map_replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).named("get_manga_update"):
        borrowed = set(
            re.findall(
                r"\blet\s+Some\(\s*([A-Za-z_]\w*)\s*\)\s*=\s*&",
                function.text,
            )
        )
        borrowed.update(
            re.findall(
                r"\blet\s+([A-Za-z_]\w*)(?:\s*:\s*[^=;]+)?\s*=\s*&",
                function.text,
            )
        )
        normalized = function.text
        for owner in borrowed:
            normalized = re.sub(
                rf"\b{re.escape(owner)}\.(?P<field>[A-Za-z_]\w*)"
                rf"\.map\(\|(?P<item>[A-Za-z_]\w*)\|\s*"
                rf"(?P=item)\.(?P<member>[A-Za-z_]\w*)",
                rf"{owner}.\g<field>.as_ref().map(|\g<item>| \g<item>.\g<member>",
                normalized,
            )
        if normalized != function.text:
            borrowed_map_replacements.append((function.text, normalized))
    for original, normalized in borrowed_map_replacements:
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


def _normalize_moved_field_collection_usage(content: str) -> str:
    pattern = re.compile(
        r"(?P<indent>^[ \t]*)for\s+(?P<item>[^\n]+)\s+in\s+"
        r"(?P<owner>[A-Za-z_]\w*)\.(?P<field>[A-Za-z_]\w*)\s*\{",
        re.MULTILINE,
    )
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        normalized = function.text
        for match in reversed(list(pattern.finditer(normalized))):
            expression = f"{match.group('owner')}.{match.group('field')}"
            if f"{expression}.is_empty()" not in normalized[match.end() :]:
                continue
            variable = f"{match.group('owner')}_{match.group('field')}_is_empty"
            insertion = f"{match.group('indent')}let {variable} = {expression}.is_empty();\n"
            normalized = normalized[: match.start()] + insertion + normalized[match.start() :]
            tail_start = match.end() + len(insertion)
            normalized = normalized[:tail_start] + normalized[tail_start:].replace(
                f"{expression}.is_empty()", variable
            )
        if normalized != function.text:
            replacements.append((function.text, normalized))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    return content


def _normalize_overwritten_loop_initializers(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    declaration_pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]*)let\s+mut\s+(?P<name>[A-Za-z_]\w*)"
        r"(?:\s*:\s*[^=;]+)?\s*=\s*[^;]+;\s*\n"
    )
    for function in RustInspection.from_content(content).functions:
        normalized = function.text
        edits: list[tuple[int, int, str]] = []
        for declaration in declaration_pattern.finditer(normalized):
            name = declaration.group("name")
            loop = re.search(r"\bloop\s*\{", normalized[declaration.end() :])
            if loop is None:
                continue
            loop_start = declaration.end() + loop.end()
            first_use = re.search(rf"\b{re.escape(name)}\b", normalized[loop_start:])
            if first_use is None:
                continue
            use_start = loop_start + first_use.start()
            line_start = normalized.rfind("\n", loop_start, use_start) + 1
            if normalized[line_start:use_start].strip():
                continue
            assignment = re.match(
                rf"{re.escape(name)}\s*=\s*[^;]+;",
                normalized[use_start:],
            )
            if assignment is None:
                continue
            edits.append((declaration.start(), declaration.end(), ""))
            edits.append(
                (
                    use_start,
                    use_start + len(name),
                    f"let {name}",
                )
            )
        for start, end, replacement in sorted(edits, reverse=True):
            normalized = normalized[:start] + replacement + normalized[end:]
        if normalized != function.text:
            replacements.append((function.text, normalized))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
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


def normalize_detail_ownership(content: str, *, trace: NormalizationTrace) -> str:
    for rewrite in (_normalize_detail_partial_move, _normalize_manga_replacement_chapters):
        content = trace.apply(rewrite.__name__.removeprefix("_"), content, rewrite)
    return content


def normalize_pagination_ownership(content: str, *, trace: NormalizationTrace) -> str:
    rewrites: tuple[Callable[[str], str], ...] = (
        _normalize_partial_move_pagination,
        _normalize_partial_move_loop_pagination,
        _normalize_collection_len_after_move,
        _normalize_moved_field_collection_usage,
        _normalize_overwritten_loop_initializers,
        _normalize_moved_key_then_borrowed_url,
    )
    for rewrite in rewrites:
        content = trace.apply(rewrite.__name__.removeprefix("_"), content, rewrite)
    return content
