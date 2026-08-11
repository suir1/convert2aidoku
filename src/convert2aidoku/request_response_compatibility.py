from __future__ import annotations

import re

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


def _borrow_header_values(content: str) -> str:
    content = content.replace(
        '.header("User-Agent", get_user_agent())',
        '.header("User-Agent", &get_user_agent())',
    )
    content = content.replace(".header(key, val)", ".header(key, &val)")
    return re.sub(
        r'(?P<prefix>\.header\(\s*"(?:\\.|[^"\\])*"\s*,\s*)'
        r"(?P<value>[A-Za-z_]\w*)(?P<suffix>\s*\))",
        r"\g<prefix>&\g<value>\g<suffix>",
        content,
    )


def normalize_generic_response_models(content: str, *, trace: NormalizationTrace) -> str:
    return trace.apply("normalize_generic_deserialize", content, _normalize_generic_deserialize)


def normalize_html_response_values(content: str, *, trace: NormalizationTrace) -> str:
    return trace.apply("normalize_html_element_text", content, _normalize_html_element_text)


def normalize_pagination_response_models(content: str, *, trace: NormalizationTrace) -> str:
    return trace.apply(
        "normalize_pagination_result_impls",
        content,
        _normalize_pagination_result_impls,
    )


def normalize_request_header_values(content: str, *, trace: NormalizationTrace) -> str:
    return trace.apply("borrow_header_values", content, _borrow_header_values)


def normalize_request_result_tails(content: str, *, trace: NormalizationTrace) -> str:
    return trace.apply("normalize_result_request_tails", content, _normalize_result_request_tails)


def normalize_legacy_request_compatibility(content: str, *, trace: NormalizationTrace) -> str:
    return trace.apply("normalize_legacy_request_errors", content, _normalize_legacy_request_errors)


def normalize_request_response_compatibility(
    content: str,
    *,
    request_builder_helpers: set[str] | None,
    trace: NormalizationTrace,
) -> str:
    content = trace.apply(
        "normalize_aidoku_result_errors", content, _normalize_aidoku_result_errors
    )
    content = trace.apply(
        "normalize_raw_json_response_bindings",
        content,
        _normalize_raw_json_response_bindings,
    )
    content = trace.apply(
        "normalize_request_builder_helpers",
        content,
        lambda value: _normalize_request_builder_helpers(value, request_builder_helpers),
    )
    return trace.apply("normalize_json_envelope_helper", content, _normalize_json_envelope_helper)


def normalize_request_retry_compatibility(content: str, *, trace: NormalizationTrace) -> str:
    return trace.apply("normalize_idempotent_get_retry", content, _normalize_idempotent_get_retry)
