from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .constants import AIDOKU_RUNTIME_MANAGED_REQUEST_HEADERS, MAX_AI_DIAGNOSTIC_CHARS
from .decompiled_input import (
    DecompiledDtoField,
    DecompiledDtoShape,
    decompiled_detail_uses_api_envelope,
    decompiled_dto_shapes,
    decompiled_rank_list_wraps_comic,
)
from .dependency_policy import evaluate_dependency_policy
from .models import Capability, GeneratedResources, GenerationManifest, SourceIR
from .rust_inspection import RustInspection

RepairKind = Literal[
    "retry",
    "chapter_regex",
    "dto_shape",
    "image_resolution",
    "transport_header",
]


@dataclass(frozen=True)
class ContractDiagnostic:
    message: str
    repair_kind: RepairKind | None = None


@dataclass(frozen=True)
class ContractRepair:
    excerpts: list[dict[str, object]]
    diagnostics: str


@dataclass(frozen=True)
class ContractEvaluation:
    diagnostics: tuple[ContractDiagnostic, ...]

    @property
    def messages(self) -> list[str]:
        return [diagnostic.message for diagnostic in self.diagnostics]

    @property
    def is_fully_targeted_repair(self) -> bool:
        return bool(self.diagnostics) and all(
            diagnostic.repair_kind is not None for diagnostic in self.diagnostics
        )

    def repair(self, project: Path) -> ContractRepair | None:
        if not self.is_fully_targeted_repair:
            return None
        kinds = {
            diagnostic.repair_kind
            for diagnostic in self.diagnostics
            if diagnostic.repair_kind is not None
        }
        excerpts = _repair_excerpts(project, kinds, self.messages)
        if not excerpts:
            return None
        return ContractRepair(
            excerpts=excerpts,
            diagnostics="\n".join(self.messages)[-MAX_AI_DIAGNOSTIC_CHARS:],
        )


def evaluate_manifest_contract(
    ir: SourceIR,
    manifest: GenerationManifest,
) -> ContractEvaluation:
    resources = GeneratedResources(manifest)
    traits = set(manifest.implemented_traits)
    dependencies = {item.name for item in manifest.dependencies}
    dependency_evaluation = evaluate_dependency_policy(
        dependencies,
        capabilities=ir.capabilities,
    )
    rust_files = [item for item in manifest.files if item.path.endswith(".rs")]
    rust_content = "\n".join(item.content for item in rust_files)
    rust = RustInspection(item.content for item in rust_files)
    input_content = "\n".join(item.content for item in ir.files)
    diagnostics: list[ContractDiagnostic] = []

    def add(message: str, repair_kind: RepairKind | None = None) -> None:
        diagnostics.append(ContractDiagnostic(message, repair_kind))

    for message in _decompiled_dto_shape_gaps(ir, rust):
        add(message, "dto_shape")

    if (Capability.POPULAR in ir.capabilities or Capability.LATEST in ir.capabilities) and (
        "ListingProvider" not in traits
    ):
        add("source declares popular/latest listings but generated no ListingProvider")
    dynamic_filters_cover_all = (
        Capability.DYNAMIC_FILTERS in ir.capabilities
        and "DynamicFilters" in traits
        and not ir.filter_specs
    )
    if Capability.FILTERS in ir.capabilities and not dynamic_filters_cover_all:
        if not resources.has(GeneratedResources.FILTERS):
            add("source declares filters but generated no res/filters.json")
        elif resources.is_empty(GeneratedResources.FILTERS):
            add("source declares filters but generated an empty res/filters.json")
        for message in resources.filter_contract_gaps(
            ir.filter_specs,
            has_sort_mapping="FilterValue::Sort" in rust_content,
        ):
            add(message)
    if Capability.DYNAMIC_FILTERS in ir.capabilities and "DynamicFilters" not in traits:
        add("source fetches dynamic filters but generated no DynamicFilters provider")
    dynamic_filter_deserialization = any(
        "serde_json::from_str" in function.text
        and (
            re.search(
                r"(?:let\s+[A-Za-z_]\w*\s*:\s*|from_str\s*::\s*<)"
                r"(?:Vec\s*<\s*)?(?:aidoku::)?Filter\b",
                function.text,
            )
            or re.search(
                r"->\s*Result\s*<\s*Vec\s*<\s*(?:aidoku::)?Filter\s*>\s*>\s*"
                r"\{\s*serde_json::from_str",
                function.text,
            )
        )
        for function in rust.named("get_dynamic_filters")
    )
    if Capability.DYNAMIC_FILTERS in ir.capabilities and dynamic_filter_deserialization:
        add(
            "get_dynamic_filters attempts to deserialize aidoku::Filter with serde_json; "
            "construct typed SelectFilter/Filter values directly because Filter is not "
            "Deserialize"
        )
    if Capability.DYNAMIC_FILTERS in ir.capabilities and rust.function_contains(
        "get_dynamic_filters", "listing_query"
    ):
        add(
            "get_dynamic_filters performs a full manga listing request; use a dedicated "
            "options-only request (for GraphQL, query only the recovered option field) "
            "instead of downloading manga entries"
        )
    if (
        Capability.JSON_API in ir.capabilities
        and rust.function_contains("listing_query", "description")
        and rust.function_contains("listing_query", "allCategory")
    ):
        add(
            "GraphQL listing requests include detail-only fields and dynamic-filter metadata; "
            "keep the source page size but omit description from list projections and fetch "
            "allCategory only in the dedicated dynamic-filter request"
        )
    if Capability.DYNAMIC_FILTERS in ir.capabilities:
        for filter_id in sorted(_dynamic_filter_ids_missing_from_query_mapping(rust)):
            add(
                f"dynamic filter {filter_id!r} is never read by get_search_manga_list; "
                "read its FilterValue using the same id and send the selected site value "
                "in the list/search request"
            )
    if Capability.SETTINGS in ir.capabilities:
        if not resources.has(GeneratedResources.SETTINGS):
            add("source declares settings but generated no res/settings.json")
        elif resources.is_empty(GeneratedResources.SETTINGS):
            add("source declares settings but generated an empty res/settings.json")
        else:
            unread_settings = _settings_not_read(resources, rust_content)
            if unread_settings:
                add(
                    "generated Rust does not consume source settings with defaults_get: "
                    + ", ".join(unread_settings)
                    + "; read every setting key and apply its recovered behavior"
                )
    if Capability.IMAGE_HEADERS in ir.capabilities and "ImageRequestProvider" not in traits:
        add("source declares image headers but generated no ImageRequestProvider")
    missing_shared_headers = (
        [
            name
            for name, value in ir.shared_request_headers.items()
            if json.dumps(name) not in rust_content or json.dumps(value) not in rust_content
        ]
        if "Request::" in rust_content
        else []
    )
    if missing_shared_headers:
        add("generated requests omit source-wide headers: " + ", ".join(missing_shared_headers))
    manual_runtime_headers = sorted(
        name
        for name in AIDOKU_RUNTIME_MANAGED_REQUEST_HEADERS
        if any(rust.function_has_header(function.name, name) for function in rust.functions)
    )
    if manual_runtime_headers:
        display = ", ".join(name.title() for name in manual_runtime_headers)
        add(
            f"generated requests set {display} manually; omit it because the Aidoku runtime "
            "owns response decompression and may otherwise expose compressed bytes to "
            "HTML/JSON parsers",
            "transport_header",
        )
    if (
        Capability.IMAGE_HEADERS in ir.capabilities
        and rust.function_contains("get_image_request", "context")
        and rust.function_contains("get_image_request", "referer")
        and "PageContent::url_context" not in rust_content
    ):
        add(
            "get_image_request reads a Referer from PageContext but generated pages never use "
            "PageContent::url_context; attach the exact chapter/page Referer to every page URL "
            "and use the site base URL as the cover-image fallback"
        )
    source_uses_cookie_jar = any(
        marker in input_content
        for marker in ("cookieJar.loadForRequest", "cookieJar.saveFromResponse")
    )
    optional_cookie_refresh = re.search(
        r"cookieJar\.loadForRequest[^\n]{0,300}\?:\s*return\b",
        input_content,
    )
    if source_uses_cookie_jar and optional_cookie_refresh is None:
        has_cookie_setting = resources.contains_text(GeneratedResources.SETTINGS, "cookie")
        has_api_cookie_header = rust.function_has_header("post_query", "cookie")
        has_image_cookie_header = rust.function_has_header("get_image_request", "cookie")
        if not has_cookie_setting or not has_api_cookie_header or not has_image_cookie_header:
            add(
                "input public requests depend on a Cookie session, but the generated source "
                "does not expose an optional cookie setting and apply its Cookie header to both "
                "API and image requests"
            )
    if Capability.DEEP_LINKS in ir.capabilities and "DeepLinkHandler" not in traits:
        add("source declares deep links but generated no DeepLinkHandler")
    for message in dependency_evaluation.diagnostics:
        add(message)
    update_uses_combined_helper = (
        rust.function_contains("get_manga_update", "needs_details")
        and rust.function_contains("get_manga_update", "needs_chapters")
        and rust.function_contains("get_manga_update", "manga_query")
    )
    helper_selects_requested_projection = rust.function_contains(
        "manga_query", "(true, false)"
    ) and rust.function_contains("manga_query", "(false, true)")
    if (
        Capability.DETAILS in ir.capabilities
        and Capability.CHAPTERS in ir.capabilities
        and update_uses_combined_helper
        and not helper_selects_requested_projection
        and "comicById" in rust_content
        and "chaptersByComicId" in rust_content
    ):
        add(
            "get_manga_update unconditionally fetches a combined details-and-chapters GraphQL "
            "query; choose details-only, chapters-only, or combined queries so each call fetches "
            "only the data requested by needs_details and needs_chapters"
        )
    if Capability.DETAILS in ir.capabilities and Capability.CHAPTERS in ir.capabilities:
        repeated_detail_routes: set[str] = set()
        if rust.function_contains("get_manga_update", "needs_details") and rust.function_contains(
            "get_manga_update", "needs_chapters"
        ):
            update_routes = rust.request_route_literals("get_manga_update")
            chapter_helpers = {
                name for name in rust.calls("get_manga_update") if "chapter" in name.lower()
            }
            for helper in chapter_helpers:
                helper_routes = rust.request_route_literals(helper)
                repeated_detail_routes.update(update_routes & helper_routes)
        if repeated_detail_routes:
            add(
                "get_manga_update and its chapter helper fetch the same REST detail route twice "
                "when details and chapters are both requested; fetch it once and pass the "
                "decoded detail response into the chapter helper: "
                + ", ".join(sorted(repeated_detail_routes))
            )
    if (
        ir.source_format == "decompiled_apk"
        and Capability.JSON_API in ir.capabilities
        and not _has_idempotent_get_retry(rust)
    ):
        add(
            "decompiled Tachi JSON source generated no centralized one-retry helper for "
            "transient idempotent GET RequestError; reconstruct and resend the same request "
            "once, then deserialize only the successful response",
            "retry",
        )
    if (
        ir.source_format == "kotlin_module"
        and "HttpSource" in ir.parent_classes
        and _has_get_send_path(rust)
        and not _has_idempotent_get_retry(rust)
    ):
        add(
            "standard Kotlin HttpSource generated no centralized one-retry helper for "
            "transient idempotent GET RequestError; reconstruct and resend the same request "
            "once, then parse only the successful response",
            "retry",
        )
    if Capability.CHAPTERS in ir.capabilities and _rust_chapter_parser_compiles_regex(rust):
        add(
            "generated code compiles Regex::new on every chapter parse; for fixed embedded-JSON "
            "delimiters or numeric chapter labels, use bounded string scanning so each update "
            "does not compile a regex and pull regex runtime cost into the WASM hot path",
            "chapter_regex",
        )
    if (
        Capability.CONTEXTUAL_CHAPTER_URLS in ir.capabilities
        and not _preserves_contextual_chapter_urls(rust_content)
    ):
        add(
            "source resolves placeholder chapter URLs from adjacent chapter context, but the "
            "generated Rust does not preserve the placeholder chapters and complete the "
            "prev/next response lookup, numeric fallback, and terminal '_2.' path rewrite"
        )
    if Capability.TRIPLE_DES_CBC in ir.capabilities and (
        "current_date" not in rust_content or re.search(r"\blet\s+time\s*=\s*\"0\"", rust_content)
    ):
        add(
            "3DES-CBC request signing uses no live millisecond Unix timestamp; "
            "call aidoku::imports::std::current_date() and multiply its seconds by 1000"
        )
    if Capability.DYNAMIC_BASE_URLS in ir.capabilities:
        if not resources.has(GeneratedResources.SETTINGS):
            add("dynamic base URL source generated no res/settings.json")
        if "defaults_get" not in rust_content:
            add("dynamic base URL source generated no validated defaults_get resolver")
    if ir.relative_url_keys:
        if not rust.has_function("absolute_url"):
            add("source emits relative manga/chapter keys but generated no absolute_url helper")
        if _passes_relative_key_to_request(rust):
            add("generated code passes Manga.key or Chapter.key to a request without absolute_url")
    for route in ir.chapter_page_routes:
        default_variant = next(variant for variant in route.variants if variant.is_default)
        if default_variant.strip_prefix and default_variant.strip_prefix not in rust_content:
            add(
                "chapter page route omits required key prefix removal: "
                + repr(default_variant.strip_prefix)
            )
        for replacement in default_variant.replacements:
            if replacement.new not in rust_content:
                add(
                    "chapter page route omits required default replacement "
                    f"{replacement.old!r} -> {replacement.new!r} for "
                    f"{route.endpoint_template!r}"
                )
    image_policy = ir.image_url_policy
    if image_policy is not None:
        if image_policy.preserve_cover_urls and re.search(
            r"\bcover\s*:[\s\S]{0,500}?\b(?:image_)?resolution\s*\(",
            rust_content,
        ):
            add(
                "source image policy requires preserving each cover URL exactly; "
                "generated code applies a chapter-resolution transform to a cover URL",
                "image_resolution",
            )
        if image_policy.chapter_resolution_regex and not _has_terminal_image_suffix_scope(
            rust_content
        ):
            add(
                "chapter image resolution translation lacks the recovered exact x.jpg/x.webp "
                f"suffix scope {image_policy.chapter_resolution_regex!r}",
                "image_resolution",
            )
    if "date_upload" in input_content and "date_uploaded" not in rust_content:
        add("input chapters expose date_upload but generated chapters omit date_uploaded")
    if "scanlator" in input_content and "scanlators" not in rust_content:
        add("input chapters expose scanlator but generated chapters omit scanlators")
    if decompiled_rank_list_wraps_comic(ir.files) and _rank_helper_skips_item_wrapper(rust):
        add(
            "rank endpoint returns RankResult.list entries wrapping the manga in ListItem.comic; "
            "generated code incorrectly deserializes ranks as a direct ListResult<Comic>, "
            "producing empty manga titles and keys"
        )
    if decompiled_detail_uses_api_envelope(ir.files) and _detail_helper_skips_api_envelope(rust):
        add(
            "detail endpoint is wrapped in ApiResponse<ComicDetailResult>, but generated detail "
            "helper deserializes the HTTP response directly into DetailResult; deserialize "
            "ApiResponse<DetailResult> and return response.results"
        )
    if re.search(r"\bSChapter\b[\s\S]{0,2000}?\burl\s*=", input_content) and not re.search(
        r"\bChapter\s*\{[\s\S]{0,4000}?\burl\s*:", rust_content
    ):
        add("input chapters expose a URL but generated Chapter values omit url")
    legacy_settings = {value for value in ("zh-hant", "zh-hans") if f'"{value}"' in input_content}
    missing_legacy_settings = sorted(
        value for value in legacy_settings if f'"{value}"' not in rust_content
    )
    if missing_legacy_settings:
        add(
            "generated settings logic omits legacy input values: "
            + ", ".join(missing_legacy_settings)
        )
    return ContractEvaluation(tuple(diagnostics))


def _settings_not_read(resources: GeneratedResources, rust_content: str) -> list[str]:
    keys = resources.setting_keys()
    if not keys:
        return []
    constants = {
        match.group("name"): json.loads(match.group("value"))
        for match in re.finditer(
            r"\b(?:const|static)\s+(?P<name>[A-Za-z_]\w*)\s*"
            r"(?::\s*[^=;]+)?=\s*(?P<value>\"(?:\\.|[^\"\\])*\")\s*;",
            rust_content,
        )
    }
    arguments = re.findall(
        r"(?:aidoku::imports::defaults::)?defaults_get\s*"
        r"(?:::\s*<[^;{}()]+>)?\s*\(\s*"
        r"(?P<argument>\"(?:\\.|[^\"\\])*\"|[A-Za-z_]\w*)\s*\)",
        rust_content,
    )
    consumed: set[str] = set()
    for argument in arguments:
        if argument.startswith('"'):
            consumed.add(json.loads(argument))
        elif argument in constants:
            consumed.add(constants[argument])
    return [key for key in keys if key not in consumed]


def _preserves_contextual_chapter_urls(rust_content: str) -> bool:
    has_placeholder = "javascript:cid(1)" in rust_content
    has_directions = all(
        any(marker in rust_content for marker in (f'"#{direction}"', f'"{direction}"'))
        for direction in ("prev", "next")
    )
    has_response_routes = all(marker in rust_content for marker in ("url_previous", "url_next"))
    has_fallback_route = "/read/" in rust_content and ".html" in rust_content
    has_terminal_rewrite = "_2." in rust_content or (
        '"_2"' in rust_content and "rfind('.')" in rust_content
    )
    return all(
        (
            has_placeholder,
            has_directions,
            has_response_routes,
            has_fallback_route,
            has_terminal_rewrite,
        )
    )


def normalize_decompiled_dto_manifest(
    ir: SourceIR,
    manifest: GenerationManifest,
) -> GenerationManifest:
    if ir.source_format != "decompiled_apk":
        return manifest
    shapes = decompiled_dto_shapes(ir.files)
    files = []
    changed = False
    for generated in manifest.files:
        content = generated.content
        if generated.path.endswith(".rs"):
            content = _normalize_decompiled_dto_content(content, shapes)
        changed |= content != generated.content
        files.append(generated.model_copy(update={"content": content}))
    return manifest.model_copy(update={"files": files}) if changed else manifest


def normalize_decompiled_setting_manifest(
    ir: SourceIR,
    manifest: GenerationManifest,
) -> GenerationManifest:
    if ir.source_format != "decompiled_apk":
        return manifest
    enum_values: dict[str, dict[str, str]] = {}
    for source in ir.files:
        key_match = re.search(
            r'public\s+static\s+final\s+String\s+KEY\s*=\s*"([^"]+)"', source.content
        )
        enum_match = re.search(
            r"public\s+enum\s+[A-Za-z_]\w*\s*\{(?P<body>[\s\S]{1,12000}?)\s*;",
            source.content,
        )
        if key_match is None or enum_match is None:
            continue
        values = {
            found.group("name"): found.group("value")
            for found in re.finditer(
                r'^\s*(?P<name>[A-Z][A-Z0-9_]*)\(\s*"(?:\\.|[^"\\])*"\s*,\s*'
                r'"(?P<value>(?:\\.|[^"\\])*)"',
                enum_match.group("body"),
                re.MULTILINE,
            )
        }
        if values:
            enum_values[key_match.group(1)] = values
    if not enum_values:
        return manifest

    files = []
    changed = False
    for generated in manifest.files:
        content = generated.content
        if generated.path == GeneratedResources.SETTINGS:
            data = json.loads(content)
            for group in data:
                if not isinstance(group, dict) or not isinstance(group.get("items"), list):
                    continue
                for item in group["items"]:
                    if not isinstance(item, dict):
                        continue
                    mapping = enum_values.get(item.get("key"))
                    values = item.get("values")
                    if (
                        mapping
                        and isinstance(values, list)
                        and values
                        and all(isinstance(value, str) and value in mapping for value in values)
                    ):
                        item["values"] = [mapping[value] for value in values]
                        default = item.get("default")
                        if isinstance(default, str) and default in mapping:
                            item["default"] = mapping[default]
            content = json.dumps(data, ensure_ascii=False, indent="\t") + "\n"
        changed |= content != generated.content
        files.append(generated.model_copy(update={"content": content}))
    return manifest.model_copy(update={"files": files}) if changed else manifest


def _normalize_decompiled_dto_content(
    content: str,
    shapes: tuple[DecompiledDtoShape, ...],
) -> str:
    rust = RustInspection.from_content(content)
    edits: list[tuple[int, int, bytes]] = []
    for shape in shapes:
        for field in shape.fields:
            rust_field = _matching_rust_field(rust, shape.name, field)
            if rust_field is None:
                continue
            rename_attribute = next(
                (
                    attribute
                    for attribute in rust_field.attributes
                    if re.search(
                        r'\brename\s*=\s*"[^"\\]+"',
                        attribute.text.decode("utf-8", errors="replace"),
                    )
                ),
                None,
            )
            struct_node = rust_field.node.parent
            while struct_node is not None and struct_node.type != "struct_item":
                struct_node = struct_node.parent
            struct_rename_all = False
            if struct_node is not None:
                sibling = struct_node.prev_named_sibling
                while sibling is not None and sibling.type == "attribute_item":
                    if "rename_all" in sibling.text.decode("utf-8", errors="replace"):
                        struct_rename_all = True
                        break
                    sibling = sibling.prev_named_sibling
            if rust_field.serialized_name == field.serialized_name and not (
                rename_attribute is None and struct_rename_all and "_" in field.serialized_name
            ):
                continue
            if rename_attribute is not None:
                original = rename_attribute.text.decode("utf-8", errors="replace")
                replacement = re.sub(
                    r'(\brename\s*=\s*")[^"\\]+(")',
                    rf"\g<1>{field.serialized_name}\g<2>",
                    original,
                    count=1,
                ).encode()
                edits.append((rename_attribute.start_byte, rename_attribute.end_byte, replacement))
            else:
                indent = b" " * rust_field.node.start_point[1]
                insertion = f'#[serde(rename = "{field.serialized_name}")]\n'.encode() + indent
                edits.append((rust_field.node.start_byte, rust_field.node.start_byte, insertion))
    if not edits:
        return content
    raw = bytearray(content.encode())
    for start, end, replacement in sorted(edits, reverse=True):
        raw[start:end] = replacement
    return raw.decode()


def _repair_excerpts(
    project: Path,
    repair_kinds: set[RepairKind],
    diagnostics: list[str],
) -> list[dict[str, object]]:
    excerpts: list[dict[str, object]] = []
    dto_names = {
        match.group(1)
        for diagnostic in diagnostics
        if (match := re.match(r"decompiled DTO ([A-Za-z_][A-Za-z0-9_]*)\.", diagnostic))
    }
    source_root = project / "src"
    for path in sorted(source_root.rglob("*.rs")):
        if path.name == "generated_smoke.rs" or path.is_symlink():
            continue
        content = path.read_text(encoding="utf-8")
        relative = path.relative_to(project).as_posix()
        inspection = RustInspection.from_content(content)
        if "dto_shape" in repair_kinds:
            for struct in inspection.structs:
                if struct.name not in dto_names:
                    continue
                excerpts.append(
                    {
                        "path": relative,
                        "start_line": struct.node.start_point[0] + 1,
                        "end_line": struct.node.end_point[0] + 1,
                        "content": struct.text,
                    }
                )
        for function in inspection.functions:
            include = (
                ("retry" in repair_kinds and ".send()" in function.text)
                or (
                    "chapter_regex" in repair_kinds
                    and "chapter" in function.name.lower()
                    and "Regex::new" in function.text
                )
                or ("image_resolution" in repair_kinds and "resolution" in function.name.lower())
                or (
                    "transport_header" in repair_kinds
                    and any(
                        inspection.function_has_header(function.name, name)
                        for name in AIDOKU_RUNTIME_MANAGED_REQUEST_HEADERS
                    )
                )
            )
            if not include:
                continue
            excerpts.append(
                {
                    "path": relative,
                    "start_line": function.node.start_point[0] + 1,
                    "end_line": function.node.end_point[0] + 1,
                    "content": function.text,
                }
            )
    return excerpts[:8]


def _generic_type(type_text: str) -> tuple[str, tuple[str, ...]]:
    value = type_text.strip()
    opening = value.find("<")
    if opening < 0 or not value.endswith(">"):
        return value.rsplit("::", 1)[-1].rsplit(".", 1)[-1], ()
    base = value[:opening].strip().rsplit("::", 1)[-1].rsplit(".", 1)[-1]
    body = value[opening + 1 : -1]
    arguments: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(body):
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
        elif char == "," and depth == 0:
            arguments.append(body[start:index].strip())
            start = index + 1
    arguments.append(body[start:].strip())
    return base, tuple(arguments)


def _simple_type(type_text: str) -> str:
    base, arguments = _generic_type(type_text)
    if base in {"Option", "Box", "Cow"} and arguments:
        return _simple_type(arguments[-1])
    return base


def _unwrapped_generic(type_text: str) -> tuple[str, tuple[str, ...]]:
    base, arguments = _generic_type(type_text)
    while base in {"Option", "Box"} and len(arguments) == 1:
        base, arguments = _generic_type(arguments[0])
    return base, arguments


def _rust_field_names(field: DecompiledDtoField) -> tuple[str, ...]:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", field.name).lower()
    return tuple(
        dict.fromkeys(
            (
                field.name,
                snake,
                field.serialized_name,
                f"{field.name}_",
                f"{snake}_",
                f"r#{field.name}",
            )
        )
    )


def _matching_rust_field(
    rust: RustInspection,
    owner: str,
    field: DecompiledDtoField,
):
    return next(
        (
            value
            for name in _rust_field_names(field)
            if (value := rust.struct_field(owner, name)) is not None
        ),
        None,
    )


def _decompiled_dto_shape_gaps(ir: SourceIR, rust: RustInspection) -> list[str]:
    if ir.source_format != "decompiled_apk":
        return []
    shapes = decompiled_dto_shapes(ir.files)
    dto_names = {shape.name for shape in shapes}
    gaps: list[str] = []
    for shape in shapes:
        if rust.struct_named(shape.name) is None:
            continue
        for field in shape.fields:
            rust_field = _matching_rust_field(rust, shape.name, field)
            if rust_field is None:
                continue
            if rust_field.serialized_name != field.serialized_name:
                gaps.append(
                    f"decompiled DTO {shape.name}.{field.name} has serialized name "
                    f"{field.serialized_name!r}, but generated Rust field "
                    f"{rust_field.name} uses {rust_field.serialized_name!r}; preserve the "
                    "recovered JSON field name"
                )
            java_base, java_arguments = _generic_type(field.java_type)
            if java_base != "Map" or len(java_arguments) != 2:
                continue
            expected_value = _simple_type(java_arguments[1])
            expected_key = _simple_type(java_arguments[0])
            if expected_value not in dto_names:
                continue
            rust_type = rust_field.type_text
            rust_base, rust_arguments = _unwrapped_generic(rust_type)
            actual_key = (
                _simple_type(rust_arguments[0])
                if rust_base in {"BTreeMap", "HashMap", "Map"} and len(rust_arguments) == 2
                else ""
            )
            actual_value = (
                _simple_type(rust_arguments[1])
                if rust_base in {"BTreeMap", "HashMap", "Map"} and len(rust_arguments) == 2
                else _simple_type(rust_type)
            )
            key_matches = expected_key != "String" or actual_key == expected_key
            if (
                rust_base in {"BTreeMap", "HashMap", "Map"}
                and key_matches
                and actual_value == expected_value
            ):
                continue
            gaps.append(
                f"decompiled DTO {shape.name}.{field.serialized_name} is "
                f"{field.java_type}, but the generated Rust field is {rust_type}; preserve "
                "the recovered map key/value DTO types so detail JSON can deserialize"
            )
    return gaps


def _dynamic_filter_ids_missing_from_query_mapping(rust: RustInspection) -> set[str]:
    dynamic_texts = [function.text for function in rust.named("get_dynamic_filters")]
    dynamic_ids = {
        match.group(1)
        for text in dynamic_texts
        for match in re.finditer(
            r'\bSelectFilter\s*\{[\s\S]{0,1200}?\bid\s*:\s*"([^"\\]+)"',
            text,
        )
    }
    reachable = rust.reachable_functions("get_search_manga_list")
    query_mapping = "\n".join(function.text for name in reachable for function in rust.named(name))
    return {filter_id for filter_id in dynamic_ids if f'"{filter_id}"' not in query_mapping}


def _detail_helper_skips_api_envelope(rust: RustInspection) -> bool:
    for function in rust.functions:
        if "detail" not in function.name.lower():
            continue
        signature = function.text.split("{", 1)[0]
        reachable = rust.reachable_functions(function.name)
        reachable_text = "\n".join(item.text for name in reachable for item in rust.named(name))
        if (
            re.search(r"Result<(?:Comic)?DetailResult>", signature)
            and ".get_json_owned()" in reachable_text
            and "ApiResponse<" not in reachable_text
        ):
            return True
    return False


def _rank_helper_skips_item_wrapper(rust: RustInspection) -> bool:
    text = "\n".join(function.text for function in rust.named("get_search_manga_list"))
    if "/ranks" not in text:
        return False
    if re.search(
        r"contains\(\s*\"/ranks\"\s*\)[\s\S]{0,1800}?"
        r"ApiResponse\s*<\s*C2aRankResult\s*>"
        r"[\s\S]{0,1800}?\.comic\b",
        text,
    ):
        return False
    for response in re.finditer(
        r"ApiResponse\s*<\s*(?P<inner>[A-Za-z_]\w*"
        r"(?:\s*<\s*[A-Za-z_]\w*\s*>)?)\s*>",
        text,
    ):
        inner = response.group("inner")
        if inner == "ListResult" or re.fullmatch(r"[A-Za-z_]\w*\s*<\s*Comic\s*>", inner):
            return True
        if rust.struct_field_type(inner, "list") == "Vec<Comic>":
            return True
    return False


def _has_idempotent_get_retry(rust: RustInspection) -> bool:
    for function in rust.functions:
        compact = RustInspection.compact_node(function.node)
        if compact.count(".send()") >= 2 and (
            "match" in compact or "or_else" in compact or "ifletErr" in compact
        ):
            return True
    return False


def _has_get_send_path(rust: RustInspection) -> bool:
    get_paths = {function.name for function in rust.functions if "Request::get" in function.text}
    changed = True
    while changed:
        changed = False
        for function in rust.functions:
            if function.name not in get_paths and function.calls & get_paths:
                get_paths.add(function.name)
                changed = True
    return any(
        ".send()" in function.text for function in rust.functions if function.name in get_paths
    )


def _rust_chapter_parser_compiles_regex(rust: RustInspection) -> bool:
    return any(
        "chapter" in function.name.lower() and "Regex::new" in function.text
        for function in rust.functions
    )


def _has_terminal_image_suffix_scope(content: str) -> bool:
    if r"\d+(?=x\.(?:jpg|webp)$)" in content:
        return True

    def scoped(extension: str) -> bool:
        direct = re.search(
            rf'(?:ends_with|strip_suffix)\(\s*"x?\.{extension}"',
            content,
        )
        if direct:
            return True
        lengths = (len(f".{extension}"), len(f"x.{extension}"))
        return any(
            re.search(
                rf'rfind\(\s*"{prefix}\.{extension}"\s*\)'
                rf"[\s\S]{{0,160}}?\+\s*{length}\s*==\s*"
                r"[A-Za-z_][A-Za-z0-9_]*\.len\(\)",
                content,
            )
            for prefix, length in zip(("", "x"), lengths, strict=True)
        )

    return scoped("jpg") and scoped("webp")


def _passes_relative_key_to_request(rust: RustInspection) -> bool:
    for node in rust.nodes("call_expression"):
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if function is None or arguments is None:
            continue
        called = RustInspection.compact_node(function)
        if not (
            called in {"Request::get", "Request::post", "request"} or called.endswith(".request")
        ):
            continue
        values = RustInspection.compact_node(arguments)
        has_relative_key = "manga.key" in values or "chapter.key" in values
        if has_relative_key and "absolute_url" not in values:
            return True
    return False
