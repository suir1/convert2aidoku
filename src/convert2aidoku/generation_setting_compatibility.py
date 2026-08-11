from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping

from .constants import DEFAULT_BROWSER_USER_AGENT
from .models import GeneratedFile
from .normalization_trace import NormalizationTrace
from .rust_inspection import RustInspection

_PLATFORM_PROTOCOL_VALUES: Mapping[str, str | None] = {
    "platform.none": None,
    "platform.blank": " ",
    "platform.one": "1",
    "platform.two": "2",
    "platform.three": "3",
    "platform.four": "4",
    "platform.five": "5",
}


def platform_protocol_map(values: tuple[str, ...]) -> Mapping[str, str | None] | None:
    """Return storage-to-header mappings for enum-key or direct-value settings."""
    stored_values = set(values)
    if stored_values and stored_values.issubset(_PLATFORM_PROTOCOL_VALUES):
        if "platform.one" in stored_values:
            return {value: _PLATFORM_PROTOCOL_VALUES[value] for value in values}
        return None
    protocol_values = {value for value in _PLATFORM_PROTOCOL_VALUES.values() if value is not None}
    protocol_values.add("")
    if stored_values and stored_values.issubset(protocol_values) and "1" in stored_values:
        return {value: value or None for value in values}
    return None


_REQUEST_BINDING = re.compile(
    r"\blet\s+(?:mut\s+)?(?P<variable>[A-Za-z_]\w*)[^=;]*=\s*"
    r"(?:aidoku::imports::net::)?Request::(?:get|post|put|patch|delete)\s*\("
)


def _wrap_request_builder_results(
    content: str,
    *,
    helper_name: str,
    header_name: str,
) -> str:
    """Route a header through every function that returns a constructed Request."""
    replacements: list[tuple[str, str]] = []
    header_pattern = re.compile(rf'"{re.escape(header_name)}"', re.IGNORECASE)
    for function in RustInspection.from_content(content).functions:
        if (
            function.name == helper_name
            or helper_name in function.text
            or header_pattern.search(function.text) is not None
        ):
            continue
        function_text = function.text
        for binding in _REQUEST_BINDING.finditer(function_text):
            variable = binding.group("variable")
            for returned in re.finditer(r"\bOk\(\s*", function_text):
                start = returned.end()
                cursor = start
                wrappers = 0
                while helper := re.match(r"c2a_apply_[A-Za-z_]\w*\s*\(\s*", function_text[cursor:]):
                    wrappers += 1
                    cursor += helper.end()
                if not function_text.startswith(variable, cursor):
                    continue
                cursor += len(variable)
                if cursor < len(function_text) and (
                    function_text[cursor].isalnum() or function_text[cursor] == "_"
                ):
                    continue
                end = cursor
                for _ in range(wrappers):
                    whitespace = re.match(r"\s*", function_text[end:])
                    assert whitespace is not None
                    end += whitespace.end()
                    if end >= len(function_text) or function_text[end] != ")":
                        break
                    end += 1
                else:
                    receiver = function_text[start:end]
                    wrapped = (
                        function_text[:start] + f"{helper_name}({receiver})" + function_text[end:]
                    )
                    replacements.append((function_text, wrapped))
                    break
            else:
                continue
            break
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    return content


def _normalize_user_agent_setting(
    content: str,
    setting_defaults: Mapping[str, str] | None,
) -> str:
    """Route every generated User-Agent header through the recovered setting."""
    key = next(
        (
            candidate
            for candidate in (setting_defaults or {})
            if candidate.rsplit(".", 1)[-1] == "user_agent"
        ),
        None,
    )
    helper_name = "c2a_apply_user_agent"
    if key is None:
        return content
    helper_exists = re.search(rf"\bfn\s+{helper_name}\s*\(", content) is not None

    edits: list[tuple[int, int, bytes]] = []
    if not helper_exists:
        for call in RustInspection.from_content(content).nodes("call_expression"):
            function = call.child_by_field_name("function")
            arguments = call.child_by_field_name("arguments")
            if function is None or function.type != "field_expression" or arguments is None:
                continue
            method = function.child_by_field_name("field")
            receiver = function.child_by_field_name("value")
            values = arguments.named_children
            if (
                method is None
                or method.text.decode("utf-8", errors="replace") != "header"
                or receiver is None
                or len(values) < 2
            ):
                continue
            header = values[0].text.decode("utf-8", errors="replace")
            try:
                header_name = json.loads(header)
            except json.JSONDecodeError:
                continue
            if not isinstance(header_name, str) or header_name.casefold() != "user-agent":
                continue
            receiver_text = receiver.text.decode("utf-8", errors="replace")
            target = (
                call.parent
                if call.parent is not None and call.parent.type == "try_expression"
                else call
            )
            edits.append(
                (target.start_byte, target.end_byte, f"{helper_name}({receiver_text})".encode())
            )
    encoded = content.encode("utf-8")
    for start, end, replacement in sorted(edits, reverse=True):
        encoded = encoded[:start] + replacement + encoded[end:]
    normalized = encoded.decode("utf-8")
    implicitly_normalized = _wrap_request_builder_results(
        normalized,
        helper_name=helper_name,
        header_name="User-Agent",
    )
    if helper_exists:
        return implicitly_normalized
    if not edits and implicitly_normalized == normalized:
        return content
    helper = (
        f"fn {helper_name}(request: aidoku::imports::net::Request) "
        "-> aidoku::imports::net::Request {\n"
        "    let user_agent = "
        "aidoku::imports::defaults::defaults_get::<aidoku::alloc::String>"
        f"({json.dumps(key)}).unwrap_or_default();\n"
        "    match user_agent.as_str() {\n"
        '        "none" => request,\n'
        '        "" | "reset" | "desktop" | "mobile" | "app" => '
        f'request.header("User-Agent", {json.dumps(DEFAULT_BROWSER_USER_AGENT)}),\n'
        '        _ => request.header("User-Agent", &user_agent),\n'
        "    }\n"
        "}"
    )
    return implicitly_normalized.rstrip() + "\n\n" + helper + "\n"


def project_user_agent_setting(
    files: list[GeneratedFile],
    setting_defaults: Mapping[str, str],
) -> list[GeneratedFile]:
    updated: list[GeneratedFile] = []
    for generated in files:
        content = generated.content
        if generated.path.endswith(".rs"):
            content = _normalize_user_agent_setting(content, setting_defaults)
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


def _normalize_generated_setting_defaults(
    content: str,
    setting_defaults: Mapping[str, str] | None,
) -> str:
    for key, default in (setting_defaults or {}).items():
        if not default:
            continue
        key_literal = json.dumps(key, ensure_ascii=False)
        default_literal = json.dumps(default, ensure_ascii=False)
        rust_string = r'"(?:\\.|[^"\\])*"'
        key_constants = re.findall(
            rf"\bconst\s+([A-Za-z_]\w*)\s*:\s*&str\s*=\s*"
            rf"{re.escape(key_literal)}\s*;",
            content,
        )
        key_reference = (
            "(?:"
            + "|".join(
                [re.escape(key_literal), *(rf"\b{re.escape(name)}\b" for name in key_constants)]
            )
            + ")"
        )
        content = re.sub(
            rf"defaults_get(?:::<String>)?\(\s*{key_reference}\s*\)"
            rf"\s*(?:\.unwrap_or_default\(\)|\.unwrap_or_else\(\|\|\s*"
            rf"(?:String::from\(\s*{rust_string}\s*\)|{rust_string}\.into\(\)|"
            rf"{rust_string}\.to_string\(\))\s*\))",
            f"defaults_get::<String>({key_literal})"
            f".unwrap_or_else(|| String::from({default_literal}))",
            content,
        )
        content = re.sub(
            rf"(?P<prefix>defaults_get(?:::<String>)?\(\s*{key_reference}\s*\)"
            rf"[^;{{}}]{{0,500}}?\.unwrap_or_else\(\|\|\s*)"
            rf"{rust_string}\.to_string\(\)\s*\)",
            lambda match, literal=default_literal: (
                f"{match.group('prefix')}String::from({literal}))"
            ),
            content,
        )
        fallback_constants = set(
            re.findall(
                rf"defaults_get(?:::<String>)?\(\s*{key_reference}\s*\)"
                r"[^;{}]{0,500}?\.unwrap_or_else\(\|\|\s*"
                r"([A-Z][A-Z0-9_]*)\s*\.into\(\)\s*\)",
                content,
            )
        )
        for constant in fallback_constants:
            content = re.sub(
                rf"(?P<prefix>\bconst\s+{re.escape(constant)}\s*:\s*&str\s*=\s*)"
                rf"{rust_string}(?P<suffix>\s*;)",
                rf"\g<prefix>{default_literal}\g<suffix>",
                content,
            )
        if key.rsplit(".", 1)[-1] != "api_domain":
            continue
        function_replacements: list[tuple[str, str]] = []
        for function in RustInspection.from_content(content).functions:
            if (
                re.search(
                    rf"defaults_get(?:::<String>)?\(\s*{key_reference}\s*\)",
                    function.text,
                )
                is None
            ):
                continue
            setting_variables = set(
                re.findall(
                    rf"\blet\s+(?:mut\s+)?([A-Za-z_]\w*)\s*=\s*"
                    rf"defaults_get(?:::<String>)?\(\s*{key_reference}\s*\)"
                    r"[^;{}]{0,500};",
                    function.text,
                )
            )
            normalized = function.text
            match_replacements: list[tuple[str, str]] = []
            for match_node in RustInspection.from_content(function.text).nodes("match_expression"):
                match_text = match_node.text.decode("utf-8", errors="replace")
                scrutinee = match_text.split("{", 1)[0]
                uses_setting = re.search(
                    rf"defaults_get(?:::<String>)?\(\s*{key_reference}\s*\)",
                    scrutinee,
                ) is not None or any(
                    re.search(rf"\bmatch\s+{re.escape(variable)}\b", scrutinee)
                    for variable in setting_variables
                )
                if not uses_setting:
                    continue
                normalized_match = re.sub(
                    rf"(?P<prefix>\b_\s*=>\s*)"
                    rf"(?:String::from\(\s*{rust_string}\s*\)|"
                    rf"{rust_string}\.to_string\(\)|{rust_string}\.into\(\))",
                    rf"\g<prefix>String::from({default_literal})",
                    match_text,
                )
                if normalized_match != match_text:
                    match_replacements.append((match_text, normalized_match))
            for original, replacement in match_replacements:
                normalized = normalized.replace(original, replacement, 1)
            if normalized != function.text:
                function_replacements.append((function.text, normalized))
        for original, normalized in function_replacements:
            content = content.replace(original, normalized, 1)
    return content


def _normalize_dynamic_api_base(
    content: str,
    setting_defaults: Mapping[str, str] | None,
) -> str:
    api_key = next(
        (key for key in (setting_defaults or {}) if key.rsplit(".", 1)[-1] == "api_domain"),
        None,
    )
    if api_key is None:
        return content
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        if function.name != "api_url" and not function.name.endswith("_api_url"):
            continue
        normalized = re.sub(
            r"String::from\(\s*[A-Z][A-Z0-9_]*\s*\)",
            'format!("https://{}", api_domain())',
            function.text,
            count=1,
        )
        if normalized != function.text:
            replacements.append((function.text, normalized))
    if not replacements:
        return content
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    if re.search(r"\bfn\s+api_domain\s*\(", content):
        return content
    default = (setting_defaults or {}).get(api_key, "")
    helper = (
        "fn api_domain() -> String {\n"
        f"    defaults_get::<String>({json.dumps(api_key)})\n"
        f"        .unwrap_or_else(|| String::from({json.dumps(default)}))\n"
        "}"
    )
    return content.rstrip() + "\n\n" + helper + "\n"


def _normalize_generated_setting_key_aliases(
    content: str,
    setting_defaults: Mapping[str, str] | None,
    setting_keys: tuple[str, ...] | None = None,
) -> str:
    keys = tuple(dict.fromkeys([*(setting_keys or ()), *(setting_defaults or {}).keys()]))
    suffixes: dict[str, list[str]] = {}
    for key in keys:
        suffixes.setdefault(key.rsplit(".", 1)[-1].casefold(), []).append(key)
    aliases = {
        suffix: matches[0]
        for suffix, matches in suffixes.items()
        if len(matches) == 1 and suffix != matches[0]
    }
    for alias, key in aliases.items():
        alias_literal = json.dumps(alias, ensure_ascii=False)
        key_literal = json.dumps(key, ensure_ascii=False)
        content = re.sub(
            rf"(?P<prefix>defaults_get(?:::<[^>]+>)?\(\s*)"
            rf"{re.escape(alias_literal)}(?P<suffix>\s*\))",
            rf"\g<prefix>{key_literal}\g<suffix>",
            content,
        )
    canonical = {suffix: matches[0] for suffix, matches in suffixes.items() if len(matches) == 1}
    for literal in set(re.findall(r'"(?:\\.|[^"\\])*"', content)):
        try:
            value = json.loads(literal)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, str):
            continue
        prefix, separator, suffix = value.rpartition(".")
        suffix = suffix.casefold()
        if not separator or not suffix.startswith("http_"):
            continue
        normalized = suffix.removeprefix("http_")
        key = canonical.get(normalized)
        if key is not None and key.rpartition(".")[0].casefold() == prefix.casefold():
            content = content.replace(literal, json.dumps(key, ensure_ascii=False))
    return content


def _normalize_resolution_setting(
    content: str,
    setting_defaults: Mapping[str, str] | None,
    setting_values: Mapping[str, tuple[str, ...]] | None,
) -> str:
    defaults = setting_defaults or {}
    for key, values in (setting_values or {}).items():
        if (
            key.rsplit(".", 1)[-1] != "resolution"
            or not values
            or not all(re.fullmatch(r"resolution\.r[1-9][0-9]*", value) for value in values)
        ):
            continue
        default = defaults.get(key, values[-1]).rsplit(".r", 1)[-1]
        arms = "\n".join(
            f"Some({json.dumps(value)}) => String::from({json.dumps(value.rsplit('.r', 1)[-1])}),"
            for value in values
        )
        replacement = (
            f"match defaults_get::<String>({json.dumps(key)}).as_deref() {{\n"
            f"        {arms}\n"
            f"        _ => String::from({json.dumps(default)}),\n"
            "    }"
        )
        rust_string = r'"(?:\\.|[^"\\])*"'
        content = re.sub(
            rf"defaults_get(?:::<String>)?\(\s*{re.escape(json.dumps(key))}\s*\)"
            rf"\s*\.unwrap_or_else\(\|\|\s*String::from\(\s*{rust_string}\s*\)\s*\)",
            lambda _match, value=replacement: value,
            content,
        )
    return content


def _normalize_platform_header_setting(
    content: str,
    setting_defaults: Mapping[str, str] | None,
    setting_values: Mapping[str, tuple[str, ...]] | None,
) -> str:
    """Translate recovered enum storage keys before sending the platform header."""
    defaults = setting_defaults or {}
    candidates = {
        key: (values, protocol_map)
        for key, values in (setting_values or {}).items()
        if key.rsplit(".", 1)[-1] == "platform"
        and (protocol_map := platform_protocol_map(values)) is not None
    }
    if not candidates:
        return content

    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        function_text = function.text
        key = next(
            (candidate for candidate in candidates if json.dumps(candidate) in function_text),
            None,
        )
        vector_push = None
        if key is not None:
            binding = (
                rf"(?P<indent>^[ \t]*)if\s+let\s+Some\((?P<variable>[A-Za-z_]\w*)\)"
                rf"\s*=\s*(?:aidoku::imports::defaults::)?defaults_get(?:::<String>)?\(\s*"
                rf"{re.escape(json.dumps(key))}\s*\)\s*"
            )
            vector_push = re.search(
                binding + r"(?:&&\s*!\s*(?P=variable)\.is_empty\(\)\s*)?"
                r"\{(?P<body>[^{}]*\"platform\"[^{}]*\b(?P=variable)\b[^{}]*)\}",
                function_text,
                re.MULTILINE,
            ) or re.search(
                binding + r"\{\s*if\s+!\s*(?P=variable)\.is_empty\(\)\s*"
                r"\{(?P<body>[^{}]*\"platform\"[^{}]*\b(?P=variable)\b[^{}]*)\}\s*\}",
                function_text,
                re.MULTILINE,
            )
        if vector_push is not None:
            indent = vector_push.group("indent")
            variable = vector_push.group("variable")
            arms = []
            values, protocol_map = candidates[key]
            for stored in values:
                protocol_value = protocol_map[stored]
                expression = (
                    "None"
                    if protocol_value is None
                    else f"Some(String::from({json.dumps(protocol_value)}))"
                )
                arms.append(f"{indent}    Some({json.dumps(stored)}) => {expression},")
            default = defaults.get(key, "platform.one")
            protocol_default = protocol_map.get(default, "1")
            default_expression = (
                "None"
                if protocol_default is None
                else f"Some(String::from({json.dumps(protocol_default)}))"
            )
            arms.append(f"{indent}    _ => {default_expression},")
            replacement = (
                f"{indent}let {variable} = match "
                "aidoku::imports::defaults::defaults_get::<String>"
                f"({json.dumps(key)}).as_deref() {{\n"
                + "\n".join(arms)
                + f"\n{indent}}};\n"
                + f"{indent}if let Some({variable}) = {variable} {{"
                + vector_push.group("body")
                + "}"
            )
            normalized = (
                function_text[: vector_push.start()]
                + replacement
                + function_text[vector_push.end() :]
            )
            replacements.append((function_text, normalized))
            continue
        if (
            key is not None
            and '"platform"' in function_text
            and "Option<" not in function_text.split("{", 1)[0]
        ):
            rust_string = r'"(?:\\.|[^"\\])*"'
            binding = re.search(
                rf"(?P<indent>^[ \t]*)let\s+(?P<variable>[A-Za-z_]\w*)"
                rf"(?:\s*:\s*String)?\s*=\s*"
                rf"(?:aidoku::imports::defaults::)?defaults_get(?:::<String>)?\(\s*"
                rf"{re.escape(json.dumps(key))}\s*\)\s*\.unwrap_or_else\(\|\|\s*"
                rf"String::from\(\s*{rust_string}\s*\)\s*\)\s*;",
                function_text,
                re.MULTILINE,
            )
            if (
                binding is not None
                and f"match {binding.group('variable')}.as_str()" not in function_text
                and re.search(
                    rf'"platform"[\s\S]{{0,160}}\b{re.escape(binding.group("variable"))}\b',
                    function_text,
                )
            ):
                indent = binding.group("indent")
                arms = []
                values, protocol_map = candidates[key]
                for stored in values:
                    protocol_value = protocol_map[stored]
                    expression = (
                        "String::new()"
                        if protocol_value is None
                        else f"String::from({json.dumps(protocol_value)})"
                    )
                    arms.append(f"{indent}    Some({json.dumps(stored)}) => {expression},")
                default = defaults.get(key, "platform.one")
                protocol_default = protocol_map.get(default, "1")
                default_expression = (
                    "String::new()"
                    if protocol_default is None
                    else f"String::from({json.dumps(protocol_default)})"
                )
                arms.append(f"{indent}    _ => {default_expression},")
                replacement = (
                    f"{indent}let {binding.group('variable')} = "
                    "match aidoku::imports::defaults::defaults_get::<String>"
                    f"({json.dumps(key)}).as_deref() {{\n" + "\n".join(arms) + f"\n{indent}}};"
                )
                normalized = (
                    function_text[: binding.start()] + replacement + function_text[binding.end() :]
                )
                replacements.append((function_text, normalized))
                continue
        if (
            key is None
            or '"platform"' not in function_text
            or ".map(" not in function_text
            or "Option<" not in function_text.split("{", 1)[0]
        ):
            continue
        opening = function_text.find("{")
        if opening < 0:
            continue
        default = defaults.get(key, "platform.one")
        arms = []
        values, protocol_map = candidates[key]
        for stored in values:
            protocol_value = protocol_map[stored]
            if protocol_value is None:
                arms.append(f"        {json.dumps(stored)} => None,")
            else:
                arms.append(
                    f'        {json.dumps(stored)} => Some(("platform", '
                    f"String::from({json.dumps(protocol_value)}))),"
                )
        arms.append("        _ => None,")
        replacement = (
            function_text[:opening].rstrip()
            + " {\n"
            + "    let platform = aidoku::imports::defaults::defaults_get::<String>"
            + f"({json.dumps(key)})\n"
            + f"        .unwrap_or_else(|| String::from({json.dumps(default)}));\n"
            + "    match platform.as_str() {\n"
            + "\n".join(arms)
            + "\n    }\n}"
        )
        replacements.append((function_text, replacement))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    helper_name = "c2a_apply_platform"
    helper_exists = re.search(rf"\bfn\s+{helper_name}\s*\(", content) is not None
    wrapped = _wrap_request_builder_results(
        content,
        helper_name=helper_name,
        header_name="platform",
    )
    if wrapped == content or helper_exists:
        return wrapped
    key = next(iter(candidates))
    values, protocol_map = candidates[key]
    arms = []
    for stored in values:
        protocol = protocol_map[stored]
        rendered = "None" if protocol is None else f"Some({json.dumps(protocol)})"
        arms.append(f"        Some({json.dumps(stored)}) => {rendered},")
    default = defaults.get(key, "platform.one")
    default_protocol = protocol_map.get(default, "1")
    rendered_default = (
        "None" if default_protocol is None else f"Some({json.dumps(default_protocol)})"
    )
    arms.append(f"        _ => {rendered_default},")
    helper = (
        f"fn {helper_name}(request: aidoku::imports::net::Request) "
        "-> aidoku::imports::net::Request {\n"
        "    let platform = match "
        "aidoku::imports::defaults::defaults_get::<aidoku::alloc::String>"
        f"({json.dumps(key)}).as_deref() {{\n"
        + "\n".join(arms)
        + "\n    };\n"
        + "    match platform {\n"
        + '        Some(platform) => request.header("platform", &platform),\n'
        + "        None => request,\n"
        + "    }\n"
        + "}"
    )
    return wrapped.rstrip() + "\n\n" + helper + "\n"


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


def _normalize_typed_domain_default(content: str) -> str:
    content = re.sub(
        r"\blet\s+domain\s*=\s*defaults_get\(",
        "let domain: String = defaults_get(",
        content,
    )
    return content.replace(
        "let domain: String = defaults_get(",
        "let domain: String = defaults_get::<String>(",
    )


def normalize_runtime_setting_access(content: str, *, trace: NormalizationTrace) -> str:
    for rewrite in (
        _normalize_defaults_get_bindings,
        _normalize_owned_setting_routes,
        _normalize_defaults_set_string_values,
    ):
        content = trace.apply(rewrite.__name__.removeprefix("_"), content, rewrite)
    return content


def normalize_typed_domain_setting(content: str, *, trace: NormalizationTrace) -> str:
    return trace.apply("typed_domain_default", content, _normalize_typed_domain_default)


def normalize_generation_settings(
    content: str,
    *,
    setting_defaults: Mapping[str, str] | None,
    setting_keys: tuple[str, ...] | None,
    setting_values: Mapping[str, tuple[str, ...]] | None,
    trace: NormalizationTrace,
) -> str:
    """Apply recovered setting compatibility while preserving stable trace rule IDs."""

    def apply(rewrite: Callable[..., str], *args: object) -> None:
        nonlocal content
        content = trace.apply(
            rewrite.__name__.removeprefix("_"),
            content,
            lambda value: rewrite(value, *args),
        )

    apply(_normalize_dynamic_api_base, setting_defaults)
    apply(_normalize_generated_setting_key_aliases, setting_defaults, setting_keys)
    apply(_normalize_generated_setting_defaults, setting_defaults)
    apply(_normalize_resolution_setting, setting_defaults, setting_values)
    apply(_normalize_platform_header_setting, setting_defaults, setting_values)
    apply(_normalize_user_agent_setting, setting_defaults)
    return content
