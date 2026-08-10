from __future__ import annotations

import json
import re
from collections.abc import Mapping

from .constants import DEFAULT_BROWSER_USER_AGENT
from .decompiled_input import decompiled_detail_uses_api_envelope
from .models import GeneratedFile, GenerationManifest, SourceIR
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


def _platform_protocol_map(values: tuple[str, ...]) -> Mapping[str, str | None] | None:
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


def _project_user_agent_setting(
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


def _prequeried_url_helpers(manifest: GenerationManifest) -> set[str]:
    helpers: set[str] = set()
    for generated in manifest.files:
        if not generated.path.endswith(".rs"):
            continue
        for function in RustInspection.from_content(generated.content).functions:
            if re.search(r'"(?:\\.|[^"\\])*\?(?:\\.|[^"\\])*"', function.text):
                helpers.add(function.name)
    return helpers


def _request_builder_helpers(manifest: GenerationManifest) -> set[str]:
    helpers: set[str] = set()
    for generated in manifest.files:
        if not generated.path.endswith(".rs"):
            continue
        for function in RustInspection.from_content(generated.content).functions:
            if "Request::get" in function.text and ".header(" in function.text:
                helpers.add(function.name)
    return helpers


def _request_header_lines(headers: Mapping[str, str], *, indent: str) -> list[str]:
    return [
        f"{indent}request = request.header({json.dumps(name)}, {json.dumps(value)});"
        for name, value in headers.items()
    ]


def _recovered_request_builder(
    ir: SourceIR,
    *,
    setting_defaults: Mapping[str, str],
    setting_values: Mapping[str, tuple[str, ...]],
) -> str | None:
    profiles = [profile for profile in ir.request_header_profiles if profile.headers]
    if not profiles and not ir.shared_request_headers:
        return None
    api_key = next(
        (key for key in setting_defaults if key.rsplit(".", 1)[-1] == "api_domain"), None
    )
    default_domain = setting_defaults.get(api_key, "") if api_key else ""
    default_profile = next(
        (profile for profile in profiles if default_domain in profile.domains),
        profiles[0] if profiles else None,
    )
    lines = [
        "fn c2a_request(url: &str) -> aidoku::Result<aidoku::imports::net::Request> {",
        "    let mut request = aidoku::imports::net::Request::get(url)?;",
    ]
    conditional = [
        profile for profile in profiles if profile is not default_profile and profile.domains
    ]
    for index, profile in enumerate(conditional):
        condition = " || ".join(f"url.contains({json.dumps(domain)})" for domain in profile.domains)
        lines.append(("    if " if index == 0 else "    else if ") + condition + " {")
        lines.extend(_request_header_lines(profile.headers, indent="        "))
        lines.append("    }")
    if default_profile is not None:
        if conditional:
            lines.append("    else {")
            lines.extend(_request_header_lines(default_profile.headers, indent="        "))
            lines.append("    }")
        else:
            lines.extend(_request_header_lines(default_profile.headers, indent="    "))
    lines.extend(_request_header_lines(ir.shared_request_headers, indent="    "))

    platform_key = next(
        (
            key
            for key, values in setting_values.items()
            if key.rsplit(".", 1)[-1] == "platform" and _platform_protocol_map(values) is not None
        ),
        None,
    )
    if platform_key is not None:
        protocol_map = _platform_protocol_map(setting_values[platform_key])
        assert protocol_map is not None
        default = setting_defaults.get(platform_key, "platform.one")
        lines.extend(
            [
                "    let platform = match "
                "aidoku::imports::defaults::defaults_get::<aidoku::alloc::String>"
                f"({json.dumps(platform_key)}).as_deref() {{",
            ]
        )
        for stored in setting_values[platform_key]:
            protocol = protocol_map[stored]
            rendered = "None" if protocol is None else f"Some({json.dumps(protocol)})"
            lines.append(f"        Some({json.dumps(stored)}) => {rendered},")
        default_protocol = protocol_map.get(default, "1")
        rendered_default = (
            "None" if default_protocol is None else f"Some({json.dumps(default_protocol)})"
        )
        lines.extend(
            [
                f"        _ => {rendered_default},",
                "    };",
                "    if let Some(platform) = platform {",
                '        request = request.header("platform", &platform);',
                "    }",
            ]
        )

    user_agent_key = next(
        (key for key in setting_defaults if key.rsplit(".", 1)[-1] == "user_agent"),
        None,
    )
    if user_agent_key is not None and "User-Agent" in ir.header_names:
        lines.extend(
            [
                '    request = request.header("User-Agent", '
                f"{json.dumps(DEFAULT_BROWSER_USER_AGENT)});",
            ]
        )
    lines.extend(["    Ok(request)", "}"])
    return "\n".join(lines)


def _project_recovered_request_headers(
    ir: SourceIR,
    files: list[GeneratedFile],
    *,
    setting_defaults: Mapping[str, str],
    setting_values: Mapping[str, tuple[str, ...]],
) -> list[GeneratedFile]:
    files = _project_shared_request_headers(ir.shared_request_headers, files)
    builder = _recovered_request_builder(
        ir,
        setting_defaults=setting_defaults,
        setting_values=setting_values,
    )
    if builder is None:
        return files
    updated = []
    projected = False
    for generated in files:
        content = generated.content
        if projected or not generated.path.endswith(".rs") or "fn c2a_request(" in content:
            updated.append(generated)
            continue
        replacement = None
        for function in RustInspection.from_content(content).functions:
            if (
                "Request::get" in function.text
                and ".send()" in function.text
                and re.search(r"\burl\s*:\s*&str\b", function.text)
                and "Response" in function.text.split("{", 1)[0]
                and ".header(" not in function.text
            ):
                header = function.text.split("{", 1)[0].rstrip()
                replacement = (
                    header
                    + "{\n"
                    + "    let response = match c2a_request(url)?.send() {\n"
                    + "        Ok(response) => response,\n"
                    + "        Err(_) => c2a_request(url)?.send()?,\n"
                    + "    };\n"
                    + "    Ok(response)\n"
                    + "}"
                )
                content = content.replace(function.text, replacement, 1)
                content = content.rstrip() + "\n\n" + builder + "\n"
                projected = True
                break
        updated.append(
            generated.model_copy(update={"content": content})
            if replacement is not None
            else generated
        )
    return updated


def _project_shared_request_headers(
    headers: Mapping[str, str],
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    if not headers:
        return files
    updated: list[GeneratedFile] = []
    for generated in files:
        content = generated.content
        if not generated.path.endswith(".rs") or generated.path == "src/c2a_source_traits.rs":
            updated.append(generated)
            continue
        replacements: list[tuple[str, str]] = []
        for function in RustInspection.from_content(content).functions:
            if "Request::get" not in function.text:
                continue
            missing = [
                (name, value)
                for name, value in headers.items()
                if re.search(
                    rf"\.header\(\s*{re.escape(json.dumps(name))}\s*,",
                    function.text,
                    re.IGNORECASE,
                )
                is None
            ]
            if not missing:
                continue
            local = RustInspection.from_content(function.text)
            projected_function = None
            for call in local.nodes("call_expression"):
                callee = call.child_by_field_name("function")
                if callee is None or callee.type != "field_expression":
                    continue
                field = callee.child_by_field_name("field")
                receiver = callee.child_by_field_name("value")
                if (
                    field is None
                    or field.text != b"send"
                    or receiver is None
                    or "Request::get" not in receiver.text.decode("utf-8", errors="replace")
                ):
                    continue
                request = receiver.text.decode("utf-8", errors="replace")
                projected_request = request + "".join(
                    f".header({json.dumps(name)}, {json.dumps(value)})" for name, value in missing
                )
                encoded = function.text.encode("utf-8")
                projected_function = (
                    encoded[: receiver.start_byte]
                    + projected_request.encode("utf-8")
                    + encoded[receiver.end_byte :]
                ).decode("utf-8")
                break
            if projected_function is not None:
                replacements.append((function.text, projected_function))
                continue
            for call in local.nodes("call_expression"):
                callee = call.child_by_field_name("function")
                arguments = call.child_by_field_name("arguments")
                if (
                    callee is None
                    or callee.text != b"Ok"
                    or arguments is None
                    or len(arguments.named_children) != 1
                ):
                    continue
                argument_node = arguments.named_children[0]
                argument = argument_node.text.decode("utf-8", errors="replace")
                if "Request::get" not in argument:
                    if not re.fullmatch(r"[A-Za-z_]\w*", argument):
                        continue
                    request_binding = re.search(
                        rf"\blet\s+(?:mut\s+)?{re.escape(argument)}(?:\s*:[^=;]+)?\s*=\s*"
                        r"(?:aidoku::imports::net::)?Request::get\b",
                        function.text,
                    )
                    request_name = re.search(
                        r"(?:^|_)(?:request|req|retry)(?:_|$)",
                        argument,
                        re.IGNORECASE,
                    )
                    if request_binding is None and request_name is None:
                        continue
                projected = argument + "".join(
                    f".header({json.dumps(name)}, {json.dumps(value)})" for name, value in missing
                )
                encoded = function.text.encode("utf-8")
                replacements.append(
                    (
                        function.text,
                        (
                            encoded[: argument_node.start_byte]
                            + projected.encode("utf-8")
                            + encoded[argument_node.end_byte :]
                        ).decode("utf-8"),
                    )
                )
                break
        for original, normalized in replacements:
            content = content.replace(original, normalized, 1)
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


def _project_recovered_detail_api_envelope(
    ir: SourceIR,
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    if not decompiled_detail_uses_api_envelope(ir.files):
        return files
    inspection = RustInspection(
        generated.content for generated in files if generated.path.endswith(".rs")
    )
    envelope = inspection.struct_named("ApiResponse")
    if envelope is None or inspection.struct_field_type("ApiResponse", "results") != "T":
        return files

    updated: list[GeneratedFile] = []
    for generated in files:
        content = generated.content
        if not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        replacements: list[tuple[str, str]] = []
        for function in RustInspection.from_content(content).functions:
            signature = function.text.split("{", 1)[0]
            result = re.search(
                r"->\s*(?:aidoku::)?Result\s*<\s*"
                r"(?P<detail>(?:[A-Za-z_]\w*::)*(?:Comic)?DetailResult)\s*>",
                signature,
            )
            if (
                result is None
                or "detail" not in function.name.lower()
                or "comic2" not in function.text
                or "ApiResponse<" in function.text
            ):
                continue
            for call in RustInspection.from_content(function.text).nodes("call_expression"):
                callee = call.child_by_field_name("function")
                if (
                    callee is None
                    or not callee.text.decode("utf-8", errors="replace").endswith("get_json")
                    or call.parent is None
                    or call.parent.type != "block"
                    or call.parent.parent is None
                    or call.parent.parent.type != "function_item"
                ):
                    continue
                original = call.text.decode("utf-8", errors="replace")
                indent = " " * call.start_point.column
                inner = indent + "    "
                replacement = (
                    "{\n"
                    f"{inner}let response: ApiResponse<{result.group('detail')}> = {original}?;\n"
                    f"{inner}Ok(response.results)\n"
                    f"{indent}}}"
                )
                replacements.append(
                    (function.text, function.text.replace(original, replacement, 1))
                )
                break
        for original, replacement in replacements:
            content = content.replace(original, replacement, 1)
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


def _variant_profile_domains(ir: SourceIR, variant_name: str) -> tuple[str, ...]:
    variant_token = re.sub(r"[^a-z0-9]", "", variant_name.lower())
    domains: list[str] = []
    for profile in ir.request_header_profiles:
        profile_token = re.sub(r"[^a-z0-9]", "", profile.name.lower())
        if variant_token and variant_token in profile_token:
            domains.extend(profile.domains)
    return tuple(dict.fromkeys(domains))


def _project_recovered_chapter_page_variants(
    ir: SourceIR,
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    rules: list[tuple[str, str, str, tuple[str, ...]]] = []
    requires_api_v3 = any(
        route.endpoint_template.startswith("/api/v3/") for route in ir.chapter_page_routes
    )
    for route in ir.chapter_page_routes:
        default = next((variant for variant in route.variants if variant.is_default), None)
        if default is None or len(default.replacements) != 1:
            continue
        replacement = default.replacements[0]
        for variant in route.variants:
            domains = _variant_profile_domains(ir, variant.name)
            if (
                variant.is_default
                or not domains
                or variant.strip_prefix != default.strip_prefix
                or variant.replacements
            ):
                continue
            helper = "c2a_is_" + re.sub(r"[^a-z0-9]+", "_", variant.name.lower()).strip("_")
            rules.append((replacement.old, replacement.new, helper + "_domain", domains))
    if not rules:
        return files

    updated: list[GeneratedFile] = []
    for generated in files:
        content = generated.content
        if not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        for old, new, helper, domains in rules:
            if f"fn {helper}(" in content:
                continue
            old_literal = json.dumps(old)
            new_literal = json.dumps(new)
            pattern = re.compile(
                rf"(?m)^(?P<indent>[ \t]*)let\s+(?P<variable>[A-Za-z_]\w*)\s*=\s*"
                rf"(?P<base>[A-Za-z_]\w*[^;]{{0,700}}?)"
                rf"\.replace\(\s*{re.escape(old_literal)}\s*,\s*"
                rf"{re.escape(new_literal)}\s*\)\s*;"
            )
            match = pattern.search(content)
            if match is None:
                continue
            indent = match.group("indent")
            inner = indent + "    "
            variable = match.group("variable")
            base = match.group("base").rstrip()
            replacement = (
                f"{indent}let c2a_chapter_key = {base};\n"
                f"{indent}let {variable} = if {helper}(&api_domain()) {{\n"
                f"{inner}aidoku::alloc::String::from(c2a_chapter_key)\n"
                f"{indent}}} else {{\n"
                f"{inner}c2a_chapter_key.replace({old_literal}, {new_literal})\n"
                f"{indent}}};"
            )
            domain_patterns = " | ".join(json.dumps(domain) for domain in domains)
            helper_content = (
                f"fn {helper}(domain: &str) -> bool {{\n    matches!(domain, {domain_patterns})\n}}"
            )
            content = (
                content[: match.start()]
                + replacement
                + content[match.end() :].rstrip()
                + "\n\n"
                + helper_content
                + "\n"
            )
        if requires_api_v3 and re.search(r"\bfn\s+api_url\s*\(", content):
            base_replacements = [
                (function.text, function.text.replace("self.api_base()", "self.api_url()"))
                for function in RustInspection.from_content(content).functions
                if "chapter" in function.name.lower()
                and "url" in function.name.lower()
                and "self.api_base()" in function.text
            ]
            for original, replacement in base_replacements:
                content = content.replace(original, replacement, 1)
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


def _project_recovered_chapter_image_resolution(
    ir: SourceIR,
    files: list[GeneratedFile],
    *,
    setting_defaults: Mapping[str, str],
    setting_values: Mapping[str, tuple[str, ...]],
) -> list[GeneratedFile]:
    policy = ir.image_url_policy
    if policy is None or policy.chapter_resolution_regex != r"\d+(?=x\.(?:jpg|webp)$)":
        return files
    rust_content = "\n".join(
        generated.content for generated in files if generated.path.endswith(".rs")
    )
    if "c2a_translate_chapter_resolution" in rust_content or (
        "resolution" in rust_content
        and 'ends_with(".jpg")' in rust_content
        and 'ends_with(".webp")' in rust_content
    ):
        return files
    resolution_key = next(
        (
            key
            for key, values in setting_values.items()
            if key.rsplit(".", 1)[-1] == "resolution"
            and values
            and all(re.fullmatch(r"resolution\.r[1-9][0-9]*", value) for value in values)
        ),
        None,
    )
    if resolution_key is None:
        return files
    values = setting_values[resolution_key]
    default = setting_defaults.get(resolution_key, values[-1])
    arms = "\n".join(
        f"        Some({json.dumps(value)}) => {json.dumps(value.rsplit('.r', 1)[-1])},"
        for value in values
    )
    helper = (
        "fn c2a_translate_chapter_resolution(url: aidoku::alloc::String) "
        "-> aidoku::alloc::String {\n"
        "    let resolution = match "
        "aidoku::imports::defaults::defaults_get::<aidoku::alloc::String>"
        f"({json.dumps(resolution_key)}).as_deref() {{\n"
        f"{arms}\n"
        f"        _ => {json.dumps(default.rsplit('.r', 1)[-1])},\n"
        "    };\n"
        '    let suffix_start = if url.ends_with(".jpg") {\n'
        "        Some(url.len() - 4)\n"
        '    } else if url.ends_with(".webp") {\n'
        "        Some(url.len() - 5)\n"
        "    } else {\n"
        "        None\n"
        "    };\n"
        "    if let Some(suffix_start) = suffix_start {\n"
        "        let before_suffix = &url[..suffix_start];\n"
        "        if let Some(x_pos) = before_suffix.rfind('x') {\n"
        "            let before_x = &before_suffix[..x_pos];\n"
        "            let digits_start = before_x\n"
        "                .rfind(|character: char| !character.is_ascii_digit())\n"
        "                .map_or(0, |position| position + 1);\n"
        "            if digits_start < x_pos {\n"
        '                return aidoku::alloc::format!("{}{}{}", '
        "&url[..digits_start], resolution, &url[x_pos..]);\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "    url\n"
        "}"
    )

    updated: list[GeneratedFile] = []
    helper_added = False
    for generated in files:
        content = generated.content
        if not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        edits: list[tuple[int, int, bytes]] = []
        inspection = RustInspection.from_content(content)
        for function in inspection.functions:
            if "page" not in function.name.lower():
                continue
            for call in RustInspection.from_content(function.text).nodes("call_expression"):
                callee = call.child_by_field_name("function")
                arguments = call.child_by_field_name("arguments")
                if callee is None or arguments is None or not arguments.named_children:
                    continue
                if callee.text.decode("utf-8", errors="replace") not in {
                    "PageContent::url",
                    "PageContent::url_context",
                }:
                    continue
                url = arguments.named_children[0]
                url_text = url.text.decode("utf-8", errors="replace")
                if "c2a_translate_chapter_resolution" in url_text:
                    continue
                edits.append(
                    (
                        function.node.start_byte + url.start_byte,
                        function.node.start_byte + url.end_byte,
                        f"c2a_translate_chapter_resolution({url_text})".encode(),
                    )
                )
        encoded = content.encode("utf-8")
        for begin, end, replacement in sorted(edits, reverse=True):
            encoded = encoded[:begin] + replacement + encoded[end:]
        content = encoded.decode("utf-8")
        if edits and not helper_added:
            content = content.rstrip() + "\n\n" + helper + "\n"
            helper_added = True
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated
