from pathlib import Path

import pytest

from convert2aidoku.decompiled_analysis import _java_request_header_policy
from convert2aidoku.decompiled_input import (
    DecompiledInputInspection,
    DecompiledManifest,
    decompiled_detail_uses_api_envelope,
    decompiled_dto_shapes,
    decompiled_dynamic_filter_endpoint,
    decompiled_rank_list_wraps_comic,
    normalize_decompiled_java,
    project_java_behavior,
)
from convert2aidoku.errors import InputError
from convert2aidoku.ingest import ResolvedSource, collect_source_files
from convert2aidoku.models import SourceFile

FIXTURE = Path(__file__).parent / "fixtures" / "decompiled_apk"


def test_detects_recovered_detail_api_envelope() -> None:
    files = [
        SourceFile(
            path="CopyManga.java",
            sha256="0",
            content=(
                "Reflection.typeOf(ApiResponse.class, "
                "KTypeProjection.Companion.invariant("
                "Reflection.typeOf(ComicDetailResult.class)))"
            ),
        )
    ]

    assert decompiled_detail_uses_api_envelope(files)
    assert not decompiled_detail_uses_api_envelope([])


def test_detects_recovered_rank_item_comic_wrapper() -> None:
    files = [
        SourceFile(
            path="RankResult.java",
            sha256="0",
            content="class RankResult { private final List<ListItem> list; }",
        ),
        SourceFile(
            path="ListItem.java",
            sha256="0",
            content="class ListItem { private final ComicSummary comic; }",
        ),
    ]

    assert decompiled_rank_list_wraps_comic(files)
    assert not decompiled_rank_list_wraps_comic(files[:1])


def test_recovers_dynamic_filter_endpoint() -> None:
    files = [
        SourceFile(
            path="ApiRepo.java",
            sha256="0",
            content="""
            public final String tagList() {
                return getApiUrl() + "/theme/comic/count?limit=100";
            }
            """,
        )
    ]

    assert decompiled_dynamic_filter_endpoint(files) == "/theme/comic/count?limit=100"
    assert decompiled_dynamic_filter_endpoint([]) is None


def test_manifest_exposes_all_shared_apk_facts() -> None:
    manifest = DecompiledManifest.from_content(
        """<?xml version="1.0" encoding="utf-8"?>
        <manifest xmlns:android="http://schemas.android.com/apk/res/android"
            android:versionCode="12"
            package="eu.kanade.tachiyomi.extension.zh.example">
            <application android:label="Tachiyomi: Example">
                <meta-data android:name="tachiyomi.extension.class"
                    android:value=".Example,.Second" />
                <meta-data android:name="tachiyomi.extension.nsfw" android:value="1" />
            </application>
        </manifest>"""
    )

    assert manifest.package == "eu.kanade.tachiyomi.extension.zh.example"
    assert manifest.main_class_name == "Example"
    assert manifest.application_label == "Tachiyomi: Example"
    assert manifest.version_text == "12"
    assert manifest.metadata["tachiyomi.extension.nsfw"] == "1"


def test_manifest_translates_invalid_xml_and_missing_main_class() -> None:
    with pytest.raises(InputError, match="unable to parse decompiled AndroidManifest"):
        DecompiledManifest.from_content("<manifest>")

    manifest = DecompiledManifest.from_content("<manifest><application /></manifest>")

    assert manifest.version_text == "1"
    with pytest.raises(InputError, match="does not declare tachiyomi.extension.class"):
        _ = manifest.main_class_name


def test_inspection_recovers_one_consistent_main_class_and_java_view() -> None:
    resolved = ResolvedSource(
        input_ref="fixture.apk",
        module_path=FIXTURE,
        repository_root=FIXTURE,
        commit=None,
        license_path=None,
        source_format="decompiled_apk",
    )

    inspection = DecompiledInputInspection.from_files(collect_source_files(resolved))

    assert inspection.main_class == "CopyManga"
    assert inspection.parents == ("HttpSource", "ConfigurableSource")
    assert inspection.main_file.path.endswith("CopyManga.java")
    assert "searchMangaRequest" in inspection.method_names
    assert {"Accept", "Origin", "Version"} <= set(inspection.header_names)
    assert "compiler noise" not in inspection.java


def test_java_request_header_policy_recovers_profiles_domains_and_shared_headers() -> None:
    profiles, shared = _java_request_header_policy(
        """
        private static final Headers COPY_HEADER = Headers.Companion.of(new String[]{
            "Accept", "application/json", "Origin", "https://copy.example"
        });
        private static final Headers HOT_HEADER = Headers.Companion.of(new String[]{
            "Accept", "application/json", "Webp", "1"
        });
        COPY("api.copy.example", "api.copy.example", "Copy", ApiRepo.INSTANCE.getCOPY_HEADER()),
        HOT("api.hot.example", "api.hot.example", "Hot", ApiRepo.INSTANCE.getHOT_HEADER());
        this.insertHeader = Headers.Companion.of(new String[]{
            "sec-fetch-mode", "navigate"
        });
        """
    )

    assert [profile.name for profile in profiles] == ["COPY_HEADER", "HOT_HEADER"]
    assert profiles[0].domains == ["api.copy.example"]
    assert profiles[0].headers["Origin"] == "https://copy.example"
    assert profiles[1].domains == ["api.hot.example"]
    assert shared == {"sec-fetch-mode": "navigate"}


def test_dto_normalization_is_idempotent_and_preserves_mapping_behavior() -> None:
    source = """
package example;
@Serializable
public final class Comic {
    private final String pathWord;
    private final String name;
    @SerialName("path_word")
    public static void getPathWord$annotations() {}
    public final String getPathWord() { return this.pathWord; }
    public final Comic copy(String pathWord, String name) { return new Comic(pathWord, name); }
    public boolean equals(Object other) { return other instanceof Comic; }
    public int hashCode() { return this.pathWord.hashCode(); }
    public String toString() { return this.name; }
    public final SManga toSManga() {
        SManga manga = SManga.create();
        manga.setUrl("/comic/" + this.pathWord);
        manga.setTitle(this.name);
        return manga;
    }
}
"""

    compacted = normalize_decompiled_java(source, Path("api/dto/Comic.java"))

    assert normalize_decompiled_java(compacted, Path("api/dto/Comic.java")) == compacted
    assert "private final String pathWord;" in compacted
    assert 'pathWord -> "path_word"' in compacted
    assert "toSManga" in compacted
    assert 'manga.setUrl("/comic/" + this.pathWord);' in compacted
    assert " copy(" not in compacted
    assert "equals(" not in compacted
    assert "hashCode(" not in compacted
    assert "toString(" not in compacted


def test_dto_shapes_preserve_generic_field_types_and_serialized_names() -> None:
    detail = SourceFile(
        path="sources/example/api/dto/ComicDetailResult.java",
        sha256="0",
        content="""
        package example;
        // C2A compacted JADX DTO: generated constructors and value methods removed.
        public final class ComicDetailResult {
            // Serialized field names:
            // pathWord -> "path_word"
            // Fields:
            private final Map<String, GroupInfo> groups;
            private final String pathWord;
            public static final Companion INSTANCE = new Companion(null);
        }
        """,
    )

    shapes = decompiled_dto_shapes([detail])

    assert [shape.name for shape in shapes] == ["ComicDetailResult"]
    assert [(field.name, field.serialized_name, field.java_type) for field in shapes[0].fields] == [
        ("groups", "groups", "Map<String, GroupInfo>"),
        ("pathWord", "path_word", "String"),
    ]
    assert shapes[0].render() == (
        "ComicDetailResult { groups: Map<String, GroupInfo>, pathWord (json path_word): String }"
    )


def test_dto_shapes_restore_kotlin_boolean_is_prefix_to_snake_case() -> None:
    files = [
        SourceFile(
            path="sources/example/api/dto/ChapterDetail.java",
            sha256="0",
            content="""
            public final class ChapterDetail {
                private final boolean isLong;
            }
            """,
        )
    ]

    shapes = decompiled_dto_shapes(files)

    assert shapes[0].fields[0].serialized_name == "is_long"


def test_behavior_projection_keeps_distinct_main_and_helper_policies() -> None:
    java = """
public final class Example extends HttpSource {
    private final String baseUrl = "https://example.com";
    public Example() { this.client = buildClient("constructor-literal"); }
    public String getApiUrl() { return baseUrl + "/api"; }
    public Request searchMangaRequest() { return GET(getApiUrl() + "/comics"); }
    public Request loginRequest() { return GET(baseUrl + "/login"); }
    public String component1() { return baseUrl; }
    public String copy() { return baseUrl; }
    public String toString() { return "generated-noise"; }
}
"""

    main = project_java_behavior(java, main=True, public_only=True)
    helper = project_java_behavior(java, main=False, public_only=True)

    assert "Example()" in main
    assert "getApiUrl" in main
    assert "searchMangaRequest" in main
    assert "loginRequest" not in main
    assert "component1" not in main
    assert "generated-noise" not in main
    assert "Example()" not in helper
    assert "loginRequest" in helper
    assert '"constructor-literal"' in helper
    assert len(main) <= len(java)
    assert len(helper) <= len(java)


def test_partial_jadx_java_falls_back_to_original_content() -> None:
    content = "not valid java " * 20

    assert project_java_behavior(content, main=True, public_only=True) == content
