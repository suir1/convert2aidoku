from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .models import Capability

type InputDialect = Literal["kotlin", "decompiled_java"]
type _Markers = tuple[tuple[Capability, tuple[str, ...]], ...]

_COMMON_MARKERS: _Markers = (
    (Capability.FILTERS, ("getFilterList",)),
    (Capability.DYNAMIC_FILTERS, ("resetThemeFilter",)),
    (Capability.SETTINGS, ("setupPreferenceScreen", "ConfigurableSource")),
    (Capability.IMAGE_HEADERS, ("imageRequest", "headersBuilder")),
    (Capability.DEEP_LINKS, ("getMangaUrl", "getChapterUrl")),
    (Capability.JSON_API, ("decodeFromString", "application/json")),
)
_DIALECT_MARKERS: dict[InputDialect, _Markers] = {
    "kotlin": (
        (Capability.SEARCH, ("searchManga", "getSearchMangaList")),
        (Capability.POPULAR, ("popularManga", "getPopularManga")),
        (Capability.LATEST, ("latestUpdates", "getLatestUpdates")),
        (Capability.DETAILS, ("mangaDetails", "getMangaDetails", "fetchMangaUpdate")),
        (Capability.CHAPTERS, ("chapterList", "fetchAllChapters")),
        (Capability.PAGES, ("pageList", "getPageList")),
        (Capability.DYNAMIC_FILTERS, ("theme/comic/count",)),
        (Capability.JSON_API, ("parseAs<", "get_json_owned")),
        (
            Capability.DYNAMIC_BASE_URLS,
            ("API_DOMAINS", "apiDomains", "domainPreference", "baseUrlPreference"),
        ),
    ),
    "decompiled_java": (
        (Capability.SEARCH, ("searchMangaRequest", "searchMangaParse")),
        (Capability.POPULAR, ("popularMangaRequest", "popularMangaParse")),
        (Capability.LATEST, ("latestUpdatesRequest", "latestUpdatesParse")),
        (
            Capability.DETAILS,
            ("mangaDetailsRequest", "mangaDetailsParse", "fetchMangaDetails"),
        ),
        (Capability.CHAPTERS, ("fetchChapterList", "chapterListParse")),
        (Capability.PAGES, ("fetchPageList", "pageListParse")),
        (Capability.DYNAMIC_FILTERS, ("/theme/comic/count",)),
        (Capability.IMAGE_HEADERS, ("getImageRequest",)),
        (Capability.JSON_API, ("ApiResponse", "Json.INSTANCE")),
        (
            Capability.DYNAMIC_BASE_URLS,
            ("ApiDomainOption", "getApiDomain", "getApiUrl", "KEY_CUSTOM"),
        ),
    ),
}
_CRYPTO_MARKERS = (
    "javax.crypto",
    "Cipher.getInstance",
    "SecretKeySpec",
    "MessageDigest",
)
_CRYPTO_TRANSFORMATIONS = {
    Capability.ENCRYPTED_JSON: frozenset({"AES/CBC/PKCS5Padding", "AES/CBC/PKCS7Padding"}),
    Capability.TRIPLE_DES_CBC: frozenset({"DESede/CBC/PKCS5Padding", "DESede/CBC/PKCS7Padding"}),
}


@dataclass(frozen=True)
class InputCapabilityRecognition:
    capabilities: tuple[Capability, ...]
    unsupported_crypto: bool


def _crypto_capabilities(content: str) -> tuple[set[Capability], bool]:
    """Recognize only crypto constructions with a pinned Rust implementation path."""
    capabilities: set[Capability] = set()
    unsupported = False
    transformations = frozenset(re.findall(r'Cipher\.getInstance\(\s*"([^"]+)"', content))
    remaining = set(transformations)

    symmetric = [
        capability
        for capability, supported in _CRYPTO_TRANSFORMATIONS.items()
        if transformations.intersection(supported)
    ]
    if symmetric:
        if len(symmetric) == 1 and "SecretKeySpec" in content and "IvParameterSpec" in content:
            capability = symmetric[0]
            capabilities.add(capability)
            remaining.difference_update(_CRYPTO_TRANSFORMATIONS[capability])
        else:
            unsupported = True

    rsa_transformations = {"RSA/ECB/PKCS1Padding"}
    if remaining.intersection(rsa_transformations):
        if 'KeyFactory.getInstance("RSA")' in content and "X509EncodedKeySpec" in content:
            capabilities.add(Capability.RSA_PKCS1_V15)
            remaining.difference_update(rsa_transformations)
        else:
            unsupported = True
    if remaining:
        unsupported = True

    digest_names = set(
        re.findall(
            r'MessageDigest\s*\.\s*getInstance\(\s*"([^"]+)"',
            content,
        )
    )
    if re.search(r"MessageDigest\s*\.\s*getInstance\(\s*type\s*\)", content):
        digest_names.update(re.findall(r'hashString\(\s*"([^"]+)"', content))
    if digest_names:
        if digest_names == {"MD5"}:
            capabilities.add(Capability.MD5_REQUEST_SIGNING)
        else:
            unsupported = True

    has_crypto_markers = any(marker in content for marker in _CRYPTO_MARKERS)
    if has_crypto_markers and not capabilities:
        unsupported = True
    return capabilities, unsupported


def recognize_input_capabilities(
    content: str,
    *,
    dialect: InputDialect,
) -> InputCapabilityRecognition:
    detected = {
        capability
        for capability, markers in (*_COMMON_MARKERS, *_DIALECT_MARKERS[dialect])
        if any(marker in content for marker in markers)
    }
    if dialect == "kotlin":
        if re.search(r"\bsupportsLatest\s*=\s*false\b", content):
            detected.discard(Capability.LATEST)
        if (
            "allCategory" in content
            and re.search(r"\bvar\s+categories\s*:", content)
            and "getFilterList" in content
        ):
            detected.add(Capability.DYNAMIC_FILTERS)

    crypto_capabilities, unsupported_crypto = _crypto_capabilities(content)
    detected.update(crypto_capabilities)
    return InputCapabilityRecognition(
        capabilities=tuple(capability for capability in Capability if capability in detected),
        unsupported_crypto=unsupported_crypto,
    )
