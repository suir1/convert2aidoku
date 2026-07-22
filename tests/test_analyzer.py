import shutil
from pathlib import Path

import pytest

from convert2aidoku import ingest
from convert2aidoku.analyzer import _uses_relative_url_keys, analyze_path
from convert2aidoku.errors import UnsupportedSourceError
from convert2aidoku.models import Capability, ContentRating

FIXTURE = Path(__file__).parent / "fixtures" / "simple"
ENCRYPTED_API_FIXTURE = Path(__file__).parent / "fixtures" / "encrypted_api"
DECOMPILED_APK_FIXTURE = Path(__file__).parent / "fixtures" / "decompiled_apk"


def test_analyzes_standard_http_source() -> None:
    ir = analyze_path(str(FIXTURE))

    assert ir.metadata.source_id == "en.simple"
    assert ir.metadata.name == "Simple Source"
    assert ir.metadata.base_url == "https://example.com"
    assert ir.metadata.version == 7
    assert ir.metadata.content_rating is ContentRating.SAFE
    assert ir.main_class == "Simple"
    assert "HttpSource" in ir.parent_classes
    assert Capability.SEARCH in ir.capabilities
    assert Capability.DETAILS in ir.capabilities
    assert Capability.CHAPTERS in ir.capabilities
    assert Capability.PAGES in ir.capabilities
    assert "Referer" in ir.header_names
    assert "User-Agent" in ir.header_names
    assert ir.license_text and ir.license_text.strip() == "Synthetic fixture license."


def test_rejects_crypto_source(tmp_path: Path) -> None:
    (tmp_path / "build.gradle.kts").write_text(
        'keiyoushi { name = "X"; versionCode = 1; source { lang = "en"; baseUrl = "https://x" } }'
    )
    source = tmp_path / "src" / "X.kt"
    source.parent.mkdir()
    source.write_text(
        'abstract class X : HttpSource() { val cipher = javax.crypto.Cipher.getInstance("AES") }'
    )

    with pytest.raises(UnsupportedSourceError, match="cryptography"):
        analyze_path(str(tmp_path))


def test_analyzes_supported_encrypted_json_api() -> None:
    ir = analyze_path(str(ENCRYPTED_API_FIXTURE))

    assert Capability.JSON_API in ir.capabilities
    assert Capability.ENCRYPTED_JSON in ir.capabilities
    assert Capability.DYNAMIC_BASE_URLS in ir.capabilities
    assert not ir.unsupported_features


def test_detects_relative_manga_and_chapter_keys() -> None:
    assert _uses_relative_url_keys('manga.setUrlWithoutDomain(a.absUrl("href"))')
    assert _uses_relative_url_keys('chapter.url = "/chapters/${item.id}"')
    assert not _uses_relative_url_keys('manga.url = "https://example.com/comics/1"')


def test_analyzes_decompiled_apk_as_public_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apk = tmp_path / "copymanga.apk"
    apk.write_bytes(b"synthetic apk")

    def fake_jadx(_apk: Path, destination: Path) -> None:
        shutil.copytree(DECOMPILED_APK_FIXTURE, destination)

    monkeypatch.setattr(ingest, "_run_jadx", fake_jadx)
    ir = analyze_path(str(apk))

    assert ir.source_format == "decompiled_apk"
    assert ir.feature_scope == "public_only"
    assert ir.metadata.source_id == "zh.copymanga"
    assert ir.metadata.name == "拷贝漫画"
    assert ir.metadata.base_url == "https://www.copy3000.com"
    assert ir.metadata.version == 82
    assert ir.metadata.content_rating is ContentRating.NSFW
    assert Capability.SEARCH in ir.capabilities
    assert Capability.POPULAR in ir.capabilities
    assert Capability.LATEST in ir.capabilities
    assert Capability.DETAILS in ir.capabilities
    assert Capability.CHAPTERS in ir.capabilities
    assert Capability.PAGES in ir.capabilities
    assert Capability.FILTERS in ir.capabilities
    assert Capability.DYNAMIC_FILTERS in ir.capabilities
    assert Capability.SETTINGS in ir.capabilities
    assert Capability.DEEP_LINKS in ir.capabilities
    assert Capability.JSON_API in ir.capabilities
    assert Capability.DYNAMIC_BASE_URLS in ir.capabilities
    assert ir.relative_url_keys
    assert len(ir.chapter_page_routes) == 1
    route = ir.chapter_page_routes[0]
    assert route.chapter_key_template == "/comic/{comic_path}/chapter/{chapter_id}"
    assert route.endpoint_template == "/api/v3/comic/{normalized_chapter_key}"
    assert route.variants[0].is_default
    assert route.variants[0].strip_prefix == "/comic/"
    assert route.variants[0].replacements[0].old == "/chapter/"
    assert route.variants[0].replacements[0].new == "/chapter2/"
    assert route.variants[1].name == "hot_manga"
    assert not route.variants[1].replacements
    assert ir.image_url_policy is not None
    assert ir.image_url_policy.preserve_cover_urls
    assert ir.image_url_policy.chapter_resolution_regex == r"\d+(?=x\.(?:jpg|webp)$)"
    audience = next(item for item in ir.filter_specs if item.id == "audience")
    assert audience.kind == "select"
    assert audience.default_index == 0
    assert [(item.title, item.value) for item in audience.options] == [
        ("默認(男頻)", "male"),
        ("女频", "female"),
    ]
    sort = next(item for item in ir.filter_specs if item.id == "sort")
    assert sort.kind == "sort"
    assert sort.default_index == 1
    assert sort.default_ascending is True
    assert {"Accept", "Origin", "Version"} <= set(ir.header_names)
    assert any("login/authentication" in item for item in ir.unsupported_features)
    assert any("collection/bookcase" in item for item in ir.unsupported_features)
    assert any("chapter comments" in item for item in ir.unsupported_features)
