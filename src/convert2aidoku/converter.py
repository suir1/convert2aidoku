from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

from .ai import AIResult, OpenAICompatibleClient, ai_round
from .analyzer import analyze_source
from .config import AISettings
from .constants import MAX_AI_DIAGNOSTIC_CHARS
from .errors import AIProviderError, InputError, SecurityError
from .ingest import resolve_source
from .models import (
    Capability,
    ConversionCheckpoint,
    ConversionReport,
    GeneratedFile,
    GenerationManifest,
    RepairPatch,
    SourceIR,
    ValidationResult,
)
from .reports import classify_status, write_report
from .scaffold import (
    apply_generation_manifest,
    create_scaffold,
    normalize_pinned_aidoku_rust,
    read_generated_files,
    validate_generated_content,
)
from .templates import match_templates
from .validator import validate_project


@dataclass(frozen=True)
class ConversionOutcome:
    output: Path
    report: ConversionReport
    source_ir: SourceIR


_LIVE_VALIDATION_EVIDENCE = {
    "zh.mycomic": (
        "Independent browser-only benchmark evidence (not proof of runner connectivity): "
        "GET /comics?sort=-views returned manga entries; /comics/54348 returned details and "
        "chapters; /chapters/794527 returned 30 pages. The CLI/test runner may still receive "
        "HTTP 403 because it does not share the browser's network/session. Preserve relative "
        "keys, but make every request URL absolute."
    ),
    "zh.copymanga": (
        "Independent 2026-07-21 public API reachability evidence from the Tachi input only: "
        "the required CopyManga headers are Accept: application/json, Origin: "
        "https://2025copy.com, Version: 2025.11.21, Region: 0, Webp: 0, platform: 1, and a "
        "browser User-Agent. GET mapi.copy20.com/api/v3/comic2/<path> and "
        "mapi.copy2000.site/api/v3/comic2/<path> returned API code 200, while the input's "
        "api.copy3000.com default returned custom HTTP/API code 210 on this network. Keep the "
        "finite input allowlist, but prefer a currently reachable public domain as the default. "
        "Official AidokuRunner differential evidence for generated v83 loaded all seven filters "
        "across the Swift/Postcard boundary. Region, sort, and dynamic theme changed manga keys, "
        "but rank=day and audience=female produced Manga values with empty titles and key "
        "'/comic/' because /ranks returns RankResult.list of ListItem { comic }, not direct "
        "Comic entries. The free_type filter is marked HotManga-only by the input and is expected "
        "not to change results on the default CopyManga domain. The /comic2/<path> detail "
        "endpoint is also wrapped as ApiResponse<ComicDetailResult>: deserialize the outer "
        "ApiResponse<DetailResult> and use .results before reading .comic or .groups. "
        "Deserializing the HTTP response directly into DetailResult silently produces default "
        "empty fields. Official AidokuRunner evidence for clean4 loaded the dynamic theme UI, "
        "but the filter had no effect because get_search_manga_list never read FilterValue id "
        "'theme'. Read that same id and append &theme=<selected path_word> to the /comics "
        "request; a visible filter that does not change its request is incomplete."
    ),
}

# Values here are benchmark observations, not arbitrary URLs. They are applied only when the AI
# already emitted the same value in a finite select-setting allowlist recovered from the input.
_LIVE_VALIDATED_SETTING_DEFAULTS = {
    "zh.copymanga": {"v2.pref.api_domain": "mapi.copy20.com"},
}

_RUST_DIAGNOSTIC_LOCATION = re.compile(
    r"-->\s+(?P<path>src/[A-Za-z0-9_./-]+\.rs):(?P<line>[1-9][0-9]*):[1-9][0-9]*"
)


def _diagnostic_file_excerpts(
    project: Path,
    diagnostics: str,
    *,
    context_lines: int = 10,
) -> list[dict[str, object]]:
    locations: dict[str, set[int]] = {}
    for match in _RUST_DIAGNOSTIC_LOCATION.finditer(diagnostics):
        path = match.group("path")
        if path == "src/generated_smoke.rs" or ".." in Path(path).parts:
            continue
        locations.setdefault(path, set()).add(int(match.group("line")))

    excerpts: list[dict[str, object]] = []
    for relative, line_numbers in sorted(locations.items()):
        path = project / relative
        if not path.is_file() or path.is_symlink():
            continue
        resolved = path.resolve()
        project_resolved = project.resolve()
        if resolved != project_resolved and project_resolved not in resolved.parents:
            continue
        lines = _remove_generated_smoke_for_repair(path.read_text(encoding="utf-8")).splitlines()
        ranges = sorted(
            (max(1, line - context_lines), min(len(lines), line + context_lines))
            for line in line_numbers
        )
        merged: list[list[int]] = []
        for start, end in ranges:
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        for start, end in merged:
            excerpts.append(
                {
                    "path": relative,
                    "start_line": start,
                    "end_line": end,
                    "content": "\n".join(lines[start - 1 : end]),
                }
            )
    return excerpts[:12]


def _remove_generated_smoke_for_repair(content: str) -> str:
    return content.replace("\n#[cfg(test)]\nmod generated_smoke;\n", "\n")


def _can_use_targeted_repair(
    validation: ValidationResult,
    capability_gaps: list[str],
    excerpts: list[dict[str, object]],
) -> bool:
    failed_stages = {stage.name for stage in validation.stages if not stage.ok}
    return (
        not capability_gaps
        and bool(excerpts)
        and bool(failed_stages)
        and failed_stages <= {"cargo-check", "clippy", "clippy-fix"}
    )


def _apply_repair_patch(
    manifest: GenerationManifest,
    current_files: list[dict[str, str]],
    patch: RepairPatch,
    allowed_excerpts: list[dict[str, object]],
) -> GenerationManifest:
    contents = {item["path"]: item["content"] for item in current_files}
    excerpt_contents: dict[str, list[str]] = {}
    for excerpt in allowed_excerpts:
        path = excerpt.get("path")
        content = excerpt.get("content")
        if isinstance(path, str) and isinstance(content, str):
            excerpt_contents.setdefault(path, []).append(content)
    for edit in patch.edits:
        if not any(edit.old_text in excerpt for excerpt in excerpt_contents.get(edit.path, [])):
            raise AIProviderError(
                f"repair patch old_text was not present in a supplied excerpt: {edit.path}"
            )
        content = contents.get(edit.path)
        if content is None:
            raise AIProviderError(f"repair patch references a missing current file: {edit.path}")
        occurrences = content.count(edit.old_text)
        if occurrences != 1:
            raise AIProviderError(
                f"repair patch old_text must match exactly once in {edit.path}; "
                f"matched {occurrences} times"
            )
        contents[edit.path] = content.replace(edit.old_text, edit.new_text, 1)

    payload = manifest.model_dump(mode="json")
    files = []
    for path, content in sorted(contents.items()):
        if path.endswith(".rs"):
            content = normalize_pinned_aidoku_rust(content)
        try:
            validate_generated_content(path, content)
        except SecurityError as exc:
            raise AIProviderError(f"repair patch failed safety validation: {exc}") from exc
        try:
            generated = GeneratedFile(path=path, content=content)
        except ValueError as exc:
            raise AIProviderError(f"repair patch produced an invalid file: {exc}") from exc
        files.append(generated.model_dump(mode="json"))
    payload["files"] = files
    try:
        return GenerationManifest.model_validate(payload)
    except ValueError as exc:
        raise AIProviderError(f"repair patch produced an invalid manifest: {exc}") from exc


def _needs_toolchain_installation(validation: ValidationResult) -> bool:
    return any(stage.kind.value == "toolchain" for stage in validation.stages)


def _recovered_filter_gaps(
    ir: SourceIR,
    *,
    filters_content: str | None,
    rust_content: str,
) -> list[str]:
    if not ir.filter_specs or filters_content is None:
        return []
    try:
        raw_filters = json.loads(filters_content)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw_filters, list):
        return []
    filters = {
        item.get("id"): item
        for item in raw_filters
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    gaps: list[str] = []
    for spec in ir.filter_specs:
        item = filters.get(spec.id)
        if item is None:
            gaps.append(
                f"recovered Tachi filter {spec.source_class} is missing Aidoku id {spec.id!r}"
            )
            continue
        if item.get("type") != spec.kind:
            gaps.append(
                f"filter {spec.id!r} has type {item.get('type')!r}; "
                f"recovered Tachi {spec.source_class} requires {spec.kind!r}"
            )
        expected_titles = [option.title for option in spec.options]
        expected_values = [option.value for option in spec.options]
        if item.get("options") != expected_titles:
            gaps.append(
                f"filter {spec.id!r} does not preserve recovered display options "
                f"{expected_titles!r}"
            )
        if spec.kind == "select":
            if item.get("ids") != expected_values:
                gaps.append(f"filter {spec.id!r} site values must be {expected_values!r}")
            expected_default = expected_values[spec.default_index]
            if item.get("default") != expected_default:
                gaps.append(
                    f"filter {spec.id!r} default must be recovered site value {expected_default!r}"
                )
        else:
            expected_default = {
                "index": spec.default_index,
                "ascending": bool(spec.default_ascending),
            }
            if item.get("default") != expected_default:
                gaps.append(f"filter {spec.id!r} default must be {expected_default!r}")
            if "FilterValue::Sort" not in rust_content:
                gaps.append(
                    f"filter {spec.id!r} requires a FilterValue::Sort index/ascending mapping"
                )
    return gaps


def _capability_gaps(ir: SourceIR, manifest: GenerationManifest) -> list[str]:
    resource_contents = {item.path: item.content for item in manifest.files}
    traits = set(manifest.implemented_traits)
    dependencies = {item.name for item in manifest.dependencies}
    rust_files = [item for item in manifest.files if item.path.endswith(".rs")]
    rust_content = "\n".join(item.content for item in rust_files)
    input_content = "\n".join(item.content for item in ir.files)
    gaps: list[str] = []
    if (Capability.POPULAR in ir.capabilities or Capability.LATEST in ir.capabilities) and (
        "ListingProvider" not in traits
    ):
        gaps.append("source declares popular/latest listings but generated no ListingProvider")
    if Capability.FILTERS in ir.capabilities:
        filters = resource_contents.get("res/filters.json")
        if filters is None:
            gaps.append("source declares filters but generated no res/filters.json")
        else:
            try:
                filters_empty = not json.loads(filters)
            except json.JSONDecodeError:
                filters_empty = False
            if filters_empty:
                gaps.append("source declares filters but generated an empty res/filters.json")
        gaps.extend(
            _recovered_filter_gaps(
                ir,
                filters_content=filters,
                rust_content=rust_content,
            )
        )
    if Capability.DYNAMIC_FILTERS in ir.capabilities and "DynamicFilters" not in traits:
        gaps.append("source fetches dynamic filters but generated no DynamicFilters provider")
    if Capability.DYNAMIC_FILTERS in ir.capabilities and any(
        _rust_function_contains(item.content, "get_dynamic_filters", "serde_json::from_str")
        for item in rust_files
    ):
        gaps.append(
            "get_dynamic_filters attempts to deserialize aidoku::Filter with serde_json; "
            "construct typed SelectFilter/Filter values directly because Filter is not "
            "Deserialize"
        )
    if Capability.DYNAMIC_FILTERS in ir.capabilities and any(
        _rust_function_contains(item.content, "get_dynamic_filters", "listing_query")
        for item in rust_files
    ):
        gaps.append(
            "get_dynamic_filters performs a full manga listing request; use a dedicated "
            "options-only request (for GraphQL, query only the recovered option field) "
            "instead of downloading manga entries"
        )
    if Capability.JSON_API in ir.capabilities and any(
        _rust_function_contains(item.content, "listing_query", "description")
        and _rust_function_contains(item.content, "listing_query", "allCategory")
        for item in rust_files
    ):
        gaps.append(
            "GraphQL listing requests include detail-only fields and dynamic-filter metadata; "
            "keep the source page size but omit description from list projections and fetch "
            "allCategory only in the dedicated dynamic-filter request"
        )
    if Capability.DYNAMIC_FILTERS in ir.capabilities:
        for filter_id in sorted(
            {
                filter_id
                for item in rust_files
                for filter_id in _dynamic_filter_ids_missing_from_query_mapping(item.content)
            }
        ):
            gaps.append(
                f"dynamic filter {filter_id!r} is never read by get_search_manga_list; "
                "read its FilterValue using the same id and send the selected site value "
                "in the list/search request"
            )
    if Capability.SETTINGS in ir.capabilities:
        settings = resource_contents.get("res/settings.json")
        if settings is None:
            gaps.append("source declares settings but generated no res/settings.json")
        else:
            try:
                settings_empty = not json.loads(settings)
            except json.JSONDecodeError:
                settings_empty = False
            if settings_empty:
                gaps.append("source declares settings but generated an empty res/settings.json")
    if Capability.IMAGE_HEADERS in ir.capabilities and "ImageRequestProvider" not in traits:
        gaps.append("source declares image headers but generated no ImageRequestProvider")
    if (
        Capability.IMAGE_HEADERS in ir.capabilities
        and any(
            _rust_function_contains(item.content, "get_image_request", "context")
            and _rust_function_contains(item.content, "get_image_request", "referer")
            for item in rust_files
        )
        and "PageContent::url_context" not in rust_content
    ):
        gaps.append(
            "get_image_request reads a Referer from PageContext but generated pages never use "
            "PageContent::url_context; attach the exact chapter/page Referer to every page URL "
            "and use the site base URL as the cover-image fallback"
        )
    source_uses_cookie_jar = any(
        marker in input_content
        for marker in ("cookieJar.loadForRequest", "cookieJar.saveFromResponse")
    )
    if source_uses_cookie_jar:
        settings_content = resource_contents.get("res/settings.json", "")
        has_cookie_setting = "cookie" in settings_content.lower()
        has_api_cookie_header = any(
            _rust_function_has_header(item.content, "post_query", "cookie") for item in rust_files
        )
        has_image_cookie_header = any(
            _rust_function_has_header(item.content, "get_image_request", "cookie")
            for item in rust_files
        )
        if not has_cookie_setting or not has_api_cookie_header or not has_image_cookie_header:
            gaps.append(
                "input public requests depend on a Cookie session, but the generated source "
                "does not expose an optional cookie setting and apply its Cookie header to both "
                "API and image requests"
            )
    if Capability.DEEP_LINKS in ir.capabilities and "DeepLinkHandler" not in traits:
        gaps.append("source declares deep links but generated no DeepLinkHandler")
    if Capability.JSON_API in ir.capabilities and "serde" not in dependencies:
        gaps.append("JSON API source generated no pinned serde dependency")
    update_uses_combined_helper = any(
        _rust_function_contains(item.content, "get_manga_update", "needs_details")
        and _rust_function_contains(item.content, "get_manga_update", "needs_chapters")
        and _rust_function_contains(item.content, "get_manga_update", "manga_query")
        for item in rust_files
    )
    helper_selects_requested_projection = any(
        _rust_function_contains(item.content, "manga_query", "(true, false)")
        and _rust_function_contains(item.content, "manga_query", "(false, true)")
        for item in rust_files
    )
    if (
        Capability.DETAILS in ir.capabilities
        and Capability.CHAPTERS in ir.capabilities
        and update_uses_combined_helper
        and not helper_selects_requested_projection
        and "comicById" in rust_content
        and "chaptersByComicId" in rust_content
    ):
        gaps.append(
            "get_manga_update unconditionally fetches a combined details-and-chapters GraphQL "
            "query; choose details-only, chapters-only, or combined queries so each call fetches "
            "only the data requested by needs_details and needs_chapters"
        )
    if Capability.DETAILS in ir.capabilities and Capability.CHAPTERS in ir.capabilities:
        repeated_detail_routes: set[str] = set()
        for item in rust_files:
            if not (
                _rust_function_contains(item.content, "get_manga_update", "needs_details")
                and _rust_function_contains(item.content, "get_manga_update", "needs_chapters")
            ):
                continue
            update_routes = _rust_function_route_literals(item.content, "get_manga_update")
            chapter_helpers = {
                name
                for name in _rust_function_calls(item.content, "get_manga_update")
                if "chapter" in name.lower()
            }
            for helper in chapter_helpers:
                helper_routes = {
                    route
                    for rust_file in rust_files
                    for route in _rust_function_route_literals(rust_file.content, helper)
                }
                repeated_detail_routes.update(update_routes & helper_routes)
        if repeated_detail_routes:
            gaps.append(
                "get_manga_update and its chapter helper fetch the same REST detail route twice "
                "when details and chapters are both requested; fetch it once and pass the "
                "decoded detail response into the chapter helper: "
                + ", ".join(sorted(repeated_detail_routes))
            )
    if (
        ir.source_format == "decompiled_apk"
        and Capability.JSON_API in ir.capabilities
        and not any(_has_idempotent_get_retry(item.content) for item in rust_files)
    ):
        gaps.append(
            "decompiled Tachi JSON source generated no centralized one-retry helper for "
            "transient idempotent GET RequestError; reconstruct and resend the same request "
            "once, then deserialize only the successful response"
        )
    if (
        ir.source_format == "kotlin_module"
        and "HttpSource" in ir.parent_classes
        and "Request::get" in rust_content
        and ".send()" in rust_content
        and not any(_has_idempotent_get_retry(item.content) for item in rust_files)
    ):
        gaps.append(
            "standard Kotlin HttpSource generated no centralized one-retry helper for "
            "transient idempotent GET RequestError; reconstruct and resend the same request "
            "once, then parse only the successful response"
        )
    if Capability.CHAPTERS in ir.capabilities and any(
        _rust_chapter_parser_compiles_regex(item.content) for item in rust_files
    ):
        gaps.append(
            "generated code compiles Regex::new on every chapter parse; for fixed embedded-JSON "
            "delimiters or numeric chapter labels, use bounded string scanning so each update "
            "does not compile a regex and pull regex runtime cost into the WASM hot path"
        )
    if Capability.ENCRYPTED_JSON in ir.capabilities:
        missing_crypto = sorted({"aes", "cbc", "serde", "serde_json"} - dependencies)
        if missing_crypto:
            gaps.append(
                "encrypted JSON source omitted required pinned dependencies: "
                + ", ".join(missing_crypto)
            )
        if not dependencies.intersection({"base64", "hex"}):
            gaps.append("encrypted JSON source requested neither hex nor base64 decoding")
    if Capability.TRIPLE_DES_CBC in ir.capabilities:
        missing_crypto = sorted({"des", "cbc", "base64"} - dependencies)
        if missing_crypto:
            gaps.append(
                "3DES-CBC request signing omitted required pinned dependencies: "
                + ", ".join(missing_crypto)
            )
        if "current_date" not in rust_content or re.search(
            r"\blet\s+time\s*=\s*\"0\"", rust_content
        ):
            gaps.append(
                "3DES-CBC request signing uses no live millisecond Unix timestamp; "
                "call aidoku::imports::std::current_date() and multiply its seconds by 1000"
            )
    if Capability.DYNAMIC_BASE_URLS in ir.capabilities:
        settings = resource_contents.get("res/settings.json")
        if settings is None:
            gaps.append("dynamic base URL source generated no res/settings.json")
        if "defaults_get" not in rust_content:
            gaps.append("dynamic base URL source generated no validated defaults_get resolver")
    if ir.relative_url_keys:
        if not any(_has_rust_function(item.content, "absolute_url") for item in rust_files):
            gaps.append(
                "source emits relative manga/chapter keys but generated no absolute_url helper"
            )
        if any(_passes_relative_key_to_request(item.content) for item in rust_files):
            gaps.append(
                "generated code passes Manga.key or Chapter.key to a request without absolute_url"
            )
    for route in ir.chapter_page_routes:
        default_variant = next(variant for variant in route.variants if variant.is_default)
        if default_variant.strip_prefix and default_variant.strip_prefix not in rust_content:
            gaps.append(
                "chapter page route omits required key prefix removal: "
                + repr(default_variant.strip_prefix)
            )
        for replacement in default_variant.replacements:
            if replacement.new not in rust_content:
                gaps.append(
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
            gaps.append(
                "source image policy requires preserving each cover URL exactly; "
                "generated code applies a chapter-resolution transform to a cover URL"
            )
        if image_policy.chapter_resolution_regex and not (
            re.search(r'(?:ends_with|strip_suffix)\(\s*"x?\.jpg"', rust_content)
            and re.search(r'(?:ends_with|strip_suffix)\(\s*"x?\.webp"', rust_content)
        ):
            gaps.append(
                "chapter image resolution translation lacks the recovered exact x.jpg/x.webp "
                f"suffix scope {image_policy.chapter_resolution_regex!r}"
            )
    if "date_upload" in input_content and "date_uploaded" not in rust_content:
        gaps.append("input chapters expose date_upload but generated chapters omit date_uploaded")
    if "scanlator" in input_content and "scanlators" not in rust_content:
        gaps.append("input chapters expose scanlator but generated chapters omit scanlators")
    if re.search(
        r"\bclass\s+RankResult\b[\s\S]{0,1000}?\bList<ListItem>\s+list\b", input_content
    ) and re.search(
        r'["]/ranks\?[\s\S]{0,3000}?\bApiResponse<ListResult>\b',
        rust_content,
    ):
        gaps.append(
            "rank endpoint returns RankResult.list entries wrapping the manga in ListItem.comic; "
            "generated code incorrectly deserializes ranks as a direct ListResult<Comic>, "
            "producing empty manga titles and keys"
        )
    if re.search(
        r"ApiResponse\.class[\s\S]{0,300}?"
        r"Reflection\.typeOf\(ComicDetailResult\.class\)",
        input_content,
    ) and any(_detail_helper_skips_api_envelope(item.content) for item in rust_files):
        gaps.append(
            "detail endpoint is wrapped in ApiResponse<ComicDetailResult>, but generated detail "
            "helper deserializes the HTTP response directly into DetailResult; deserialize "
            "ApiResponse<DetailResult> and return response.results"
        )
    if re.search(r"\bSChapter\b[\s\S]{0,2000}?\burl\s*=", input_content) and not re.search(
        r"\bChapter\s*\{[\s\S]{0,4000}?\burl\s*:", rust_content
    ):
        gaps.append("input chapters expose a URL but generated Chapter values omit url")
    legacy_settings = {value for value in ("zh-hant", "zh-hans") if f'"{value}"' in input_content}
    missing_legacy_settings = sorted(
        value for value in legacy_settings if f'"{value}"' not in rust_content
    )
    if missing_legacy_settings:
        gaps.append(
            "generated settings logic omits legacy input values: "
            + ", ".join(missing_legacy_settings)
        )
    return gaps


def _walk_rust(content: str):
    stack = [get_parser("rust").parse(content.encode("utf-8")).root_node]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _has_rust_function(content: str, name: str) -> bool:
    for node in _walk_rust(content):
        if node.type != "function_item":
            continue
        identifier = node.child_by_field_name("name")
        if identifier is not None and identifier.text.decode("utf-8") == name:
            return True
    return False


def _rust_function_contains(content: str, name: str, needle: str) -> bool:
    for node in _walk_rust(content):
        if node.type != "function_item":
            continue
        identifier = node.child_by_field_name("name")
        if identifier is None or identifier.text.decode("utf-8") != name:
            continue
        return needle in node.text.decode("utf-8", errors="replace")
    return False


def _rust_function_has_header(content: str, name: str, header: str) -> bool:
    pattern = re.compile(rf'\.header\s*\(\s*"{re.escape(header)}"', re.IGNORECASE)
    for node in _walk_rust(content):
        if node.type != "function_item":
            continue
        identifier = node.child_by_field_name("name")
        if identifier is None or identifier.text.decode("utf-8") != name:
            continue
        return pattern.search(node.text.decode("utf-8", errors="replace")) is not None
    return False


def _rust_function_calls(content: str, name: str) -> set[str]:
    for node in _walk_rust(content):
        if node.type != "function_item":
            continue
        identifier = node.child_by_field_name("name")
        if identifier is None or identifier.text.decode("utf-8") != name:
            continue
        text = node.text.decode("utf-8", errors="replace")
        return set(re.findall(r"\b(?:self\.)?([A-Za-z_][A-Za-z0-9_]*)\s*\(", text))
    return set()


def _rust_function_route_literals(content: str, name: str) -> set[str]:
    for node in _walk_rust(content):
        if node.type != "function_item":
            continue
        identifier = node.child_by_field_name("name")
        if identifier is None or identifier.text.decode("utf-8") != name:
            continue
        text = node.text.decode("utf-8", errors="replace")
        return set(re.findall(r'"(/[^"\\]*(?:\\.[^"\\]*)*)"', text))
    return set()


def _dynamic_filter_ids_missing_from_query_mapping(content: str) -> set[str]:
    dynamic_texts: list[str] = []
    functions: dict[str, list[str]] = {}
    for node in _walk_rust(content):
        if node.type != "function_item":
            continue
        identifier = node.child_by_field_name("name")
        if identifier is None:
            continue
        name = identifier.text.decode("utf-8")
        text = node.text.decode("utf-8", errors="replace")
        functions.setdefault(name, []).append(text)
        if name == "get_dynamic_filters":
            dynamic_texts.append(text)
    dynamic_ids = {
        match.group(1)
        for text in dynamic_texts
        for match in re.finditer(
            r'\bSelectFilter\s*\{[\s\S]{0,1200}?\bid\s*:\s*"([^"\\]+)"',
            text,
        )
    }
    reachable = {"get_search_manga_list"}
    pending = ["get_search_manga_list"]
    while pending:
        current = pending.pop()
        current_text = "\n".join(functions.get(current, []))
        for candidate in functions:
            if candidate in reachable:
                continue
            if re.search(rf"\b{re.escape(candidate)}\s*\(", current_text):
                reachable.add(candidate)
                pending.append(candidate)
    query_mapping = "\n".join(text for name in reachable for text in functions.get(name, []))
    return {filter_id for filter_id in dynamic_ids if f'"{filter_id}"' not in query_mapping}


def _detail_helper_skips_api_envelope(content: str) -> bool:
    for node in _walk_rust(content):
        if node.type != "function_item":
            continue
        identifier = node.child_by_field_name("name")
        if identifier is None or "detail" not in identifier.text.decode("utf-8").lower():
            continue
        text = node.text.decode("utf-8", errors="replace")
        signature = text.split("{", 1)[0]
        if (
            re.search(r"Result<(?:Comic)?DetailResult>", signature)
            and ".get_json_owned()" in text
            and "ApiResponse<" not in text
        ):
            return True
    return False


def _has_idempotent_get_retry(content: str) -> bool:
    for node in _walk_rust(content):
        if node.type != "function_item":
            continue
        compact = _compact_rust_node(node)
        if compact.count(".send()") >= 2 and (
            "match" in compact or "or_else" in compact or "ifletErr" in compact
        ):
            return True
    return False


def _rust_chapter_parser_compiles_regex(content: str) -> bool:
    for node in _walk_rust(content):
        if node.type != "function_item":
            continue
        identifier = node.child_by_field_name("name")
        if identifier is None or "chapter" not in identifier.text.decode("utf-8").lower():
            continue
        if "Regex::new" in node.text.decode("utf-8", errors="replace"):
            return True
    return False


def _compact_rust_node(node: Node) -> str:
    text = node.text.decode("utf-8", errors="replace")
    text = re.sub(r"/\*[\s\S]*?\*/|//[^\r\n]*", "", text)
    return "".join(text.split())


def _passes_relative_key_to_request(content: str) -> bool:
    for node in _walk_rust(content):
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if function is None or arguments is None:
            continue
        called = _compact_rust_node(function)
        if not (
            called in {"Request::get", "Request::post", "request"} or called.endswith(".request")
        ):
            continue
        values = _compact_rust_node(arguments)
        has_relative_key = "manga.key" in values or "chapter.key" in values
        if has_relative_key and "absolute_url" not in values:
            return True
    return False


def _should_repair(
    validation: ValidationResult,
    capability_gaps: list[str],
    *,
    live: bool,
) -> bool:
    if _needs_toolchain_installation(validation):
        return False
    if capability_gaps:
        return True
    if validation.blocked:
        return False
    return not (validation.build_ok and validation.package_ok and (validation.live_ok or not live))


def _repair_diagnostics(
    ir: SourceIR,
    validation: ValidationResult,
    capability_gaps: list[str],
) -> str:
    parts = [validation.diagnostics]
    evidence = _LIVE_VALIDATION_EVIDENCE.get(ir.metadata.source_id)
    if evidence:
        parts.append(evidence)
    if capability_gaps:
        parts.append("Generated capability/contract gaps:\n- " + "\n- ".join(capability_gaps))
    return "\n\n".join(part for part in parts if part)


def _prepare_output(output: Path, *, force: bool) -> None:
    if output.exists():
        if not force:
            raise InputError(f"output already exists: {output}; pass --force to replace it")
        if output.is_symlink():
            raise InputError(f"refusing to replace a symbolic-link output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)


def _workspace_path(output: Path) -> Path:
    return output.parent / f".{output.name}.c2a-work"


def _atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _write_checkpoint(workspace: Path, checkpoint: ConversionCheckpoint) -> None:
    _atomic_write_text(
        workspace / "checkpoint.json",
        checkpoint.model_dump_json(indent=2, exclude_none=True) + "\n",
    )


def _load_checkpoint(workspace: Path) -> ConversionCheckpoint:
    path = workspace / "checkpoint.json"
    if not path.is_file():
        raise InputError(f"resume workspace has no checkpoint: {workspace}")
    try:
        return ConversionCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"invalid resume checkpoint {path}: {exc}") from exc


def _manifest_path(workspace: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.parts[:1] != ("manifests",):
        raise InputError(f"invalid manifest path in resume checkpoint: {relative}")
    path = workspace.joinpath(*candidate.parts)
    if path.is_symlink():
        raise InputError(f"refusing symbolic-link resume manifest: {path}")
    return path


def _save_manifest(
    workspace: Path,
    checkpoint: ConversionCheckpoint,
    result,
    *,
    purpose: str,
) -> GenerationManifest:
    number = len(checkpoint.ai_rounds) + 1
    relative = f"manifests/round-{number:02d}.json"
    path = _manifest_path(workspace, relative)
    path.parent.mkdir(exist_ok=True)
    _atomic_write_text(path, result.manifest.model_dump_json(indent=2) + "\n")
    checkpoint.current_manifest = relative
    checkpoint.ai_rounds.append(ai_round(number, purpose, result))
    checkpoint.warnings.extend(result.warnings)
    checkpoint.manifest_warnings = list(result.manifest.warnings)
    checkpoint.unsupported_features.extend(result.manifest.unsupported_features)
    checkpoint.phase = "manifest_saved"
    checkpoint.validation = None
    _write_checkpoint(workspace, checkpoint)
    return result.manifest


def _load_manifest(workspace: Path, checkpoint: ConversionCheckpoint) -> GenerationManifest:
    if checkpoint.current_manifest is None:
        raise InputError("resume checkpoint has no saved generation manifest")
    path = _manifest_path(workspace, checkpoint.current_manifest)
    if not path.is_file():
        raise InputError(f"saved generation manifest is missing: {path}")
    try:
        return GenerationManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"invalid saved generation manifest {path}: {exc}") from exc


def _manifest_history(workspace: Path, checkpoint: ConversionCheckpoint) -> list[dict[str, object]]:
    history: list[dict[str, object]] = []
    for number in range(1, len(checkpoint.ai_rounds) + 1):
        relative = f"manifests/round-{number:02d}.json"
        path = _manifest_path(workspace, relative)
        try:
            manifest = GenerationManifest.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InputError(f"invalid saved generation manifest {path}: {exc}") from exc
        history.append(
            {
                "round": number,
                "implemented_traits": manifest.implemented_traits,
                "dependencies": [item.model_dump(mode="json") for item in manifest.dependencies],
                "file_paths": [item.path for item in manifest.files],
            }
        )
    return history


def _effective_manifest(
    ir: SourceIR,
    workspace: Path,
    checkpoint: ConversionCheckpoint,
    manifest: GenerationManifest,
) -> GenerationManifest:
    """Carry required resources across repair rounds without altering raw AI audit files."""
    required_paths = set()
    if Capability.FILTERS in ir.capabilities:
        required_paths.add("res/filters.json")
    if Capability.SETTINGS in ir.capabilities or Capability.DYNAMIC_BASE_URLS in ir.capabilities:
        required_paths.add("res/settings.json")
    present = {item.path for item in manifest.files}
    missing = required_paths - present
    inherited = []
    if missing:
        for number in range(len(checkpoint.ai_rounds) - 1, 0, -1):
            previous_path = _manifest_path(workspace, f"manifests/round-{number:02d}.json")
            try:
                previous = GenerationManifest.model_validate_json(
                    previous_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise InputError(
                    f"invalid saved generation manifest {previous_path}: {exc}"
                ) from exc
            for item in previous.files:
                if item.path in missing:
                    inherited.append(item)
                    missing.remove(item.path)
            if not missing:
                break
    effective = manifest
    if inherited:
        inherited_paths = sorted(item.path for item in inherited)
        warning = (
            "repair manifest omitted SourceIR-required resources; preserved from the prior "
            "round: " + ", ".join(inherited_paths)
        )
        if warning not in checkpoint.warnings:
            checkpoint.warnings.append(warning)
        effective = manifest.model_copy(update={"files": manifest.files + inherited})
    effective = _with_recovered_filter_defaults(ir, effective)
    return _with_live_validated_setting_defaults(ir, effective)


def _with_recovered_filter_defaults(
    ir: SourceIR,
    manifest: GenerationManifest,
) -> GenerationManifest:
    if not ir.filter_specs:
        return manifest
    specs = {spec.id: spec for spec in ir.filter_specs}
    updated_files = []
    changed = False
    for generated in manifest.files:
        if generated.path != "res/filters.json":
            updated_files.append(generated)
            continue
        try:
            filters = json.loads(generated.content)
        except json.JSONDecodeError:
            updated_files.append(generated)
            continue
        if not isinstance(filters, list):
            updated_files.append(generated)
            continue
        for item in filters:
            if not isinstance(item, dict):
                continue
            spec = specs.get(item.get("id"))
            if spec is None:
                continue
            if spec.kind == "select":
                value = spec.options[spec.default_index].value
            else:
                value = {
                    "index": spec.default_index,
                    "ascending": bool(spec.default_ascending),
                }
                item.setdefault("canAscend", True)
            if item.get("default") != value:
                item["default"] = value
                changed = True
        content = json.dumps(filters, ensure_ascii=False, indent="\t") + "\n"
        updated_files.append(generated.model_copy(update={"content": content}))
    if not changed:
        return manifest
    return manifest.model_copy(update={"files": updated_files})


def _with_live_validated_setting_defaults(
    ir: SourceIR,
    manifest: GenerationManifest,
) -> GenerationManifest:
    overrides = _LIVE_VALIDATED_SETTING_DEFAULTS.get(ir.metadata.source_id)
    if not overrides or Capability.DYNAMIC_BASE_URLS not in ir.capabilities:
        return manifest
    updated_files = []
    changed = False
    for generated in manifest.files:
        if generated.path != "res/settings.json":
            updated_files.append(generated)
            continue
        try:
            settings = json.loads(generated.content)
        except json.JSONDecodeError:
            updated_files.append(generated)
            continue
        if not isinstance(settings, list):
            updated_files.append(generated)
            continue
        for group in settings:
            if not isinstance(group, dict) or not isinstance(group.get("items"), list):
                continue
            for item in group["items"]:
                if not isinstance(item, dict):
                    continue
                preferred = overrides.get(item.get("key"))
                values = item.get("values")
                if preferred is None or not isinstance(values, list) or preferred not in values:
                    continue
                if item.get("default") != preferred:
                    item["default"] = preferred
                    changed = True
        content = json.dumps(settings, ensure_ascii=False, indent="\t") + "\n"
        updated_files.append(generated.model_copy(update={"content": content}))
    if not changed:
        return manifest
    return manifest.model_copy(update={"files": updated_files})


def _restore_installed_workspace(output: Path, workspace: Path) -> None:
    audit = output / ".c2a"
    checkpoint_path = audit / "checkpoint.json"
    if not checkpoint_path.is_file():
        raise InputError(f"no resumable conversion workspace for output: {output}")
    try:
        ConversionCheckpoint.model_validate_json(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"invalid installed conversion checkpoint: {exc}") from exc
    workspace.mkdir()
    project = workspace / "project"
    try:
        os.replace(output, project)
        shutil.copy2(project / ".c2a" / "checkpoint.json", workspace / "checkpoint.json")
        shutil.copy2(project / ".c2a" / "source-ir.json", workspace / "source-ir.json")
        shutil.copytree(project / ".c2a" / "manifests", workspace / "manifests")
    except BaseException:
        if project.exists() and not output.exists():
            os.replace(project, output)
        shutil.rmtree(workspace, ignore_errors=True)
        raise


def _refresh_resume_source_ir(
    ir: SourceIR,
    *,
    input_ref: str,
    workspace: Path,
) -> SourceIR:
    if ir.schema_version >= 2:
        return ir
    with resolve_source(input_ref) as resolved:
        refreshed = analyze_source(resolved)
    if refreshed.metadata.source_id != ir.metadata.source_id:
        raise InputError(
            "refreshed resume input changed source id from "
            f"{ir.metadata.source_id!r} to {refreshed.metadata.source_id!r}"
        )
    _atomic_write_text(
        workspace / "source-ir.json",
        refreshed.model_dump_json(indent=2, exclude={"license_text"}) + "\n",
    )
    return refreshed


def _bump_completed_resume_version(project: Path, ir: SourceIR) -> SourceIR:
    source_path = project / "res" / "source.json"
    try:
        source = json.loads(source_path.read_text(encoding="utf-8"))
        current = int(source["info"]["version"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InputError(f"unable to bump installed source version: {exc}") from exc
    version = max(ir.metadata.version, current + 1)
    source["info"]["version"] = version
    _atomic_write_text(
        source_path,
        json.dumps(source, ensure_ascii=False, indent="\t") + "\n",
    )
    bumped = ir.model_copy(
        update={
            "metadata": ir.metadata.model_copy(update={"version": version}),
        }
    )
    _atomic_write_text(
        project.parent / "source-ir.json",
        bumped.model_dump_json(indent=2, exclude={"license_text"}) + "\n",
    )
    return bumped


def _copy_conversion_audit(workspace: Path, project: Path) -> list[str]:
    destination = project / ".c2a"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir()
    shutil.copy2(workspace / "checkpoint.json", destination / "checkpoint.json")
    shutil.copy2(workspace / "source-ir.json", destination / "source-ir.json")
    shutil.copytree(workspace / "manifests", destination / "manifests")
    files = [".c2a/checkpoint.json", ".c2a/source-ir.json"]
    files.extend(
        path.relative_to(project).as_posix()
        for path in sorted((destination / "manifests").glob("*.json"))
    )
    return files


def _check_resume_compatibility(
    checkpoint: ConversionCheckpoint,
    *,
    input_ref: str,
    output: Path,
    settings: AISettings,
    query: str | None,
    live: bool,
) -> None:
    mismatches = []
    if checkpoint.input_ref != input_ref:
        mismatches.append("input")
    if checkpoint.output != str(output):
        mismatches.append("output")
    if checkpoint.provider_base_url.rstrip("/") != settings.base_url.rstrip("/"):
        mismatches.append("base URL")
    if checkpoint.model != settings.model:
        mismatches.append("model")
    if checkpoint.query != query:
        mismatches.append("query")
    if checkpoint.live != live:
        mismatches.append("live mode")
    if mismatches:
        raise InputError("resume options do not match the saved run: " + ", ".join(mismatches))


def _install_output(staged: Path, output: Path) -> None:
    """Install a completed staging tree while keeping an old output recoverable."""
    if not output.exists():
        os.replace(staged, output)
        return
    backup = output.parent / f".{output.name}.backup-{uuid.uuid4().hex}"
    os.replace(output, backup)
    try:
        os.replace(staged, output)
    except BaseException:
        os.replace(backup, output)
        raise
    if backup.is_dir() and not backup.is_symlink():
        shutil.rmtree(backup, ignore_errors=True)
    else:
        backup.unlink(missing_ok=True)


def convert_source(
    input_ref: str,
    *,
    output: Path,
    settings: AISettings,
    query: str | None = None,
    live: bool = True,
    force: bool = False,
    proxy: str | None = None,
    resume: bool = False,
) -> ConversionOutcome:
    output = output.expanduser().absolute()
    if output.exists() and output.is_symlink():
        raise InputError(f"refusing to replace a symbolic-link output: {output}")
    workspace = _workspace_path(output)
    project = workspace / "project"

    if resume:
        if workspace.is_symlink():
            raise InputError(f"refusing symbolic-link resume workspace: {workspace}")
        if not workspace.is_dir():
            if output.is_dir():
                _restore_installed_workspace(output, workspace)
            else:
                raise InputError(f"no resumable conversion workspace for output: {output}")
        checkpoint = _load_checkpoint(workspace)
        _check_resume_compatibility(
            checkpoint,
            input_ref=input_ref,
            output=output,
            settings=settings,
            query=query,
            live=live,
        )
        if not project.is_dir() or project.is_symlink():
            raise InputError(f"resume staging project is missing or unsafe: {project}")
        source_ir_path = workspace / "source-ir.json"
        try:
            ir = SourceIR.model_validate_json(source_ir_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InputError(f"invalid saved SourceIR {source_ir_path}: {exc}") from exc
        ir = _refresh_resume_source_ir(
            ir,
            input_ref=input_ref,
            workspace=workspace,
        )
        if checkpoint.phase == "complete":
            ir = _bump_completed_resume_version(project, ir)
    else:
        _prepare_output(output, force=force)
        if workspace.exists() or workspace.is_symlink():
            raise InputError(
                f"conversion workspace already exists: {workspace}; pass --resume to continue it"
            )
        workspace.mkdir()
        try:
            with resolve_source(input_ref) as resolved:
                ir = analyze_source(resolved)
                create_scaffold(project, ir, resolved)
        except BaseException:
            shutil.rmtree(workspace, ignore_errors=True)
            raise
        _atomic_write_text(
            workspace / "source-ir.json",
            ir.model_dump_json(indent=2, exclude={"license_text"}) + "\n",
        )
        checkpoint = ConversionCheckpoint(
            input_ref=input_ref,
            output=str(output),
            provider_base_url=settings.base_url,
            model=settings.model,
            query=query,
            live=live,
            force=force,
            warnings=list(ir.warnings),
            unsupported_features=list(ir.unsupported_features),
        )
        _write_checkpoint(workspace, checkpoint)

    template_matches = match_templates(ir)
    if checkpoint.current_manifest is not None:
        manifest = _load_manifest(workspace, checkpoint)
        manifest = _effective_manifest(ir, workspace, checkpoint, manifest)
        generated_files = apply_generation_manifest(project, ir, manifest, query=query)
        capability_gaps = _capability_gaps(ir, manifest)
        validation = validate_project(project, live=live, proxy=proxy)
        validation.contract_ok = not capability_gaps
        checkpoint.generated_files = generated_files
        checkpoint.capability_gaps = capability_gaps
        checkpoint.validation = validation
        checkpoint.phase = "validated"
        _write_checkpoint(workspace, checkpoint)
    else:
        with OpenAICompatibleClient(settings) as client:
            generated = client.generate(ir)
            manifest = _save_manifest(
                workspace,
                checkpoint,
                generated,
                purpose="generate",
            )
            manifest = _effective_manifest(ir, workspace, checkpoint, manifest)
            generated_files = apply_generation_manifest(project, ir, manifest, query=query)
            capability_gaps = _capability_gaps(ir, manifest)
            validation = validate_project(project, live=live, proxy=proxy)
            validation.contract_ok = not capability_gaps
            checkpoint.generated_files = generated_files
            checkpoint.capability_gaps = capability_gaps
            checkpoint.validation = validation
            checkpoint.phase = "validated"
            _write_checkpoint(workspace, checkpoint)

    repair_number = max(0, len(checkpoint.ai_rounds) - 1)
    if _should_repair(validation, capability_gaps, live=live):
        with OpenAICompatibleClient(settings) as client:
            while (
                _should_repair(validation, capability_gaps, live=live)
                and repair_number < settings.max_repair_rounds
            ):
                repair_number += 1
                current_files = read_generated_files(project)
                targeted_diagnostics = validation.diagnostics[-MAX_AI_DIAGNOSTIC_CHARS:]
                excerpts = _diagnostic_file_excerpts(project, targeted_diagnostics)
                if _can_use_targeted_repair(validation, capability_gaps, excerpts):
                    try:
                        patch_result = client.repair_patch(
                            ir,
                            current_file_excerpts=excerpts,
                            diagnostics=targeted_diagnostics,
                        )
                        patched_manifest = _apply_repair_patch(
                            manifest,
                            current_files,
                            patch_result.patch,
                            excerpts,
                        )
                        repaired = AIResult(
                            manifest=patched_manifest,
                            structured_output=patch_result.structured_output,
                            usage=patch_result.usage,
                            warnings=patch_result.warnings,
                        )
                    except AIProviderError as exc:
                        diagnostics = _repair_diagnostics(ir, validation, capability_gaps)
                        diagnostics = diagnostics[-MAX_AI_DIAGNOSTIC_CHARS:]
                        repaired = client.repair(
                            ir,
                            current_files=current_files,
                            diagnostics=diagnostics,
                            manifest_history=_manifest_history(workspace, checkpoint),
                        )
                        repaired.warnings.append(f"targeted repair fallback: {exc}")
                else:
                    diagnostics = _repair_diagnostics(ir, validation, capability_gaps)
                    diagnostics = diagnostics[-MAX_AI_DIAGNOSTIC_CHARS:]
                    repaired = client.repair(
                        ir,
                        current_files=current_files,
                        diagnostics=diagnostics,
                        manifest_history=_manifest_history(workspace, checkpoint),
                    )
                manifest = _save_manifest(
                    workspace,
                    checkpoint,
                    repaired,
                    purpose="repair",
                )
                manifest = _effective_manifest(ir, workspace, checkpoint, manifest)
                generated_files = apply_generation_manifest(project, ir, manifest, query=query)
                capability_gaps = _capability_gaps(ir, manifest)
                validation = validate_project(project, live=live, proxy=proxy)
                validation.contract_ok = not capability_gaps
                checkpoint.generated_files = generated_files
                checkpoint.capability_gaps = capability_gaps
                checkpoint.validation = validation
                checkpoint.phase = "validated"
                _write_checkpoint(workspace, checkpoint)

    deterministic_files = [
        ".cargo/config.toml",
        "Cargo.toml",
        "res/source.json",
        "res/icon.png",
        "PROVENANCE.md",
        "report.json",
        "report.md",
    ]
    if (project / "LICENSE.input").is_file():
        deterministic_files.append("LICENSE.input")
    if (project / "package.aix").is_file():
        deterministic_files.append("package.aix")
    audit_files = [".c2a/checkpoint.json", ".c2a/source-ir.json"]
    audit_files.extend(
        f".c2a/manifests/round-{number:02d}.json"
        for number in range(1, len(checkpoint.ai_rounds) + 1)
    )
    report = ConversionReport(
        status=classify_status(validation, live_requested=live),
        input_ref=input_ref,
        source_id=ir.metadata.source_id,
        provider_base_url=settings.base_url,
        model=settings.model,
        ai_rounds=checkpoint.ai_rounds,
        generated_files=sorted(set(checkpoint.generated_files + deterministic_files + audit_files)),
        template_matches=template_matches,
        warnings=list(
            dict.fromkeys(
                checkpoint.warnings + checkpoint.manifest_warnings + checkpoint.capability_gaps
            )
        ),
        unsupported_features=list(dict.fromkeys(checkpoint.unsupported_features)),
        validation=validation,
        provenance={
            "input_commit": ir.commit,
            "input_license": ir.license_name,
            "input_format": ir.source_format,
            "feature_scope": ir.feature_scope,
        },
    )
    write_report(project, report)
    checkpoint.validation = validation
    resumable = report.status.value == "failed" or not validation.contract_ok
    checkpoint.phase = "validated" if resumable else "complete"
    _write_checkpoint(workspace, checkpoint)
    _copy_conversion_audit(workspace, project)

    if resumable:
        return ConversionOutcome(output=project, report=report, source_ir=ir)
    _install_output(project, output)
    shutil.rmtree(workspace, ignore_errors=True)
    return ConversionOutcome(output=output, report=report, source_ir=ir)


def validate_existing(
    project: Path,
    *,
    live: bool = True,
    proxy: str | None = None,
) -> ConversionReport:
    project = project.expanduser().resolve()
    source_json = project / "res" / "source.json"
    if not source_json.is_file():
        raise InputError(f"not an Aidoku source directory: {project}")
    data = json.loads(source_json.read_text(encoding="utf-8"))
    source_id = str(data.get("info", {}).get("id", project.name))
    validation: ValidationResult = validate_project(project, live=live, proxy=proxy)
    existing_report = project / "report.json"
    if existing_report.is_file():
        try:
            report = ConversionReport.model_validate_json(
                existing_report.read_text(encoding="utf-8")
            ).model_copy(
                update={
                    "status": classify_status(validation, live_requested=live),
                    "validation": validation,
                }
            )
        except (OSError, ValueError):
            report = ConversionReport(
                status=classify_status(validation, live_requested=live),
                input_ref=str(project),
                source_id=source_id,
                generated_files=[],
                validation=validation,
            )
    else:
        report = ConversionReport(
            status=classify_status(validation, live_requested=live),
            input_ref=str(project),
            source_id=source_id,
            generated_files=[],
            validation=validation,
        )
    write_report(project, report)
    return report
