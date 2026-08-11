from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .normalization_trace import NormalizationTrace
from .rust_inspection import RustInspection


def _normalize_custom_page_context_types(content: str) -> str:
    inspection = RustInspection.from_content(content)
    custom_types: dict[str, tuple[Any, Any, tuple[str, ...]]] = {}
    for implementation in inspection.nodes("impl_item"):
        match = re.match(
            r"impl(?:aidoku::)?PageContextfor(?P<name>[A-Za-z_]\w*)\{",
            RustInspection.compact_node(implementation),
        )
        if match is None:
            continue
        struct = inspection.struct_named(match.group("name"))
        if (
            struct is None
            or len(struct.fields) != 1
            or struct.fields[0].name != "referer"
            or struct.fields[0].type_text.rsplit("::", 1)[-1] != "String"
        ):
            continue
        attributes: list[str] = []
        sibling = struct.node.prev_named_sibling
        while sibling is not None and sibling.type == "attribute_item":
            attributes.append(sibling.text.decode("utf-8", errors="replace"))
            sibling = sibling.prev_named_sibling
        custom_types[struct.name] = (struct.node, implementation, tuple(attributes))

    for name, (struct_node, implementation, attributes) in custom_types.items():
        replacements: list[tuple[str, str]] = []
        for expression in RustInspection.from_content(content).nodes("struct_expression"):
            type_node = expression.child_by_field_name("name")
            body = expression.child_by_field_name("body")
            if (
                type_node is None
                or body is None
                or type_node.text.decode("utf-8", errors="replace").rsplit("::", 1)[-1] != name
            ):
                continue
            referer = next(
                (
                    value.text.decode("utf-8", errors="replace")
                    for field in body.named_children
                    if (field_name := field.child_by_field_name("field")) is not None
                    and field_name.text == b"referer"
                    and (value := field.child_by_field_name("value")) is not None
                ),
                None,
            )
            if referer is not None:
                replacements.append(
                    (
                        expression.text.decode("utf-8", errors="replace"),
                        f'PageContext::from([("referer".into(), {referer})])',
                    )
                )
        for original, replacement in replacements:
            content = content.replace(original, replacement, 1)

        downcast = re.compile(
            rf"if\s+let\s+Ok\((?P<binding>[A-Za-z_]\w*)\)\s*=\s*"
            rf"(?P<context>[A-Za-z_]\w*)\.downcast_ref::<{re.escape(name)}>\(\)\s*"
            r"\{(?P<body>[^{}]*)\}"
        )

        def replace_downcast(match: re.Match[str]) -> str:
            binding = match.group("binding")
            body = match.group("body").replace(
                f"&{binding}.referer",
                f"{binding}.as_str()",
            )
            return f'if let Some({binding}) = {match.group("context")}.get("referer") {{{body}}}'

        content = downcast.sub(replace_downcast, content)
        for attribute in attributes:
            content = content.replace(attribute, "", 1)
        content = content.replace(struct_node.text.decode("utf-8", errors="replace"), "", 1)
        content = content.replace(
            implementation.text.decode("utf-8", errors="replace"),
            "",
            1,
        )
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
    context_replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        if function.name != "get_image_request":
            continue
        normalized = re.sub(
            r"&(?P<context>[A-Za-z_][A-Za-z0-9_]*)\.url\b",
            r'\g<context>.get("referer").map(String::as_str).unwrap_or("")',
            function.text,
        )
        normalized = re.sub(
            r'\.header\("Referer",\s*(?P<function>[A-Za-z_][A-Za-z0-9_]*\(\))\)',
            r'.header("Referer", \g<function>.as_str())',
            normalized,
        )
        if normalized != function.text:
            context_replacements.append((function.text, normalized))
    for original, replacement in context_replacements:
        content = content.replace(original, replacement, 1)
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


def _normalize_page_context_maps(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        normalized = function.text
        names = set(
            re.findall(
                r"\blet\s+mut\s+([A-Za-z_]\w*)\s*=\s*PageContext::(?:new|default)\(\)",
                normalized,
            )
        )
        for name in names:
            normalized = re.sub(
                rf"\b{re.escape(name)}\.set\(\s*(?P<key>\"(?:\\.|[^\"\\])*\")\s*,",
                rf"{name}.insert(\g<key>.into(),",
                normalized,
            )
        normalized = re.sub(
            r"(?P<lookup>\b[A-Za-z_]\w*\.get\(\s*\"(?:\\.|[^\"\\])*\"\s*\))"
            r"\.unwrap_or_default\(\)",
            r"\g<lookup>.cloned().unwrap_or_default()",
            normalized,
        )
        if normalized != function.text:
            replacements.append((function.text, normalized))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    return content


def _apply_rewrites(
    content: str,
    *,
    trace: NormalizationTrace,
    rewrites: tuple[Callable[[str], str], ...],
) -> str:
    for rewrite in rewrites:
        content = trace.apply(rewrite.__name__.removeprefix("_"), content, rewrite)
    return content


def normalize_image_request_compatibility(content: str, *, trace: NormalizationTrace) -> str:
    return _apply_rewrites(
        content,
        trace=trace,
        rewrites=(_normalize_image_request_result,),
    )


def normalize_page_context_compatibility(content: str, *, trace: NormalizationTrace) -> str:
    return _apply_rewrites(
        content,
        trace=trace,
        rewrites=(
            _normalize_custom_page_context_types,
            _normalize_legacy_page_context,
            _normalize_page_url_context,
            _normalize_page_context_maps,
        ),
    )
