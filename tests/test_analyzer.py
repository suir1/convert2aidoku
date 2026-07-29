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


def test_analyzes_supported_triple_des_request_signing(tmp_path: Path) -> None:
    (tmp_path / "build.gradle.kts").write_text(
        'keiyoushi { name = "V"; versionCode = 1; source { lang = "zh"; '
        'baseUrl { custom("https://v.example") } } }'
    )
    source = tmp_path / "src" / "V.kt"
    source.parent.mkdir()
    source.write_text(
        """
        abstract class V : HttpSource() {
            fun sign() {
                val key = javax.crypto.spec.SecretKeySpec(ByteArray(24), "DESede")
                val iv = javax.crypto.spec.IvParameterSpec(ByteArray(8))
                javax.crypto.Cipher.getInstance("DESede/CBC/PKCS5Padding")
            }
            override fun pageListRequest(chapter: SChapter): Request = TODO()
        }
        """
    )

    ir = analyze_path(str(tmp_path))

    assert Capability.TRIPLE_DES_CBC in ir.capabilities
    assert not ir.unsupported_features


def test_omits_explicitly_unsupported_latest_but_detects_deep_links(tmp_path: Path) -> None:
    (tmp_path / "build.gradle.kts").write_text(
        'keiyoushi { name = "Links"; source { lang = "zh"; '
        'baseUrl { custom("https://links.example") } } }'
    )
    source = tmp_path / "src" / "Links.kt"
    source.parent.mkdir()
    source.write_text(
        """
        abstract class Links : HttpSource() {
            override val supportsLatest = false
            override fun latestUpdatesRequest(page: Int) = throw UnsupportedOperationException()
            override fun getMangaUrl(manga: SManga) = "$baseUrl/detail/${manga.url}"
            override fun getChapterUrl(chapter: SChapter) = "$baseUrl/read/${chapter.url}"
        }
        """
    )

    ir = analyze_path(str(tmp_path))

    assert Capability.LATEST not in ir.capabilities
    assert Capability.DEEP_LINKS in ir.capabilities


def test_analyzes_legacy_groovy_module_metadata(tmp_path: Path) -> None:
    module = tmp_path / "legacy"
    module.mkdir()
    (module / "build.gradle").write_text(
        "ext { extName = 'Legacy'; extClass = '.Legacy'; extVersionCode = 4; isNsfw = true }"
    )
    source = module / "src" / "Legacy.kt"
    source.parent.mkdir()
    source.write_text(
        """
        class Legacy : HttpSource() {
            override val name = "Legacy Kotlin"
            override val lang = "zh"
            override val baseUrl = "https://legacy.example"
            override fun popularMangaRequest(page: Int): Request = TODO()
        }
        """
    )

    ir = analyze_path(str(module))

    assert ir.metadata.source_id == "zh.legacy"
    assert ir.metadata.name == "Legacy"
    assert ir.metadata.version == 4
    assert ir.metadata.content_rating is ContentRating.NSFW
    assert ir.metadata.base_url == "https://legacy.example"


def test_detects_relative_manga_and_chapter_keys() -> None:
    assert _uses_relative_url_keys('manga.setUrlWithoutDomain(a.absUrl("href"))')
    assert _uses_relative_url_keys('chapter.url = "/chapters/${item.id}"')
    assert _uses_relative_url_keys('val url get() = "/comic/$id"')
    assert not _uses_relative_url_keys('manga.url = "https://example.com/comics/1"')


def test_recovers_kotlin_select_filter_site_values(tmp_path: Path) -> None:
    (tmp_path / "build.gradle.kts").write_text(
        'keiyoushi { name = "Filters"; source { lang = "zh"; '
        'baseUrl { custom("https://filters.example") } } }'
    )
    source = tmp_path / "src" / "Filters.kt"
    source.parent.mkdir()
    source.write_text(
        """
        class Filters : HttpSource() {
            override fun getFilterList() = FilterList(SortFilter(), StatusFilter())
        }
        interface SiteFilter { fun apply(variables: Variables) }
        class SortFilter :
            Filter.Select<String>("排序", arrayOf("更新", "觀看數")), SiteFilter {
            override fun apply(variables: Variables) {
                variables.order = arrayOf(OrderBy.DATE_UPDATED, OrderBy.VIEWS)[state]
            }
        }
        class StatusFilter :
            Filter.Select<String>("狀態", arrayOf("全部", "完結")), SiteFilter {
            override fun apply(variables: Variables) {
                variables.status = arrayOf("", "END")[state]
            }
        }
        """
    )

    ir = analyze_path(str(tmp_path))

    assert [(spec.id, spec.kind) for spec in ir.filter_specs] == [
        ("sort", "select"),
        ("status", "select"),
    ]
    assert [option.value for option in ir.filter_specs[0].options] == [
        "DATE_UPDATED",
        "VIEWS",
    ]
    assert [option.value for option in ir.filter_specs[1].options] == ["", "END"]


def test_recovers_kotlin_uri_part_filter_pairs_and_constants(tmp_path: Path) -> None:
    (tmp_path / "build.gradle.kts").write_text(
        'keiyoushi { name = "Filters"; source { lang = "zh"; '
        'baseUrl { custom("https://filters.example") } } }'
    )
    source = tmp_path / "src" / "Filters.kt"
    source.parent.mkdir()
    source.write_text(
        """
        class Filters : HttpSource() {
            override fun getFilterList() = FilterList(SortFilter(0), RegionFilter())
        }

        open class UriPartFilter(
            val key: String,
            name: String,
            private val pairs: List<Pair<String, String>>,
            state: Int = 0,
        ) : Filter.Select<String>(name, pairs.map { it.first }.toTypedArray(), state)

        class SortFilter(state: Int) :
            UriPartFilter(
                "sort",
                "排序",
                listOf(
                    "最新" to "",
                    "日排行" to RANK_PREFIX,
                    "週排行" to "$RANK_PREFIX-week",
                ),
                state,
            ) {
            companion object { const val RANK_PREFIX = "rank|" }
        }

        class RegionFilter :
            UriPartFilter(
                "filter[country]",
                "作品地区",
                listOf("所有" to "", "日本" to "japan"),
            )
        """
    )

    ir = analyze_path(str(tmp_path))

    assert [(spec.id, spec.kind) for spec in ir.filter_specs] == [
        ("sort", "select"),
        ("filter[country]", "select"),
    ]
    assert [(item.title, item.value) for item in ir.filter_specs[0].options] == [
        ("最新", ""),
        ("日排行", "rank|"),
        ("週排行", "rank|-week"),
    ]
    assert [(item.title, item.value) for item in ir.filter_specs[1].options] == [
        ("所有", ""),
        ("日本", "japan"),
    ]


def test_recovers_kotlin_check_and_text_filters(tmp_path: Path) -> None:
    (tmp_path / "build.gradle.kts").write_text(
        'keiyoushi { name = "Filters"; source { lang = "zh"; '
        'baseUrl { custom("https://filters.example") } } }'
    )
    source = tmp_path / "src" / "Filters.kt"
    source.parent.mkdir()
    source.write_text(
        """
        class Filters : HttpSource() {
            override fun getFilterList() = FilterList(SearchToggle(), CategoryFilter())
        }
        private class SearchToggle : Filter.CheckBox("将搜索词视为分类")
        private class CategoryFilter : Filter.Text("分类")
        """
    )

    ir = analyze_path(str(tmp_path))

    assert [(spec.id, spec.title, spec.kind, spec.options) for spec in ir.filter_specs] == [
        ("search_toggle", "将搜索词视为分类", "check", []),
        ("category", "分类", "text", []),
    ]


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


def test_analyzes_supported_triple_des_in_decompiled_apk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apk = tmp_path / "copymanga-3des.apk"
    apk.write_bytes(b"synthetic apk")

    def fake_jadx(_apk: Path, destination: Path) -> None:
        shutil.copytree(DECOMPILED_APK_FIXTURE, destination)
        main = next((destination / "sources").rglob("CopyManga.java"))
        content = main.read_text(encoding="utf-8")
        prefix, closing = content.rsplit("}", 1)
        main.write_text(
            prefix
            + """
            public void signRequest() {
                Cipher.getInstance("DESede/CBC/PKCS5Padding");
                new SecretKeySpec(key, "DESede");
                new IvParameterSpec(iv);
            }
            }
            """
            + closing,
            encoding="utf-8",
        )

    monkeypatch.setattr(ingest, "_run_jadx", fake_jadx)

    ir = analyze_path(str(apk))

    assert Capability.TRIPLE_DES_CBC in ir.capabilities
    assert "cryptography" not in ir.unsupported_features
