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
            "MessageDigest",
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
    header_names_set = set(re.findall(r"\.(?:add|set)\(\s*\"([^\"]+)\"\s*,", kotlin))
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
