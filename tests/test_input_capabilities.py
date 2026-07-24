from __future__ import annotations

import pytest

from convert2aidoku.input_capabilities import InputDialect, recognize_input_capabilities
from convert2aidoku.models import Capability


def test_recognition_maps_kotlin_and_decompiled_java_dialects() -> None:
    kotlin = recognize_input_capabilities(
        "getSearchMangaList getMangaDetails pageList apiDomains",
        dialect="kotlin",
    )
    java = recognize_input_capabilities(
        "searchMangaRequest mangaDetailsParse fetchPageList getApiDomain",
        dialect="decompiled_java",
    )

    expected = {
        Capability.SEARCH,
        Capability.DETAILS,
        Capability.PAGES,
        Capability.DYNAMIC_BASE_URLS,
    }
    assert set(kotlin.capabilities) == expected
    assert set(java.capabilities) == expected


def test_kotlin_recognition_honors_latest_override_and_dynamic_filter_shape() -> None:
    recognition = recognize_input_capabilities(
        "latestUpdates supportsLatest = false var categories: List<X> allCategory getFilterList",
        dialect="kotlin",
    )

    assert Capability.LATEST not in recognition.capabilities
    assert Capability.FILTERS in recognition.capabilities
    assert Capability.DYNAMIC_FILTERS in recognition.capabilities


@pytest.mark.parametrize(
    ("transformation", "capability"),
    [
        ("AES/CBC/PKCS5Padding", Capability.ENCRYPTED_JSON),
        ("DESede/CBC/PKCS7Padding", Capability.TRIPLE_DES_CBC),
    ],
)
@pytest.mark.parametrize("dialect", ["kotlin", "decompiled_java"])
def test_recognition_shares_supported_crypto_policy_across_dialects(
    transformation: str,
    capability: Capability,
    dialect: InputDialect,
) -> None:
    recognition = recognize_input_capabilities(
        f'javax.crypto.Cipher.getInstance("{transformation}"); '
        "new SecretKeySpec(key); new IvParameterSpec(iv);",
        dialect=dialect,
    )

    assert capability in recognition.capabilities
    assert not recognition.unsupported_crypto


@pytest.mark.parametrize(
    "content",
    [
        'javax.crypto.Cipher.getInstance("AES/GCM/NoPadding"); '
        "new SecretKeySpec(key); new IvParameterSpec(iv);",
        'javax.crypto.Cipher.getInstance("DESede/CBC/PKCS5Padding"); new SecretKeySpec(key);',
        'javax.crypto.Cipher.getInstance("AES/CBC/PKCS5Padding"); '
        'javax.crypto.Cipher.getInstance("DESede/CBC/PKCS5Padding"); '
        "new SecretKeySpec(key); new IvParameterSpec(iv);",
    ],
)
def test_recognition_rejects_unknown_incomplete_or_mixed_crypto(content: str) -> None:
    recognition = recognize_input_capabilities(content, dialect="decompiled_java")

    assert recognition.unsupported_crypto
    assert Capability.ENCRYPTED_JSON not in recognition.capabilities
    assert Capability.TRIPLE_DES_CBC not in recognition.capabilities
