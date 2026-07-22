from __future__ import annotations

import json
import re
from xml.etree import ElementTree

from .errors import InputError, UnsupportedSourceError
from .ingest import ResolvedSource, collect_source_files
from .models import (
    Capability,
    ChapterPageRoute,
    ChapterPageRouteVariant,
    ContentRating,
    ImageUrlPolicy,
    RouteReplacement,
    SourceFilterOption,
    SourceFilterSpec,
    SourceIR,
    SourceMetadata,
)

_ANDROID_XML_NAMESPACE = "http://schemas.android.com/apk/res/android"


def _match(pattern: str, text: str, default: str = "") -> str:
    result = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    return result.group(1).strip() if result else default


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
    name = _match(r"(?:abstract\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)", candidate)
    if not name:
        raise InputError("unable to identify the main HttpSource class")
    inheritance = _match(
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
    warning = _match(r"contentWarning\s*=\s*ContentWarning\.([A-Z_]+)", build, "SAFE")
    if warning in {"NSFW", "MATURE"}:
        return ContentRating.NSFW
    if warning in {"MIXED", "MATURE_MIXED"}:
        return ContentRating.MIXED
    return ContentRating.SAFE


def _capabilities(kotlin: str) -> list[Capability]:
    mapping: tuple[tuple[Capability, tuple[str, ...]], ...] = (
        (Capability.SEARCH, ("searchManga", "getSearchMangaList")),
        (Capability.POPULAR, ("popularManga", "getPopularManga")),
        (Capability.LATEST, ("latestUpdates", "getLatestUpdates")),
        (Capability.DETAILS, ("mangaDetails", "getMangaDetails", "fetchMangaUpdate")),
        (Capability.CHAPTERS, ("chapterList", "fetchAllChapters")),
        (Capability.PAGES, ("pageList", "getPageList")),
        (Capability.FILTERS, ("getFilterList",)),
        (Capability.DYNAMIC_FILTERS, ("resetThemeFilter", "theme/comic/count")),
        (Capability.SETTINGS, ("setupPreferenceScreen", "ConfigurableSource")),
        (Capability.IMAGE_HEADERS, ("imageRequest", "headersBuilder")),
        (
            Capability.JSON_API,
            ("parseAs<", "decodeFromString<", "get_json_owned", "application/json"),
        ),
        (
            Capability.DYNAMIC_BASE_URLS,
            ("API_DOMAINS", "apiDomains", "domainPreference", "baseUrlPreference"),
        ),
    )
    capabilities = [
        capability for capability, markers in mapping if any(x in kotlin for x in markers)
    ]
    if _uses_supported_aes_cbc(kotlin):
        capabilities.append(Capability.ENCRYPTED_JSON)
    return capabilities


def _uses_supported_aes_cbc(kotlin: str) -> bool:
    transformations = re.findall(r'Cipher\.getInstance\(\s*"([^"]+)"', kotlin)
    if not transformations:
        return False
    supported = {"AES/CBC/PKCS5Padding", "AES/CBC/PKCS7Padding"}
    return (
        all(transformation in supported for transformation in transformations)
        and "SecretKeySpec" in kotlin
        and "IvParameterSpec" in kotlin
    )


def _uses_relative_url_keys(kotlin: str) -> bool:
    return "setUrlWithoutDomain" in kotlin or bool(re.search(r"\burl\s*=\s*\"/", kotlin))


def _unsupported_features(build: str, kotlin: str) -> list[str]:
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
            "addInterceptor",
        ),
    }
    combined = build + "\n" + kotlin
    unsupported = [
        name for name, markers in checks.items() if any(marker in combined for marker in markers)
    ]
    crypto_markers = ("javax.crypto", "Cipher.getInstance", "SecretKeySpec")
    if any(marker in combined for marker in crypto_markers) and not _uses_supported_aes_cbc(kotlin):
        unsupported.append("cryptography")
    return unsupported


def _java_main_class(java: str) -> tuple[str, list[str]]:
    match = re.search(
        r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\s+extends\s+([A-Za-z0-9_.$]+)"
        r"(?:\s+implements\s+([^\{]+))?\s*\{",
        java,
    )
    if not match or match.group(2).rsplit(".", 1)[-1] != "HttpSource":
        raise UnsupportedSourceError("MVP supports only decompiled standalone HttpSource APKs")
    parents = ["HttpSource"]
    if match.group(3):
        parents.extend(
            item.strip().rsplit(".", 1)[-1] for item in match.group(3).split(",") if item.strip()
        )
    allowed = {"HttpSource", "ConfigurableSource"}
    if any(parent not in allowed for parent in parents):
        raise UnsupportedSourceError(
            "MVP supports only APK HttpSource classes without custom source interfaces"
        )
    return match.group(1), parents


def _java_capabilities(java: str) -> list[Capability]:
    mapping: tuple[tuple[Capability, tuple[str, ...]], ...] = (
        (Capability.SEARCH, ("searchMangaRequest", "searchMangaParse")),
        (Capability.POPULAR, ("popularMangaRequest", "popularMangaParse")),
        (Capability.LATEST, ("latestUpdatesRequest", "latestUpdatesParse")),
        (
            Capability.DETAILS,
            ("mangaDetailsRequest", "mangaDetailsParse", "fetchMangaDetails"),
        ),
        (Capability.CHAPTERS, ("fetchChapterList", "chapterListParse")),
        (Capability.PAGES, ("fetchPageList", "pageListParse")),
        (Capability.FILTERS, ("getFilterList",)),
        (Capability.DYNAMIC_FILTERS, ("resetThemeFilter", "/theme/comic/count")),
        (Capability.SETTINGS, ("setupPreferenceScreen", "ConfigurableSource")),
        (Capability.IMAGE_HEADERS, ("imageRequest", "getImageRequest", "headersBuilder")),
        (Capability.DEEP_LINKS, ("getMangaUrl", "getChapterUrl")),
        (
            Capability.JSON_API,
            ("decodeFromString", "ApiResponse", "application/json", "Json.INSTANCE"),
        ),
        (
            Capability.DYNAMIC_BASE_URLS,
            ("ApiDomainOption", "getApiDomain", "getApiUrl", "KEY_CUSTOM"),
        ),
    )
    capabilities = [
        capability for capability, markers in mapping if any(marker in java for marker in markers)
    ]
    if _uses_supported_aes_cbc(java):
        capabilities.append(Capability.ENCRYPTED_JSON)
    return list(dict.fromkeys(capabilities))


def _java_header_names(java: str) -> list[str]:
    names = set(
        re.findall(
            r"\.(?:add|set|header|addHeader|setHeader)\(\s*\"([^\"]+)\"\s*,",
            java,
        )
    )
    blocks = re.findall(r"Headers\.Companion\.of\(new String\[\]\s*\{([^}]+)\}\)", java)
    for block in blocks:
        values = re.findall(r'"([^\"]+)"', block)
        names.update(values[::2])
    return sorted(names)


def _java_method_names(main_java: str) -> list[str]:
    return sorted(
        set(
            re.findall(
                r"^\s*(?:public|protected)\s+(?:static\s+)?(?:final\s+)?"
                r"[A-Za-z0-9_.$<>?, \[\]]+\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\(",
                main_java,
                re.MULTILINE,
            )
        )
    )


def _java_chapter_page_routes(java: str) -> list[ChapterPageRoute]:
    """Extract chapter-key normalization that feeds a page-content endpoint."""
    api_prefix = _match(
        r"\bgetApiUrl\s*\(\s*\)\s*\{[\s\S]{0,600}?"
        r"return\s+[^;]*?\+\s*\"(/api/v\d+)\"",
        java,
    )
    endpoint_prefix = _match(
        r"\bchapterContentDetailUrl\s*\([^)]*\)\s*\{[\s\S]{0,800}?"
        r"return\s+getApiUrl\(\)\s*\+\s*\"([^\"]+)\"\s*\+\s*chapterId",
        java,
    )
    fix_block = _match(
        r"\bfixChapterId\s*\([^)]*\)\s*\{([\s\S]{0,1800}?)\n\s*\}",
        java,
    )
    strip_prefix = _match(
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
    match = re.search(
        r"CHAPTER_IMAGE_RESOLUTION_REGEX\s*=\s*new\s+Regex\(\s*"
        r'"((?:\\.|[^"\\])*)"\s*\)',
        java,
    )
    if match is None:
        return None
    return ImageUrlPolicy(
        preserve_cover_urls=True,
        chapter_resolution_regex=_decode_java_string(match.group(1)),
    )


def _snake_case(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def _java_filter_specs(java: str) -> list[SourceFilterSpec]:
    arrays: dict[str, list[SourceFilterOption]] = {}
    for match in re.finditer(
        r"\bTag\s*\[\]\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\{([\s\S]*?)\}\s*;",
        java,
    ):
        options: list[SourceFilterOption] = []
        seen_values: set[str] = set()
        for tag in re.finditer(
            r'new\s+Tag\(\s*"((?:\\.|[^"\\])*)"\s*,\s*'
            r'"((?:\\.|[^"\\])*)"\s*\)',
            match.group(2),
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
            arrays[match.group(1)] = options

    specs: list[SourceFilterSpec] = []
    for match in re.finditer(
        r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*Filter)\s+extends\s+"
        r"Filter\.(Select(?:<[^>]+>)?|Sort)\b",
        java,
    ):
        class_name = match.group(1)
        block = java[match.start() : match.start() + 3_000]
        accessor = re.search(r"FilterKt\.get([A-Za-z_][A-Za-z0-9_]*)\(\)", block)
        title_match = re.search(r'super\(\s*"((?:\\.|[^"\\])*)"', block)
        if accessor is None or title_match is None:
            continue
        array_name = accessor.group(1)
        array_name = array_name[:1].lower() + array_name[1:]
        options = arrays.get(array_name)
        if not options:
            continue
        kind = "sort" if match.group(2) == "Sort" else "select"
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


def _manifest_metadata(manifest: str) -> tuple[ElementTree.Element, dict[str, str]]:
    try:
        root = ElementTree.fromstring(manifest)
    except ElementTree.ParseError as exc:
        raise InputError(f"unable to parse decompiled AndroidManifest.xml: {exc}") from exc
    android_name = f"{{{_ANDROID_XML_NAMESPACE}}}name"
    android_value = f"{{{_ANDROID_XML_NAMESPACE}}}value"
    values = {
        item.get(android_name, ""): item.get(android_value, "")
        for item in root.findall("./application/meta-data")
    }
    return root, values


def _apk_optional_features(java: str) -> list[str]:
    optional: list[str] = []
    if any(marker in java for marker in ("TokenProvider", '"/login"', "loginURL")):
        optional.append("login/authentication (excluded by public-only APK scope)")
    if any(marker in java for marker in ("memberCollect", "CollectResult", "CollectInfo")):
        optional.append("authenticated collection/bookcase (excluded by public-only APK scope)")
    if any(marker in java for marker in ("ChapterComment", "chapterCommentUrl", "commentList")):
        optional.append("chapter comments (excluded by public-only APK scope)")
    return optional


def _analyze_decompiled_apk(resolved: ResolvedSource) -> SourceIR:
    files = collect_source_files(resolved)
    manifest_file = next(
        (item for item in files if item.path == "resources/AndroidManifest.xml"), None
    )
    if manifest_file is None:
        raise InputError("decompiled APK manifest was not collected")
    java_files = [item for item in files if item.path.endswith(".java")]
    main_file = next(
        (
            item
            for item in java_files
            if re.search(r"\bclass\s+\w+\s+extends\s+HttpSource\b", item.content)
        ),
        None,
    )
    if main_file is None:
        raise UnsupportedSourceError("decompiled APK contains no standalone HttpSource class")
    main_class, parents = _java_main_class(main_file.content)
    java = "\n\n".join(item.content for item in java_files)

    hard_unsupported: list[str] = []
    if any(
        marker in java
        for marker in ("android.graphics.Bitmap", "Unscrambler", "descramble", "scrambleImage")
    ):
        hard_unsupported.append("image decoding or scrambling")
    crypto_markers = ("javax.crypto", "Cipher.getInstance", "SecretKeySpec")
    if any(marker in java for marker in crypto_markers) and not _uses_supported_aes_cbc(java):
        hard_unsupported.append("cryptography")
    if hard_unsupported:
        raise UnsupportedSourceError(
            "source is outside the APK public-only scope: " + ", ".join(hard_unsupported)
        )

    root, manifest_values = _manifest_metadata(manifest_file.content)
    package = root.get("package", "")
    package_match = re.search(
        r"\.extension\.([a-z]{2,3}(?:-[a-z0-9]+)*)\.([A-Za-z0-9_]+)$", package
    )
    if not package_match:
        raise InputError(f"unable to derive source language and id from APK package: {package}")
    language, module_name = package_match.groups()
    module_slug = re.sub(r"[^a-z0-9]+", "-", module_name.lower()).strip("-")

    application = root.find("./application")
    android_label = f"{{{_ANDROID_XML_NAMESPACE}}}label"
    label = application.get(android_label, "") if application is not None else ""
    name = _match(r'\bAPP_NAME\s*=\s*"([^\"]+)"', java)
    if not name and label and not label.startswith("@"):
        name = re.sub(r"^Tachiyomi:\s*", "", label).strip()
    name = name or main_class
    base_url = _match(r'\bBASE_URL\s*=\s*"(https?://[^\"]+)"', java)
    if not base_url:
        base_url = _match(r'\bbaseUrl\s*=\s*"(https?://[^\"]+)"', java)
    if not base_url:
        raise InputError("unable to extract a source base URL from the decompiled APK")

    android_version = f"{{{_ANDROID_XML_NAMESPACE}}}versionCode"
    version_text = root.get(android_version, "1")
    if not version_text.isdigit():
        raise InputError(f"APK versionCode is not an integer: {version_text}")
    rating = (
        ContentRating.NSFW
        if manifest_values.get("tachiyomi.extension.nsfw") == "1"
        else ContentRating.SAFE
    )

    warnings = [
        "APK input was decompiled with JADX; compiler artifacts were removed and behavior "
        "must be validated against the live site",
        "APK conversion scope is public reading only; optional authenticated features are not "
        "required",
    ]
    if "addInterceptor" in java:
        warnings.append("source uses OkHttp interceptors; generated headers require live review")
    if resolved.license_path is None:
        warnings.append("no input license was found beside the APK")

    license_name = resolved.license_path.name if resolved.license_path else None
    license_text = (
        resolved.license_path.read_text(encoding="utf-8", errors="replace")
        if resolved.license_path
        else None
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
        capabilities=_java_capabilities(java),
        method_names=_java_method_names(main_file.content),
        header_names=_java_header_names(java),
        relative_url_keys=("url2comicPath" in java or "setUrlWithoutDomain" in java),
        chapter_page_routes=_java_chapter_page_routes(java),
        image_url_policy=_java_image_url_policy(java),
        filter_specs=_java_filter_specs(java),
        files=files,
        license_name=license_name,
        license_text=license_text,
        warnings=warnings,
        unsupported_features=_apk_optional_features(java),
    )


def analyze_source(resolved: ResolvedSource) -> SourceIR:
    if resolved.source_format == "decompiled_apk":
        return _analyze_decompiled_apk(resolved)
    files = collect_source_files(resolved)
    build_file = next((item for item in files if item.path == "build.gradle.kts"), None)
    if build_file is None:
        raise InputError("build.gradle.kts was not collected")
    kotlin = "\n\n".join(item.content for item in files if item.path.endswith(".kt"))
    main_class, parents = _parse_main_class(kotlin)
    unsupported = _unsupported_features(build_file.content, kotlin)
    if unsupported:
        raise UnsupportedSourceError("source is outside the MVP scope: " + ", ".join(unsupported))

    module_slug = re.sub(r"[^a-z0-9]+", "-", resolved.module_path.name.lower()).strip("-")
    language = _match(r"\blang\s*=\s*\"([^\"]+)\"", build_file.content, "multi")
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]+)*", language):
        raise InputError(f"unsupported or invalid source language code: {language}")
    name = _match(r"\bname\s*=\s*\"([^\"]+)\"", build_file.content, main_class)
    base_url = _match(r"\bbaseUrl\s*=\s*\"([^\"]+)\"", build_file.content)
    if not base_url:
        base_url = _match(r"\bcustom\(\s*\"([^\"]+)\"", build_file.content)
    if not base_url:
        base_url = _match(r"override\s+val\s+baseUrl\s*=\s*\"([^\"]+)\"", kotlin)
    if not base_url:
        raise InputError("unable to extract a source base URL")
    version_text = _match(r"\bversionCode\s*=\s*(\d+)", build_file.content, "1")

    method_names = sorted(set(re.findall(r"override\s+(?:suspend\s+)?fun\s+(\w+)", kotlin)))
    header_names_set = set(
        re.findall(r"\.(?:add|set)\(\s*\"([^\"]+)\"\s*,", kotlin)
    )
    if "super.headersBuilder()" in kotlin:
        header_names_set.add("User-Agent")
    header_names = sorted(header_names_set)
    warnings: list[str] = []
    if "addInterceptor" in kotlin or "addNetworkInterceptor" in kotlin:
        warnings.append(
            "source uses OkHttp interceptors; generated behavior requires manual review"
        )

    license_text = None
    license_name = None
    if resolved.license_path:
        license_name = resolved.license_path.name
        license_text = resolved.license_path.read_text(encoding="utf-8", errors="replace")

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
        capabilities=_capabilities(kotlin),
        method_names=method_names,
        header_names=header_names,
        relative_url_keys=_uses_relative_url_keys(kotlin),
        files=files,
        license_name=license_name,
        license_text=license_text,
        warnings=warnings,
        unsupported_features=[],
    )


def analyze_path(input_ref: str) -> SourceIR:
    from .ingest import resolve_source

    with resolve_source(input_ref) as resolved:
        return analyze_source(resolved)
