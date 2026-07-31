from __future__ import annotations

import json
import re

from .analysis_common import input_license, match
from .constants import AIDOKU_RUNTIME_MANAGED_REQUEST_HEADERS
from .decompiled_input import DecompiledInputInspection
from .errors import InputError, UnsupportedSourceError
from .ingest import ResolvedSource, collect_source_files
from .input_capabilities import recognize_input_capabilities
from .models import (
    ChapterPageRoute,
    ChapterPageRouteVariant,
    ContentRating,
    ImageUrlPolicy,
    RequestHeaderProfile,
    RouteReplacement,
    SourceFilterOption,
    SourceFilterSpec,
    SourceIR,
    SourceMetadata,
)
from .public_only_scope import (
    public_only_filter_exclusion,
    public_only_setting_exclusions,
)
from .source_rules import (
    SOURCE_ANALYSIS_RULE_IDS,
    SOURCE_BLOCK_RULE_IDS,
    validate_rule_ids,
)


def _java_string_array(content: str) -> list[str]:
    values = []
    for raw in re.findall(r'"((?:\\.|[^"\\])*)"', content):
        try:
            values.append(json.loads(f'"{raw}"'))
        except json.JSONDecodeError:
            continue
    return values


def _java_request_header_policy(
    java: str,
) -> tuple[list[RequestHeaderProfile], dict[str, str]]:
    profiles: dict[str, dict[str, str]] = {}
    for header_match in re.finditer(
        r"(?:private\s+)?static\s+final\s+Headers\s+(?P<name>[A-Z][A-Z0-9_]*)\s*=\s*"
        r"Headers\.Companion\.of\(new String\[\]\s*\{(?P<values>[^}]+)\}\)",
        java,
    ):
        values = _java_string_array(header_match.group("values"))
        if values and len(values) % 2 == 0:
            profiles[header_match.group("name")] = {
                name: value
                for name, value in zip(values[::2], values[1::2], strict=True)
                if name.casefold() not in AIDOKU_RUNTIME_MANAGED_REQUEST_HEADERS
            }

    domains: dict[str, list[str]] = {name: [] for name in profiles}
    for domain_match in re.finditer(
        r'(?m)^\s*[A-Z][A-Z0-9_]*\("(?P<domain>(?:\\.|[^"\\])*)"[^\n;]*?'
        r"\.get(?P<profile>[A-Z][A-Z0-9_]*)\(\)",
        java,
    ):
        if domain_match.group("profile") in domains:
            domains[domain_match.group("profile")].append(domain_match.group("domain"))

    shared: dict[str, str] = {}
    for shared_match in re.finditer(
        r"this\.[A-Za-z_]\w*\s*=\s*Headers\.Companion\.of\(new String\[\]\s*"
        r"\{(?P<values>[^}]+)\}\)",
        java,
    ):
        values = _java_string_array(shared_match.group("values"))
        if values and len(values) % 2 == 0:
            shared.update(
                {
                    name: value
                    for name, value in zip(values[::2], values[1::2], strict=True)
                    if name.casefold() not in AIDOKU_RUNTIME_MANAGED_REQUEST_HEADERS
                }
            )

    return (
        [
            RequestHeaderProfile(name=name, domains=domains[name], headers=headers)
            for name, headers in sorted(profiles.items())
        ],
        shared,
    )


def _java_chapter_page_routes(java: str) -> list[ChapterPageRoute]:
    """Extract chapter-key normalization that feeds a page-content endpoint."""
    api_prefix = match(
        r"\bgetApiUrl\s*\(\s*\)\s*\{[\s\S]{0,600}?"
        r"return\s+[^;]*?\+\s*\"(/api/v\d+)\"",
        java,
    )
    endpoint_prefix = match(
        r"\bchapterContentDetailUrl\s*\([^)]*\)\s*\{[\s\S]{0,800}?"
        r"return\s+getApiUrl\(\)\s*\+\s*\"([^\"]+)\"\s*\+\s*chapterId",
        java,
    )
    fix_block = match(
        r"\bfixChapterId\s*\([^)]*\)\s*\{([\s\S]{0,1800}?)\n\s*\}",
        java,
    )
    strip_prefix = match(
        r"removePrefix\(\s*chapterId\s*,\s*\"([^\"]+)\"",
        fix_block,
    )
    replacement = re.search(
        r"replace(?:\$default)?\([^,]+,\s*\"([^\"]+)\"\s*,\s*\"([^\"]+)\"",
        fix_block,
    )
    if not (api_prefix and endpoint_prefix and strip_prefix and replacement):
        return []

    old, new = replacement.groups()
    default = ChapterPageRouteVariant(
        name="default",
        condition=(
            "selected API domain is not HotManga" if "isHotManga" in fix_block else "always"
        ),
        is_default=True,
        strip_prefix=strip_prefix,
        replacements=[RouteReplacement(old=old, new=new)],
    )
    variants = [default]
    if "isHotManga" in fix_block:
        variants.append(
            ChapterPageRouteVariant(
                name="hot_manga",
                condition="selected API domain is HotManga",
                strip_prefix=strip_prefix,
            )
        )
    return [
        ChapterPageRoute(
            source_method="chapterContentDetailUrl(fixChapterId(chapter.url))",
            chapter_key_template=f"{strip_prefix}{{comic_path}}{old}{{chapter_id}}",
            endpoint_template=f"{api_prefix}{endpoint_prefix}{{normalized_chapter_key}}",
            variants=variants,
        )
    ]


def _decode_java_string(value: str) -> str:
    try:
        decoded = json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value.replace(r"\"", '"').replace("\\\\", "\\")
    return decoded if isinstance(decoded, str) else value


def _java_image_url_policy(java: str) -> ImageUrlPolicy | None:
    found = re.search(
        r"CHAPTER_IMAGE_RESOLUTION_REGEX\s*=\s*new\s+Regex\(\s*"
        r'"((?:\\.|[^"\\])*)"\s*\)',
        java,
    )
    if found is None:
        return None
    return ImageUrlPolicy(
        preserve_cover_urls=True,
        chapter_resolution_regex=_decode_java_string(found.group(1)),
    )


def _snake_case(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def _java_filter_specs(java: str) -> list[SourceFilterSpec]:
    arrays: dict[str, list[SourceFilterOption]] = {}
    for found in re.finditer(
        r"\bTag\s*\[\]\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{([\s\S]*?)\}\s*;",
        java,
    ):
        options: list[SourceFilterOption] = []
        seen_values: set[str] = set()
        for tag in re.finditer(
            r'new\s+Tag\(\s*"((?:\\.|[^"\\])*)"\s*,\s*'
            r'"((?:\\.|[^"\\])*)"\s*\)',
            found.group(2),
        ):
            title = _decode_java_string(tag.group(1))
            value = _decode_java_string(tag.group(2))
            # Aidoku requires unique select ids. Tachi sources sometimes repeat the
            # default value under a second label, so retain the first semantic value.
            if value in seen_values:
                continue
            seen_values.add(value)
            options.append(SourceFilterOption(title=title, value=value))
        if options:
            arrays[found.group(1)] = options

    specs: list[SourceFilterSpec] = []
    for found in re.finditer(
        r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*Filter)\s+extends\s+"
        r"Filter\.(Select(?:<[^>]+>)?|Sort)\b",
        java,
    ):
        class_name = found.group(1)
        block = java[found.start() : found.start() + 3_000]
        accessor = re.search(r"FilterKt\.get([A-Za-z_][A-Za-z0-9_]*)\(\)", block)
        title_match = re.search(r'super\(\s*"((?:\\.|[^"\\])*)"', block)
        if accessor is None or title_match is None:
            continue
        array_name = accessor.group(1)
        array_name = array_name[:1].lower() + array_name[1:]
        options = arrays.get(array_name)
        if not options:
            continue
        kind = "sort" if found.group(2) == "Sort" else "select"
        default_index = 0
        default_ascending: bool | None = None
        if kind == "sort":
            default = re.search(
                r"Filter\.Sort\.Selection\(\s*(\d+)\s*,\s*(true|false)\s*\)",
                block,
            )
            if default:
                default_index = int(default.group(1))
                default_ascending = default.group(2) == "true"
        else:
            default = re.search(r"super\([\s\S]{0,800}?,\s*(\d+)\s*,\s*\d+\s*,", block)
            if default:
                default_index = int(default.group(1))
        base_name = class_name.removesuffix("Filter")
        specs.append(
            SourceFilterSpec(
                source_class=class_name,
                id=_snake_case(base_name),
                title=_decode_java_string(title_match.group(1)),
                kind=kind,
                options=options,
                default_index=min(default_index, len(options) - 1),
                default_ascending=default_ascending,
            )
        )
    return specs


def _apk_optional_features(java: str) -> list[str]:
    optional: list[str] = []
    if "AntiWatermarkInterceptor" in java:
        optional.append("anti-watermark image cleanup (excluded by public-reading APK scope)")
    if any(marker in java for marker in ("TokenProvider", '"/login"', "loginURL")):
        optional.append("login/authentication (excluded by public-only APK scope)")
    if any(marker in java for marker in ("memberCollect", "CollectResult", "CollectInfo")):
        optional.append("authenticated collection/bookcase (excluded by public-only APK scope)")
    if any(marker in java for marker in ("ChapterComment", "chapterCommentUrl", "commentList")):
        optional.append("chapter comments (excluded by public-only APK scope)")
    return optional


def analyze_decompiled_source(resolved: ResolvedSource) -> SourceIR:
    files = collect_source_files(resolved)
    inspection = DecompiledInputInspection.from_files(
        files,
        manifest=resolved.decompiled_manifest,
    )
    main_class = inspection.main_class
    parents = list(inspection.parents)
    allowed = {"HttpSource", "ConfigurableSource"}
    if any(parent not in allowed for parent in parents):
        raise UnsupportedSourceError(
            "MVP supports only APK HttpSource classes without custom source interfaces",
            rule_ids=("unsupported_custom_source_base",),
        )
    java = inspection.java
    request_header_profiles, shared_request_headers = _java_request_header_policy(java)
    recognition = recognize_input_capabilities(java, dialect="decompiled_java")

    hard_unsupported: dict[str, str] = {}
    if any(
        marker in java
        for marker in ("android.graphics.Bitmap", "Unscrambler", "descramble", "scrambleImage")
    ):
        hard_unsupported["unsupported_image_processing"] = "image decoding or scrambling"
    if recognition.unsupported_crypto:
        hard_unsupported["unsupported_crypto"] = "cryptography"
    if hard_unsupported:
        rule_ids = validate_rule_ids(
            hard_unsupported,
            SOURCE_BLOCK_RULE_IDS,
            domain="source blocker",
        )
        raise UnsupportedSourceError(
            "source is outside the APK public-only scope: " + ", ".join(hard_unsupported.values()),
            rule_ids=tuple(rule_ids),
        )

    manifest = inspection.manifest
    package_match = re.search(
        r"\.extension\.([a-z]{2,3}(?:-[a-z0-9]+)*)\.([A-Za-z0-9_]+)$",
        manifest.package,
    )
    if not package_match:
        raise InputError(
            f"unable to derive source language and id from APK package: {manifest.package}"
        )
    language, module_name = package_match.groups()
    module_slug = re.sub(r"[^a-z0-9]+", "-", module_name.lower()).strip("-")

    label = manifest.application_label
    name = match(r'\bAPP_NAME\s*=\s*"([^\"]+)"', java)
    if not name and label and not label.startswith("@"):
        name = re.sub(r"^Tachiyomi:\s*", "", label).strip()
    name = name or main_class
    base_url = match(r'\bBASE_URL\s*=\s*"(https?://[^\"]+)"', java)
    if not base_url:
        base_url = match(r'\bbaseUrl\s*=\s*"(https?://[^\"]+)"', java)
    if not base_url:
        raise InputError("unable to extract a source base URL from the decompiled APK")

    version_text = manifest.version_text
    if not version_text.isdigit():
        raise InputError(f"APK versionCode is not an integer: {version_text}")
    rating = (
        ContentRating.NSFW
        if manifest.metadata.get("tachiyomi.extension.nsfw") == "1"
        else ContentRating.SAFE
    )

    warnings = [
        "APK input was decompiled with JADX; compiler artifacts were removed and behavior "
        "must be validated against the live site",
        "APK conversion scope is public reading only; optional authenticated features are not "
        "required",
    ]
    analysis_rule_ids = list(recognition.rule_ids)
    if "addInterceptor" in java:
        warnings.append("source uses OkHttp interceptors; generated headers require live review")
        analysis_rule_ids.append("warn_okhttp_interceptor")
    if resolved.license_path is None:
        warnings.append("no input license was found beside the APK")
        analysis_rule_ids.append("warn_missing_input_license")

    license_name, license_text = input_license(resolved)
    filter_specs = _java_filter_specs(java)
    excluded_filter_features = [
        reason
        for spec in filter_specs
        if (reason := public_only_filter_exclusion(spec.source_class, spec.title)) is not None
    ]
    filter_specs = [
        spec
        for spec in filter_specs
        if public_only_filter_exclusion(spec.source_class, spec.title) is None
    ]
    unsupported_features = list(
        dict.fromkeys(
            [
                *_apk_optional_features(java),
                *excluded_filter_features,
                *public_only_setting_exclusions(java),
            ]
        )
    )
    if unsupported_features:
        analysis_rule_ids.append("exclude_public_only_features")
    relative_url_keys = "url2comicPath" in java or "setUrlWithoutDomain" in java
    if relative_url_keys:
        analysis_rule_ids.append("relative_url_keys")
    analysis_rule_ids = validate_rule_ids(
        analysis_rule_ids,
        SOURCE_ANALYSIS_RULE_IDS,
        domain="source analysis",
    )
    return SourceIR(
        input_ref=resolved.input_ref,
        commit=resolved.commit,
        source_format="decompiled_apk",
        feature_scope="public_only",
        metadata=SourceMetadata(
            source_id=f"{language}.{module_slug}",
            package_name=module_slug or main_class.lower(),
            name=name,
            language=language,
            base_url=base_url.rstrip("/"),
            version=max(1, int(version_text)),
            content_rating=rating,
        ),
        main_class=main_class,
        parent_classes=parents,
        capabilities=list(recognition.capabilities),
        method_names=list(inspection.method_names),
        header_names=list(inspection.header_names),
        request_header_profiles=request_header_profiles,
        shared_request_headers=shared_request_headers,
        relative_url_keys=relative_url_keys,
        chapter_page_routes=_java_chapter_page_routes(java),
        image_url_policy=_java_image_url_policy(java),
        filter_specs=filter_specs,
        files=files,
        license_name=license_name,
        license_text=license_text,
        warnings=warnings,
        unsupported_features=unsupported_features,
        analysis_rule_ids=analysis_rule_ids,
    )
