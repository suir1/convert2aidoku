from __future__ import annotations

import re
from collections.abc import Callable

from .normalization_trace import NormalizationTrace
from .rust_inspection import RustInspection

_BOOLEAN_LET_SOME_ALTERNATIVE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)if\s+\(let\s+Some\(\(_,[ \t]*"
    r"(?P<binding>[A-Za-z_][A-Za-z0-9_]*)\)\)\s*=\s*(?P<first>[^\r\n]+?)\)\s*"
    r"\|\|\s*\(let\s+Some\(\(_,[ \t]*(?P=binding)\)\)\s*=\s*"
    r"(?P<second>[^\r\n]+?)\)\s*\{"
)


def _normalize_boolean_let_some_alternatives(content: str) -> str:
    """Rewrite an invalid AI-style boolean let condition without changing its intent."""

    def replace(match: re.Match[str]) -> str:
        return (
            f"{match.group('indent')}if let Some((_, {match.group('binding')})) = "
            f"{match.group('first')}.or_else(|| {match.group('second')}) {{"
        )

    return _BOOLEAN_LET_SOME_ALTERNATIVE.sub(replace, content)


def _normalize_if_expression_arithmetic(content: str) -> str:
    edits: list[tuple[int, bytes]] = []
    inspection = RustInspection.from_content(content)
    for node in inspection.nodes("if_expression"):
        if node.parent is None or node.parent.type != "binary_expression":
            continue
        left = node.parent.child_by_field_name("left")
        if left != node:
            continue
        edits.append((node.start_byte, b"("))
        edits.append((node.end_byte, b")"))
    encoded = content.encode("utf-8")
    for position, insertion in sorted(edits, reverse=True):
        encoded = encoded[:position] + insertion + encoded[position:]
    return encoded.decode("utf-8")


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


def _normalize_index_length_guards(content: str) -> str:
    """Require one element beyond an index that is read inside the guarded branch."""
    edits: list[tuple[int, int, bytes]] = []
    for branch in RustInspection.from_content(content).nodes("if_expression"):
        condition = branch.child_by_field_name("condition")
        consequence = branch.child_by_field_name("consequence")
        if condition is None or consequence is None:
            continue
        match = re.fullmatch(
            r"(?P<value>[A-Za-z_]\w*)\.len\(\)>=(?P<index>[0-9]+)",
            RustInspection.compact_node(condition),
        )
        if match is None:
            continue
        indexed = f"{match.group('value')}.as_bytes()[{match.group('index')}]"
        if indexed not in RustInspection.compact_node(consequence):
            continue
        replacement = condition.text.decode("utf-8", errors="replace").replace(">=", ">", 1)
        edits.append((condition.start_byte, condition.end_byte, replacement.encode()))
    encoded = content.encode("utf-8")
    for begin, end, replacement in sorted(edits, reverse=True):
        encoded = encoded[:begin] + replacement + encoded[end:]
    return encoded.decode("utf-8")


def _normalize_discarded_enumerate_index(content: str) -> str:
    counter_replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        edits: list[tuple[int, int, str]] = []
        for node in RustInspection.from_content(function.text).nodes("for_expression"):
            statement = node.parent
            if statement is None or statement.type != "expression_statement":
                continue
            block = statement.parent
            if block is None or block.type != "block":
                continue
            siblings = list(block.named_children)
            position = next(
                (
                    index
                    for index, sibling in enumerate(siblings)
                    if sibling.start_byte == statement.start_byte
                    and sibling.end_byte == statement.end_byte
                ),
                None,
            )
            if position is None or position == 0:
                continue
            declaration = siblings[position - 1]
            counter = re.fullmatch(
                r"let\s+mut\s+(?P<name>[A-Za-z_]\w*)\s*=\s*0\s*;",
                declaration.text.decode("utf-8", errors="replace").strip(),
            )
            pattern = node.child_by_field_name("pattern")
            value = node.child_by_field_name("value")
            body = node.child_by_field_name("body")
            if (
                counter is None
                or pattern is None
                or pattern.type != "identifier"
                or value is None
                or value.type != "identifier"
                or body is None
                or body.type != "block"
                or not body.named_children
            ):
                continue
            increment = body.named_children[-1]
            name = counter.group("name")
            if (
                re.fullmatch(
                    rf"{re.escape(name)}\s*\+=\s*1\s*;",
                    increment.text.decode("utf-8", errors="replace").strip(),
                )
                is None
            ):
                continue
            body_bytes = body.text
            begin = increment.start_byte - body.start_byte
            end = increment.end_byte - body.start_byte
            normalized_body = (body_bytes[:begin] + body_bytes[end:]).decode("utf-8")
            item = pattern.text.decode("utf-8", errors="replace")
            items = value.text.decode("utf-8", errors="replace")
            replacement = (
                f"for ({name}, {item}) in {items}.into_iter().enumerate() {normalized_body}"
            )
            edits.append((declaration.start_byte, node.end_byte, replacement))
        if not edits:
            continue
        encoded = function.text.encode("utf-8")
        for begin, end, replacement in sorted(edits, reverse=True):
            encoded = encoded[:begin] + replacement.encode("utf-8") + encoded[end:]
        counter_replacements.append((function.text, encoded.decode("utf-8")))
    for original, normalized in counter_replacements:
        content = content.replace(original, normalized, 1)

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
    while True:
        replacement: tuple[str, str] | None = None
        for node in RustInspection.from_content(content).nodes("if_expression"):
            condition = node.child_by_field_name("condition")
            consequence = node.child_by_field_name("consequence")
            alternative = node.child_by_field_name("alternative")
            if condition is None or consequence is None or alternative is None:
                continue
            if consequence.type != "block":
                continue
            original = node.text.decode("utf-8", errors="replace")
            condition_text = condition.text.decode("utf-8", errors="replace")
            nested_ifs = [
                child for child in alternative.named_children if child.type == "if_expression"
            ]
            if len(nested_ifs) == 1:
                nested = nested_ifs[0]
                nested_condition = nested.child_by_field_name("condition")
                nested_consequence = nested.child_by_field_name("consequence")
                nested_alternative = nested.child_by_field_name("alternative")
                if (
                    nested_condition is not None
                    and nested_consequence is not None
                    and nested_consequence.type == "block"
                    and RustInspection.compact_node(consequence)
                    == RustInspection.compact_node(nested_consequence)
                ):
                    nested_condition_text = nested_condition.text.decode("utf-8", errors="replace")
                    if re.search(r"\blet\b", condition_text) or re.search(
                        r"\blet\b", nested_condition_text
                    ):
                        continue
                    combined = (
                        f"if ({condition_text}) || "
                        f"({nested_condition_text}) "
                        f"{consequence.text.decode('utf-8', errors='replace')}"
                    )
                    if nested_alternative is not None:
                        combined += " " + nested_alternative.text.decode("utf-8", errors="replace")
                    replacement = (original, combined)
                    break
            alternative_blocks = [
                child for child in alternative.named_children if child.type == "block"
            ]
            if len(alternative_blocks) != 1:
                continue
            alternative_block = alternative_blocks[0]
            if RustInspection.compact_node(consequence) != RustInspection.compact_node(
                alternative_block
            ):
                continue
            branch = consequence.text.decode("utf-8", errors="replace")[1:-1].strip()
            replacement = (original, f"{{ let _ = {condition_text}; {branch} }}")
            break
        if replacement is None:
            return content
        content = content.replace(*replacement, 1)


def _apply_rewrites(
    content: str,
    *,
    trace: NormalizationTrace,
    rewrites: tuple[Callable[[str], str], ...],
) -> str:
    for rewrite in rewrites:
        content = trace.apply(rewrite.__name__.removeprefix("_"), content, rewrite)
    return content


def normalize_early_control_flow(content: str, *, trace: NormalizationTrace) -> str:
    return _apply_rewrites(
        content,
        trace=trace,
        rewrites=(
            _normalize_boolean_let_some_alternatives,
            _normalize_if_expression_arithmetic,
        ),
    )


def normalize_indexing_control_flow(content: str, *, trace: NormalizationTrace) -> str:
    return _apply_rewrites(
        content,
        trace=trace,
        rewrites=(_normalize_utf8_slice_loops, _normalize_index_length_guards),
    )


def normalize_iteration_control_flow(content: str, *, trace: NormalizationTrace) -> str:
    return _apply_rewrites(
        content,
        trace=trace,
        rewrites=(_normalize_discarded_enumerate_index,),
    )


def normalize_late_control_flow(content: str, *, trace: NormalizationTrace) -> str:
    return _apply_rewrites(
        content,
        trace=trace,
        rewrites=(_normalize_identical_if_branches,),
    )
