from __future__ import annotations

import json
import re

from .analysis_common import input_license, match
from .errors import InputError, UnsupportedSourceError
from .ingest import ResolvedSource, collect_source_files
from .input_capabilities import InputCapabilityRecognition, recognize_input_capabilities
from .models import (
    ContentRating,
    SourceFilterOption,
    SourceFilterSpec,
    SourceIR,
    SourceMetadata,
)

_KOTLIN_STRING = r'"(?:\\.|[^"\\])*"'


def _decode_kotlin_string(literal: str) -> str | None:
    try:
        value = json.loads(literal)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


def _filter_value(expression: str) -> str | None:
    value = expression.strip()
    if not value:
        return None
    if value.startswith('"'):
        return _decode_kotlin_string(value)
    if value == "null":
        return ""
    if re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        return value
    if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", value):
        return value.rsplit(".", 1)[-1]
    return None


def _snake_case(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def _kotlin_string_constants(kotlin: str) -> dict[str, str]:
    constants: dict[str, str] = {}
    for found in re.finditer(
        rf"\bconst\s+val\s+(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<value>{_KOTLIN_STRING})",
        kotlin,
    ):
        value = _decode_kotlin_string(found.group("value"))
        if value is not None:
            constants[found.group("name")] = value
    return constants


def _uri_part_filter_value(expression: str, constants: dict[str, str]) -> str | None:
    value = expression.strip()
    if value.startswith('"'):
        decoded = _decode_kotlin_string(value)
        if decoded is None:
            return None

        def expand(match: re.Match[str]) -> str:
            name = match.group("braced") or match.group("plain")
            return constants.get(name, match.group(0))

        return re.sub(
            r"\$\{(?P<braced>[A-Za-z_]\w*)\}|\$(?P<plain>[A-Za-z_]\w*)",
            expand,
            decoded,
        )
    if value in constants:
        return constants[value]
    return _filter_value(value)


def _kotlin_uri_part_filter_specs(kotlin: str) -> list[SourceFilterSpec]:
    constants = _kotlin_string_constants(kotlin)
    declaration_pattern = re.compile(
        rf"\bclass\s+(?P<class>[A-Za-z_]\w*Filter)"
        rf"(?:\s*\([^)]*\))?\s*:\s*UriPartFilter\s*\(\s*"
        rf"(?P<key>{_KOTLIN_STRING})\s*,\s*(?P<title>{_KOTLIN_STRING})\s*,\s*"
        r"listOf\((?P<pairs>[\s\S]*?)\)\s*,?",
    )
    pair_pattern = re.compile(
        rf"(?P<title>{_KOTLIN_STRING})\s+to\s+"
        rf"(?P<value>{_KOTLIN_STRING}|[A-Za-z_]\w*)",
    )
    specs: list[SourceFilterSpec] = []
    for declaration in _kotlin_class_declarations(kotlin):
        found = declaration_pattern.search(declaration)
        if found is None:
            continue
        key = _decode_kotlin_string(found.group("key"))
        title = _decode_kotlin_string(found.group("title"))
        if key is None or title is None:
            continue
        options: list[SourceFilterOption] = []
        for pair in pair_pattern.finditer(found.group("pairs")):
            option_title = _decode_kotlin_string(pair.group("title"))
            option_value = _uri_part_filter_value(pair.group("value"), constants)
            if option_title is None or option_value is None:
                options = []
                break
            options.append(SourceFilterOption(title=option_title, value=option_value))
        if not options or len({item.value for item in options}) != len(options):
            continue
        class_name = found.group("class")
        # UriPartFilter is a Select wrapper even when a source names one SortFilter.
        # Its site values can encode rank modes that an Aidoku ascending toggle cannot preserve.
        kind = "select"
        specs.append(
            SourceFilterSpec(
                source_class=class_name,
                id=key,
                title=title,
                kind=kind,
                options=options,
                default_ascending=None,
            )
        )
    return specs


def _kotlin_simple_filter_specs(kotlin: str) -> list[SourceFilterSpec]:
    pattern = re.compile(
        rf"\bclass\s+(?P<class>[A-Za-z_]\w*)[^:{{]*:\s*"
        rf"Filter\.(?P<kind>CheckBox|Text)\s*\(\s*(?P<title>{_KOTLIN_STRING})"
    )
    specs: list[SourceFilterSpec] = []
    for declaration in _kotlin_class_declarations(kotlin):
        found = pattern.search(declaration)
        if found is None:
            continue
        title = _decode_kotlin_string(found.group("title"))
        if title is None:
            continue
        class_name = found.group("class")
        specs.append(
            SourceFilterSpec(
                source_class=class_name,
                id=_snake_case(class_name.removesuffix("Filter")),
                title=title,
                kind="check" if found.group("kind") == "CheckBox" else "text",
            )
        )
    return specs


def _balanced_content(
    text: str,
    opening: int,
    *,
    open_character: str = "(",
    close_character: str = ")",
) -> str | None:
    depth = 0
    quoted = False
    escaped = False
    for index in range(opening, len(text)):
        character = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
        elif character == open_character:
            depth += 1
        elif character == close_character:
            depth -= 1
            if depth == 0:
                return text[opening + 1 : index]
    return None


def _top_level_arguments(content: str) -> list[str]:
    arguments: list[str] = []
    start = 0
    round_depth = square_depth = brace_depth = 0
    quoted = False
    escaped = False
    for index, character in enumerate(content):
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
        elif character == "(":
            round_depth += 1
        elif character == ")":
            round_depth -= 1
        elif character == "[":
            square_depth += 1
        elif character == "]":
            square_depth -= 1
        elif character == "{":
            brace_depth += 1
        elif character == "}":
            brace_depth -= 1
        elif character == "," and not (round_depth or square_depth or brace_depth):
            arguments.append(content[start:index].strip())
            start = index + 1
    final = content[start:].strip()
    if final:
        arguments.append(final)
    return arguments


def _kotlin_constructor_filter_specs(kotlin: str) -> list[SourceFilterSpec]:
    """Recover filters whose labels and site values are supplied to custom constructors."""
    filter_list = re.search(r"\bgetFilterList\s*\([^)]*\)\s*=\s*FilterList\s*\(", kotlin)
    if filter_list is None:
        return []
    opening = filter_list.end() - 1
    body = _balanced_content(kotlin, opening)
    if body is None:
        return []

    declarations = {
        found.group(1): declaration
        for declaration in _kotlin_class_declarations(kotlin)
        if (found := re.search(r"\bclass\s+([A-Za-z_]\w*Filter)\b", declaration))
    }
    specs: list[SourceFilterSpec] = []
    for call in re.finditer(r"\b(?P<class>[A-Za-z_]\w*Filter)\s*\(", body):
        class_name = call.group("class")
        declaration = declarations.get(class_name, "")
        if "Filter.Select" not in declaration and "Filter.Sort" not in declaration:
            continue
        call_body = _balanced_content(body, call.end() - 1)
        if call_body is None:
            continue
        arguments = _top_level_arguments(call_body)
        if len(arguments) < 2:
            continue
        title = _decode_kotlin_string(arguments[0]) if arguments[0].startswith('"') else None
        array_match = re.match(r"arrayOf\s*\(", arguments[1])
        if title is None or array_match is None:
            continue
        array_body = _balanced_content(arguments[1], array_match.end() - 1)
        if array_body is None:
            continue
        options: list[SourceFilterOption] = []
        seen: set[str] = set()
        for expression in _top_level_arguments(array_body):
            constructor = re.match(r"(?:Pair|[A-Za-z_]\w*)\s*\(", expression)
            if constructor is None:
                options = []
                break
            option_body = _balanced_content(expression, constructor.end() - 1)
            if option_body is None:
                options = []
                break
            option_arguments = _top_level_arguments(option_body)
            if not all(argument.startswith('"') for argument in option_arguments):
                options = []
                break
            parts = [_decode_kotlin_string(argument) for argument in option_arguments]
            if len(parts) < 2 or any(part is None for part in parts):
                options = []
                break
            values = [part for part in parts if part is not None]
            if len(values) > 2 and any(":" in value for value in values[1:]):
                options = []
                break
            value = values[1] if len(values) == 2 else ":".join(values[1:])
            if value in seen:
                continue
            seen.add(value)
            options.append(SourceFilterOption(title=values[0], value=value))
        if options:
            specs.append(
                SourceFilterSpec(
                    source_class=class_name,
                    id=_snake_case(class_name.removesuffix("Filter")),
                    title=title,
                    kind="sort" if "Filter.Sort" in declaration else "select",
                    options=options,
                    default_ascending=False if "Filter.Sort" in declaration else None,
                )
            )
    return specs


def _kotlin_filter_specs(kotlin: str) -> list[SourceFilterSpec]:
    specs: list[SourceFilterSpec] = []
    pattern = re.compile(
        rf"\bclass\s+(?P<class>[A-Za-z_]\w*Filter)\s*:\s*"
        rf"Filter\.(?P<kind>Select(?:<[^>]+>)?|Sort)\s*\(\s*"
        rf"(?P<title>{_KOTLIN_STRING})\s*,\s*arrayOf\((?P<options>[\s\S]*?)\)",
    )
    state_values = re.compile(r"=\s*arrayOf\((?P<values>[\s\S]*?)\)\s*\[\s*state\s*\]")
    for declaration in _kotlin_class_declarations(kotlin):
        found = pattern.search(declaration)
        if found is None:
            continue
        title = _decode_kotlin_string(found.group("title"))
        titles = [
            value
            for literal in re.findall(_KOTLIN_STRING, found.group("options"))
            if (value := _decode_kotlin_string(literal)) is not None
        ]
        if title is None or not titles:
            continue
        assignments = list(state_values.finditer(declaration))
        values = (
            [
                value
                for expression in assignments[-1].group("values").split(",")
                if (value := _filter_value(expression)) is not None
            ]
            if assignments
            else list(titles)
        )
        if len(values) != len(titles) or len(set(values)) != len(values):
            continue
        class_name = found.group("class")
        kind = "sort" if found.group("kind") == "Sort" else "select"
        specs.append(
            SourceFilterSpec(
                source_class=class_name,
                id=_snake_case(class_name.removesuffix("Filter")),
                title=title,
                kind=kind,
                options=[
                    SourceFilterOption(title=option_title, value=value)
                    for option_title, value in zip(titles, values, strict=True)
                ],
                default_ascending=False if kind == "sort" else None,
            )
        )
    existing_ids = {spec.id for spec in specs}
    specs.extend(
        spec for spec in _kotlin_uri_part_filter_specs(kotlin) if spec.id not in existing_ids
    )
    existing_ids.update(spec.id for spec in specs)
    specs.extend(
        spec for spec in _kotlin_simple_filter_specs(kotlin) if spec.id not in existing_ids
    )
    existing_ids.update(spec.id for spec in specs)
    specs.extend(
        spec for spec in _kotlin_constructor_filter_specs(kotlin) if spec.id not in existing_ids
    )
    return specs


def _kotlin_class_declarations(text: str) -> list[str]:
    try:
        from tree_sitter_language_pack import get_parser

        parser = get_parser("kotlin")
        encoded = text.encode("utf-8")
        tree = parser.parse(encoded)
        declarations: list[str] = []
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type in {"class_declaration", "object_declaration"}:
                declarations.append(encoded[node.start_byte : node.end_byte].decode("utf-8"))
            stack.extend(reversed(node.children))
        return declarations
    except (ImportError, RuntimeError, AttributeError, TypeError):
        return re.findall(
            r"(?:abstract\s+)?class\s+\w+[\s\S]*?(?=\n(?:abstract\s+)?class\s+|\Z)",
            text,
        )


def _parse_main_class(kotlin: str) -> tuple[str, list[str]]:
    declarations = _kotlin_class_declarations(kotlin)
    candidate = next(
        (
            item
            for item in declarations
            if re.search(r"\bHttpSource\s*(?:\([^)]*\))?\s*(?:,|$)", item.split("{", 1)[0])
        ),
        "",
    )
    if not candidate:
        raise UnsupportedSourceError("MVP supports only standalone HttpSource modules")
    name = match(r"(?:abstract\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)", candidate)
    if not name:
        raise InputError("unable to identify the main HttpSource class")
    inheritance = match(
        rf"class\s+{re.escape(name)}\s*:\s*([\s\S]*?)\{{",
        candidate,
    )
    parents = []
    for item in inheritance.split(","):
        parent = re.sub(r"\([^)]*\)", "", item).strip()
        if parent:
            parents.append(parent.split("<", 1)[0].strip())
    allowed_parents = {"HttpSource", "ConfigurableSource"}
    if "HttpSource" not in parents or any(parent not in allowed_parents for parent in parents):
        raise UnsupportedSourceError(
            "MVP supports only standalone HttpSource modules without custom source bases"
        )
    return name, parents


def _parse_content_rating(build: str) -> ContentRating:
    if re.search(r"\bisNsfw\s*=\s*true\b", build, re.IGNORECASE):
        return ContentRating.NSFW
    warning = match(r"contentWarning\s*=\s*ContentWarning\.([A-Z_]+)", build, "SAFE")
    if warning in {"NSFW", "MATURE"}:
        return ContentRating.NSFW
    if warning in {"MIXED", "MATURE_MIXED"}:
        return ContentRating.MIXED
    return ContentRating.SAFE


def _uses_relative_url_keys(kotlin: str) -> bool:
    return "setUrlWithoutDomain" in kotlin or bool(
        re.search(r"\burl\s*=\s*\"/", kotlin)
        or re.search(r"\bval\s+url\s+get\(\)\s*=\s*\"/", kotlin)
    )


def _unsupported_features(
    build: str,
    kotlin: str,
    recognition: InputCapabilityRecognition,
) -> list[str]:
    checks = {
        "multisrc/theme source": ("themePkg", "SourceFactory", "MultiSource"),
        "coroutine KeiSource": ("KeiSource",),
        "login/authentication": ("LoginSource", "Authenticator", "WebView", "login("),
        "image decoding or scrambling": (
            "android.graphics",
            "Bitmap",
            "Unscrambler",
            "descramble",
            "scramble",
        ),
        "custom web or crypto processing": (
            "Mac.getInstance",
            "WebView",
            "loadUrl(",
            "decodeByteArray",
        ),
    }
    combined = build + "\n" + kotlin
    unsupported = [
        name for name, markers in checks.items() if any(marker in combined for marker in markers)
    ]
    if recognition.unsupported_crypto:
        unsupported.append("cryptography")
    return unsupported


def analyze_kotlin_source(resolved: ResolvedSource) -> SourceIR:
    files = collect_source_files(resolved)
    build_file = next(
        (item for item in files if item.path in {"build.gradle.kts", "build.gradle"}),
        None,
    )
    if build_file is None:
        raise InputError("build.gradle.kts or build.gradle was not collected")
    kotlin = "\n\n".join(item.content for item in files if item.path.endswith(".kt"))
    main_class, parents = _parse_main_class(kotlin)
    recognition = recognize_input_capabilities(kotlin, dialect="kotlin")
    unsupported = _unsupported_features(build_file.content, kotlin, recognition)
    if unsupported:
        raise UnsupportedSourceError("source is outside the MVP scope: " + ", ".join(unsupported))

    module_slug = re.sub(r"[^a-z0-9]+", "-", resolved.module_path.name.lower()).strip("-")
    language = match(r"\blang\s*=\s*['\"]([^'\"]+)['\"]", build_file.content)
    if not language:
        language = match(r"override\s+val\s+lang\s*=\s*\"([^\"]+)\"", kotlin, "multi")
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]+)*", language):
        raise InputError(f"unsupported or invalid source language code: {language}")
    source_block = match(r"\bsource\s*\{([\s\S]*?)\}", build_file.content)
    name = match(r"\bname\s*=\s*['\"]([^'\"]+)['\"]", source_block)
    if not name:
        name = match(r"\bname\s*=\s*['\"]([^'\"]+)['\"]", build_file.content)
    if not name:
        name = match(r"\bextName\s*=\s*['\"]([^'\"]+)['\"]", build_file.content)
    if not name:
        name = match(r"override\s+val\s+name\s*=\s*\"([^\"]+)\"", kotlin, main_class)
    base_url = match(r"\bbaseUrl\s*=\s*\"([^\"]+)\"", build_file.content)
    if not base_url:
        base_url = match(r"\bcustom\(\s*\"([^\"]+)\"", build_file.content)
    if not base_url:
        base_url = match(r"override\s+val\s+baseUrl\s*=\s*\"([^\"]+)\"", kotlin)
    if not base_url:
        raise InputError("unable to extract a source base URL")
    version_text = match(r"\b(?:extVersionCode|versionCode)\s*=\s*(\d+)", build_file.content, "1")

    method_names = sorted(set(re.findall(r"override\s+(?:suspend\s+)?fun\s+(\w+)", kotlin)))
    header_names_set = set(re.findall(r"(?:\.|\b)(?:add|set)\(\s*\"([^\"]+)\"\s*,", kotlin))
    if "super.headersBuilder()" in kotlin:
        header_names_set.add("User-Agent")
    header_names = sorted(header_names_set)
    warnings: list[str] = []
    if "addInterceptor" in kotlin or "addNetworkInterceptor" in kotlin:
        warnings.append(
            "source uses OkHttp interceptors; generated behavior requires manual review"
        )

    license_name, license_text = input_license(resolved)
    metadata = SourceMetadata(
        source_id=f"{language}.{module_slug}",
        package_name=module_slug or main_class.lower(),
        name=name,
        language=language,
        base_url=base_url.rstrip("/"),
        version=max(1, int(version_text)),
        content_rating=_parse_content_rating(build_file.content),
    )
    return SourceIR(
        input_ref=resolved.input_ref,
        commit=resolved.commit,
        metadata=metadata,
        main_class=main_class,
        parent_classes=parents,
        capabilities=list(recognition.capabilities),
        method_names=method_names,
        header_names=header_names,
        filter_specs=_kotlin_filter_specs(kotlin),
        relative_url_keys=_uses_relative_url_keys(kotlin),
        files=files,
        license_name=license_name,
        license_text=license_text,
        warnings=warnings,
    )
