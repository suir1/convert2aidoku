from pathlib import Path

import pytest

from convert2aidoku.decompiled_input import (
    DecompiledInputInspection,
    DecompiledManifest,
    normalize_decompiled_java,
    project_java_behavior,
)
from convert2aidoku.errors import InputError
from convert2aidoku.ingest import ResolvedSource, collect_source_files

FIXTURE = Path(__file__).parent / "fixtures" / "decompiled_apk"


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
