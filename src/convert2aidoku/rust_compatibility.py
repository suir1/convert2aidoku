from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from .aidoku_import_compatibility import (
    finalize_aidoku_imports,
    normalize_aidoku_api_paths,
    normalize_aidoku_registration_imports,
    remove_grouped_use_pattern,
)
from .aidoku_model_compatibility import normalize_aidoku_models
from .generated_rust_safety import _remove_reserved_smoke_marker as _remove_reserved_smoke_marker
from .generated_rust_safety import validate_generated_content as validate_generated_content
from .generation_setting_compatibility import normalize_generation_settings
from .normalization_trace import NormalizationTrace
from .rust_inspection import RustInspection


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

    graphql_helpers = {
        function.name
        for function in RustInspection.from_content(content).functions
        if re.search(r"->\s*(?:aidoku::)?Result\s*<\s*Request\s*>", function.text)
        and "Request::post" in function.text
        and ".body(" in function.text
    }
    if graphql_helpers:
        helper_pattern = "|".join(re.escape(name) for name in sorted(graphql_helpers))
        request_pair = re.compile(
            rf"(?m)^(?P<indent>[ \t]*)let\s+(?P<request>[A-Za-z_]\w*)\s*=\s*"
            rf"(?P<call>(?:{helper_pattern})\([^;\n]+\)\?)\s*;\s*\n"
            rf"(?P=indent)let\s+(?P<response>[A-Za-z_]\w*)\s*=\s*"
            rf"(?P=request)\.send\(\)\?\s*;"
        )
        replacements = []
        for function in RustInspection.from_content(content).functions:
            if re.search(r"\b[A-Za-z_]\w*query\s*\(", function.text) is None:
                continue

            def retry(match: re.Match[str]) -> str:
                indent = match.group("indent")
                return (
                    f"{indent}let {match.group('response')} = match "
                    f"{match.group('call')}.send() {{\n"
                    f"{indent}    Ok(response) => response,\n"
                    f"{indent}    Err(_) => {match.group('call')}.send()?,\n"
                    f"{indent}}};"
                )

            normalized = request_pair.sub(retry, function.text)
            if normalized != function.text:
                replacements.append((function.text, normalized))
        for original, replacement in replacements:
            content = content.replace(original, replacement, 1)
    return content


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
    return re.sub(
        r"(?P<root>\b[A-Za-z_]\w*)\s*\.\s*(?P<field>[A-Za-z_]\w*)"
        r"\s*\.strip_prefix\(\s*(?P<first>\"(?:\\.|[^\"\\])*\")\s*\)"
        r"\s*\.unwrap_or\(\s*&(?P=root)\s*\.\s*(?P=field)\s*\)"
        r"\s*\.strip_prefix\(\s*(?P<second>\"(?:\\.|[^\"\\])*\")\s*\)"
        r"\s*\.unwrap_or\(\s*&(?P=root)\s*\.\s*(?P=field)\s*\)",
        lambda match: (
            f"{match.group('root')}.{match.group('field')}"
            f".strip_prefix({match.group('first')})"
            f".or_else(|| {match.group('root')}.{match.group('field')}"
            f".strip_prefix({match.group('second')}))"
            f".unwrap_or(&{match.group('root')}.{match.group('field')})"
        ),
        content,
    )


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
    content = re.sub(
        r"\b(?P<request>(?:request|req|retry|[A-Za-z_]\w*_(?:request|req|retry)))"
        r"\.call\(\)",
        r"\g<request>.send()",
        content,
    )
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
    content = encoded.decode("utf-8")

    closure_edits: list[tuple[int, int, bytes]] = []
    for function in RustInspection.from_content(content).functions:
        return_type = function.node.child_by_field_name("return_type")
        if (
            return_type is None
            or re.fullmatch(rb"(?:aidoku::)?Result\s*<[^>]+>", return_type.text) is None
        ):
            continue
        for call in RustInspection.from_content(function.text).nodes("call_expression"):
            callee = call.child_by_field_name("function")
            arguments = call.child_by_field_name("arguments")
            if (
                callee is None
                or callee.type != "field_expression"
                or arguments is None
                or len(arguments.named_children) != 1
            ):
                continue
            field = callee.child_by_field_name("field")
            closure = arguments.named_children[0]
            body = closure.child_by_field_name("body")
            if (
                field is None
                or field.text != b"ok_or_else"
                or closure.type != "closure_expression"
                or body is None
            ):
                continue
            body_text = body.text.decode("utf-8", errors="replace")
            if "AidokuError::message" in body_text or not any(
                marker in body_text
                for marker in ('"', "format!", ".join(", ".to_string()", "String::")
            ):
                continue
            replacement = f"aidoku::AidokuError::message({body_text})".encode()
            closure_edits.append(
                (
                    function.node.start_byte + body.start_byte,
                    function.node.start_byte + body.end_byte,
                    replacement,
                )
            )
    encoded = content.encode("utf-8")
    for begin, end, replacement in sorted(closure_edits, reverse=True):
        encoded = encoded[:begin] + replacement + encoded[end:]
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
    content = re.sub(
        r"fn\s+(?P<name>[A-Za-z_]\w*)\s*<\s*'(?P<lifetime>[A-Za-z_]\w*)\s*,\s*"
        r"(?P<type>[A-Za-z_]\w*)\s*:\s*Deserialize\s*<\s*'(?P=lifetime)\s*>\s*>",
        r"fn \g<name><\g<type>: for<'de> Deserialize<'de>>",
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


def _normalize_deep_link_defaults(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for node in RustInspection.from_content(content).nodes("struct_expression"):
        name = node.child_by_field_name("name")
        if (
            name is None
            or re.search(
                r"(?:^|::)DeepLinkResult::",
                name.text.decode("utf-8", errors="replace"),
            )
            is None
        ):
            continue
        original = node.text.decode("utf-8", errors="replace")
        normalized = re.sub(r"(?m)^\s*\.\.Default::default\(\)\s*,?\s*\n?", "", original)
        if normalized != original:
            replacements.append((original, normalized))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    return content


def _normalize_absolute_deep_link_paths(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).named("handle_deep_link"):
        if '"/comic/"' not in function.text:
            continue
        normalized = function.text
        if "then_some(url.as_str())" not in normalized:
            normalized = re.sub(
                r"(?P<url>[A-Za-z_]\w*)\.strip_prefix\("
                r'(?P<base>"https?://(?:\\.|[^"\\])*")\)',
                r"\g<url>.strip_prefix(\g<base>).or_else(|| "
                r"\g<url>.starts_with('/').then_some(\g<url>.as_str()))",
                normalized,
                count=1,
            )
        normalized = re.sub(
            r"(?P<value>[A-Za-z_]\w*)\.strip_prefix\(\"/comic/\"\)",
            r'\g<value>.split_once("/comic/").map(|(_, rest)| rest)',
            normalized,
        )
        if "DeepLinkResult::Chapter" not in normalized:
            normalized = re.sub(
                r"(?m)^(?P<indent>[ \t]*)if\s+(?P<path>[A-Za-z_]\w*)"
                r'\.starts_with\("/comic/"\)\s*\{',
                lambda match: (
                    f"{match.group('indent')}if let Some((manga_id, chapter_id)) = "
                    f'{match.group("path")}.split_once("/comic/")\n'
                    f"{match.group('indent')}    .map(|(_, rest)| rest)\n"
                    f"{match.group('indent')}    .and_then(|rest| "
                    'rest.split_once("/chapter/"))\n'
                    f"{match.group('indent')}{{\n"
                    f"{match.group('indent')}    let manga_key = "
                    'format!("/comic/{}", manga_id);\n'
                    f"{match.group('indent')}    let key = "
                    'format!("{}/chapter/{}", manga_key, chapter_id);\n'
                    f"{match.group('indent')}    return Ok(Some(DeepLinkResult::Chapter "
                    "{ manga_key, key }));\n"
                    f"{match.group('indent')}}}\n"
                    f'{match.group("indent")}if {match.group("path")}.starts_with("/comic/") {{'
                ),
                normalized,
                count=1,
            )
        normalized = re.sub(
            r'\bparts\.len\(\)\s*>=\s*2\s*&&\s*parts\[1\]\s*==\s*"chapter"',
            'parts.len() >= 3 && parts[1] == "chapter"',
            normalized,
        )
        if normalized != function.text:
            replacements.append((function.text, normalized))
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
        plain_header = re.search(r"->\s*Request\s*\{", text)
        result_header = re.search(r"->\s*(?:aidoku::)?Result\s*<\s*Request\s*>\s*\{", text)
        binding = re.search(
            r"let\s+mut\s+(?P<name>[A-Za-z_]\w*)\s*=\s*"
            r"(?P<expression>(?:aidoku::imports::net::)?Request::get\([^;]+\));",
            text,
        )
        if (plain_header is None and result_header is None) or binding is None:
            continue
        normalized = text
        if plain_header is not None:
            normalized = (
                text[: plain_header.start()] + "-> Result<Request> {" + text[plain_header.end() :]
            )
        expression = binding.group("expression")
        if ".header(" in expression:
            expression = re.sub(r"\?\s*$", "", expression)
        expression = re.sub(
            r"(?P<call>(?:aidoku::imports::net::)?Request::get\([^\r\n)]*\))(?!\?)",
            r"\g<call>?",
            expression,
            count=1,
        )
        replacement = re.sub(
            r"(?P<prefix>let\s+mut\s+[A-Za-z_]\w*\s*=\s*)[\s\S]*;",
            rf"\g<prefix>{expression};",
            binding.group(0),
            count=1,
        )
        normalized = normalized.replace(binding.group(0), replacement, 1)
        variable = binding.group("name")
        normalized = re.sub(
            rf"(?m)^(?P<indent>[ \t]*){re.escape(variable)}\s*\n"
            rf"(?P<closing>[ \t]*)\}}$",
            rf"\g<indent>Ok({variable})\n\g<closing>}}",
            normalized,
        )
        if normalized != text:
            helper_names.add(function.name)
            replacements.append((text, normalized))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    for name in helper_names:
        content = re.sub(
            rf"(?P<call>\b(?:self\.)?{re.escape(name)}\([^;\n]+\))(?=\.send\()",
            r"\g<call>?",
            content,
        )
        content = re.sub(
            rf"(?P<prefix>=\s*)(?P<call>\b(?:self\.)?{re.escape(name)}\([^;\n]+\))"
            rf"(?P<suffix>\s*;)",
            r"\g<prefix>\g<call>?\g<suffix>",
            content,
        )
    return content


def _normalize_json_envelope_helper(content: str) -> str:
    """Keep a generic JSON envelope intact when every caller expects that envelope."""
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        signature = function.text.split("{", 1)[0]
        result = re.search(r"->\s*(?:aidoku::)?Result\s*<\s*T\s*>", signature)
        envelope = re.search(
            r"\blet\s+(?P<variable>[A-Za-z_]\w*)\s*:\s*"
            r"(?P<envelope>(?:[A-Za-z_]\w*::)*[A-Za-z_]\w*)\s*<\s*T\s*>\s*=",
            function.text,
        )
        if result is None or envelope is None:
            continue
        variable = envelope.group("variable")
        returned = re.search(
            rf"\bOk\(\s*{re.escape(variable)}\.results\s*\)",
            function.text,
        )
        if returned is None:
            continue
        caller = re.search(
            rf":\s*{re.escape(envelope.group('envelope'))}\s*<[^;\n>]+>\s*=\s*"
            rf"(?:self\.)?{re.escape(function.name)}\s*\(",
            content,
        )
        if caller is None:
            continue
        normalized = re.sub(
            r"(->\s*(?:aidoku::)?Result\s*<\s*)T(\s*>)",
            rf"\g<1>{envelope.group('envelope')}<T>\g<2>",
            function.text,
            count=1,
        )
        normalized = re.sub(
            rf"\bOk\(\s*{re.escape(variable)}\.results\s*\)",
            f"Ok({variable})",
            normalized,
            count=1,
        )
        replacements.append((function.text, normalized))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    return content


def _normalize_rate_limit_integer_types(content: str) -> str:
    periods = {
        match.group("period")
        for match in re.finditer(
            r"set_rate_limit\(\s*[A-Za-z_]\w*\s*,\s*(?P<period>[A-Za-z_]\w*)\s*,",
            content,
        )
    }
    for name in periods:
        content = re.sub(
            rf"(\blet\s+(?:mut\s+)?{re.escape(name)}\s*:\s*)i64\b",
            r"\g<1>i32",
            content,
        )
    return content


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


def _normalize_source_new_delegation(content: str) -> str:
    inherent_new = {
        match.group("name")
        for match in re.finditer(
            r"impl\s+(?P<name>[A-Za-z_]\w*)\s*\{[\s\S]{0,12000}?\bpub\s+fn\s+new\s*\(",
            content,
        )
    }
    for name in inherent_new:
        content = re.sub(
            rf"(impl\s+Source\s+for\s+{re.escape(name)}\s*\{{[\s\S]{{0,1200}}?"
            rf"\bfn\s+new\s*\(\s*\)\s*->\s*Self\s*\{{\s*){re.escape(name)}::default\(\)",
            rf"\g<1>{name}::new()",
            content,
            count=1,
        )
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


def _normalize_safe_std_paths(content: str, *, remove_extern_std: bool) -> str:
    """Project allocation/core-only std paths into the no_std Aidoku runtime."""
    if remove_extern_std:
        content = re.sub(r"(?m)^\s*extern\s+crate\s+std\s*;\s*\n?", "", content)
    safe_aidoku_std_imports = {
        "String": "String",
        "ToString": "string::ToString",
        "Vec": "Vec",
        "format": "format",
        "vec": "vec",
    }
    for node in RustInspection.from_content(content).nodes("use_declaration"):
        original = node.text.decode("utf-8", errors="replace")
        if re.match(r"use\s+aidoku::", original) is None:
            continue
        match = re.search(r"\bstd::\{(?P<body>[^{}]+)\}", original)
        if match is None:
            continue
        items = [item.strip() for item in match.group("body").split(",") if item.strip()]
        if not items or any(item not in safe_aidoku_std_imports for item in items):
            continue
        projected = ", ".join(safe_aidoku_std_imports[item] for item in items)
        normalized = original[: match.start()] + f"alloc::{{{projected}}}" + original[match.end() :]
        content = content.replace(original, normalized, 1)
    content = re.sub(r"(?<!aidoku::)\balloc::vec!", "vec!", content)
    collection_aliases = {"HashMap": "BTreeMap", "HashSet": "BTreeSet"}
    for source, target in collection_aliases.items():
        marker = f"std::collections::{source}"
        if marker in content:
            content = content.replace(marker, f"aidoku::alloc::collections::{target}")
            content = re.sub(rf"\b{source}\b", target, content)
    content = content.replace("aidoku::alloc::collections::HashMap", "aidoku::HashMap")
    invalid_hash_set = "aidoku::alloc::collections::HashSet"
    if invalid_hash_set in content:
        content = content.replace(invalid_hash_set, "aidoku::alloc::collections::BTreeSet")
        content = re.sub(r"\bHashSet\b", "BTreeSet", content)
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
            + f'    if {argument.group("name")}.starts_with("http://") '
            + f'|| {argument.group("name")}.starts_with("https://") {{\n'
            + f"        aidoku::alloc::String::from({argument.group('name')})\n"
            + "    } else {\n"
            + f'        format!("{{}}/{{}}", {json.dumps(base)}, '
            + f"{argument.group('name')}.trim_start_matches('/'))\n"
            + "    }\n"
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
            + '    if relative.starts_with("http://") || relative.starts_with("https://") {\n'
            + "        aidoku::alloc::String::from(relative)\n"
            + "    } else {\n"
            + f'        format!("{{}}/{{}}", {json.dumps(base)}, '
            + "relative.trim_start_matches('/'))\n"
            + "    }\n}\n"
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
    content = re.sub(
        r"(?:[A-Za-z_][A-Za-z0-9_]*::)*[A-Za-z_][A-Za-z0-9_]*resolution"
        r"[A-Za-z0-9_]*\(\s*&(?P<receiver>[A-Za-z_][A-Za-z0-9_]*(?:\."
        r"[A-Za-z_][A-Za-z0-9_]*)*)\.cover\s*,[^)]*\)",
        r"\g<receiver>.cover.clone()",
        content,
    )
    return re.sub(
        r"(?:[A-Za-z_][A-Za-z0-9_]*::)*[A-Za-z_][A-Za-z0-9_]*"
        r"(?:resolution|image_url)[A-Za-z0-9_]*\(\s*&(?P<receiver>"
        r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\.cover)"
        r"\s*(?:,[^()]*)?\)",
        r"\g<receiver>.clone()",
        content,
    )


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
    trace: NormalizationTrace | None = None,
) -> str:
    """Apply small type-safe compatibility rewrites for the pinned Aidoku/Rust APIs."""
    active_trace = trace or NormalizationTrace()

    def apply(rewrite: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal content
        content = active_trace.apply(
            rewrite.__name__.removeprefix("_"),
            content,
            lambda value: rewrite(value, *args, **kwargs),
        )

    def record(rule_id: str, before: str) -> None:
        active_trace.observe(rule_id, before, content)

    apply(_normalize_boolean_let_some_alternatives)
    apply(_normalize_if_expression_arithmetic)
    apply(_normalize_safe_std_paths, remove_extern_std=remove_extern_std)
    apply(_normalize_graphql_request_body)
    content = normalize_aidoku_api_paths(content, trace=active_trace)
    apply(_normalize_generic_deserialize)
    apply(_normalize_graphql_body_fragment)
    apply(_normalize_html_element_text)
    apply(_normalize_utf8_slice_loops)
    apply(_normalize_index_length_guards)
    apply(_normalize_graphql_manga_update_projection)
    apply(_normalize_image_request_result)
    apply(_normalize_result_request_tails)
    apply(_normalize_detail_partial_move)
    apply(_normalize_manga_replacement_chapters)
    apply(_normalize_legacy_request_errors)
    apply(_normalize_defaults_get_bindings)
    apply(_normalize_owned_setting_routes)
    apply(_normalize_defaults_set_string_values)
    apply(_normalize_rsa_bootstrap_diagnostics)
    apply(_normalize_aidoku_result_errors)
    apply(_normalize_raw_json_response_bindings)
    apply(_normalize_request_builder_helpers, request_builder_helpers)
    apply(_normalize_json_envelope_helper)
    apply(_inject_source_new)
    apply(_normalize_source_new_delegation)
    apply(_normalize_rate_limit_integer_types)
    apply(_normalize_mutated_aidoku_models)
    apply(_normalize_default_model_assignments)
    apply(_normalize_page_index_fields)
    apply(_normalize_legacy_filter_fields)
    apply(_normalize_legacy_group_filters)
    apply(_normalize_select_filter_constructors)
    apply(_normalize_custom_page_context_types)
    apply(_normalize_legacy_page_context)
    apply(_normalize_page_url_context)
    apply(_normalize_page_context_maps)
    apply(_normalize_deep_link_defaults)
    apply(_normalize_absolute_deep_link_paths)
    apply(_normalize_parse_date_option_patterns)
    apply(_normalize_optional_chapter_dates)
    apply(_normalize_chapter_group_scope)
    before = content
    if allow_dead_code and not re.search(r"#!\[allow\([^\]]*\bdead_code\b", content):
        content = "#![allow(dead_code)]\n" + content.lstrip()
    record("allow_dead_code", before)
    before = content
    content = content.replace("aidoku::std::filters::SelectFilter", "aidoku::SelectFilter")
    content = content.replace("aidoku::filters::SelectFilter", "aidoku::SelectFilter")
    content = content.replace("aidoku::filter::SelectFilter", "aidoku::SelectFilter")
    record("select_filter_paths", before)
    content = normalize_aidoku_registration_imports(content, trace=active_trace)
    before = content
    if re.search(r"let\s+host\s*=\s*absolute\b", content):
        content = content.replace("Request::get(absolute)?", "Request::get(absolute.clone())?")
    record("clone_absolute_request_url", before)
    before = content
    content = re.sub(
        r'"(?:\\.|[^"\\])*"',
        lambda match: match.group(0).replace("}//", "}/"),
        content,
    )
    record("chapter_route_double_slash", before)
    apply(_normalize_idempotent_get_retry)
    content = normalize_aidoku_models(content, trace=active_trace)
    apply(_normalize_base_url_provider)
    apply(_normalize_comic_path_helper)
    apply(_normalize_struct_expression_defaults)
    apply(_normalize_pagination_result_impls)
    apply(_normalize_partial_move_pagination)
    apply(_normalize_partial_move_loop_pagination)
    apply(_normalize_collection_len_after_move)
    apply(_normalize_moved_field_collection_usage)
    apply(_normalize_overwritten_loop_initializers)
    apply(_normalize_moved_key_then_borrowed_url)
    apply(_normalize_select_filter_structs)
    apply(_normalize_resolution_regex)
    apply(_normalize_discarded_enumerate_index)
    apply(_normalize_filter_match_predicate)
    apply(_normalize_prequeried_url_helpers, prequeried_url_helpers)
    apply(_normalize_public_absolute_url, public_base_url)
    apply(_normalize_chapter_key_templates, chapter_key_templates)
    apply(_normalize_identical_if_branches)
    apply(_normalize_preserved_cover_urls, preserve_cover_urls)
    before = content
    content = re.sub(
        r"\blet\s+domain\s*=\s*defaults_get\(",
        "let domain: String = defaults_get(",
        content,
    )
    content = content.replace(
        "let domain: String = defaults_get(",
        "let domain: String = defaults_get::<String>(",
    )
    record("typed_domain_default", before)
    before = content
    content = re.sub(
        r"(?P<prefix>\b(?:id|title):\s*(?:Some\()?)"
        r'(?P<literal>"(?:\\.|[^"\\])*")\.to_string\(\)',
        r"\g<prefix>\g<literal>.into()",
        content,
    )
    record("aidoku_model_string_into", before)
    before = content
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
    record("borrow_header_values", before)
    content = normalize_generation_settings(
        content,
        setting_defaults=setting_defaults,
        setting_keys=setting_keys,
        setting_values=setting_values,
        trace=active_trace,
    )
    content = finalize_aidoku_imports(content, trace=active_trace)
    before = content
    content = re.sub(
        r"(\bparse_(?:local_)?date\s*\([^;]{0,800}?\))\s*\.ok\(\)",
        r"\1",
        content,
    )
    record("parse_date_result", before)
    before = content
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
    record("descending_sort_key", before)
    return content
