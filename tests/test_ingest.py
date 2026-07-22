import shutil
from pathlib import Path

import pytest

from convert2aidoku import ingest
from convert2aidoku.errors import InputError
from convert2aidoku.ingest import (
    _compact_decompiled_dto,
    collect_source_files,
    parse_github_url,
    resolve_source,
)

DECOMPILED_APK_FIXTURE = Path(__file__).parent / "fixtures" / "decompiled_apk"


def test_parse_github_module_url() -> None:
    parsed = parse_github_url(
        "https://github.com/keiyoushi/extensions-source/tree/main/src/zh/mycomic"
    )
    assert parsed is not None
    assert parsed.owner == "keiyoushi"
    assert parsed.repository == "extensions-source"
    assert parsed.ref == "main"
    assert parsed.subpath == "src/zh/mycomic"


def test_rejects_ambiguous_github_url() -> None:
    with pytest.raises(InputError):
        parse_github_url("https://github.com/owner/repo/issues/1")


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/../repo/tree/main/src",
        "https://github.com/owner/repo/tree/main/../outside",
        "https://github.com/owner/%2e%2e/tree/main/src",
    ],
)
def test_rejects_github_path_traversal(url: str) -> None:
    with pytest.raises(InputError):
        parse_github_url(url)


def test_collects_only_module_text() -> None:
    fixture = Path(__file__).parent / "fixtures" / "simple"
    with resolve_source(str(fixture)) as resolved:
        files = collect_source_files(resolved)
    assert [item.path for item in files] == ["build.gradle.kts", "src/example/Simple.kt"]


def test_collects_legacy_groovy_module(tmp_path: Path) -> None:
    (tmp_path / "build.gradle").write_text("ext { extName = 'Legacy' }")
    source = tmp_path / "src" / "example" / "Legacy.kt"
    source.parent.mkdir(parents=True)
    source.write_text("class Legacy : HttpSource()")

    with resolve_source(str(tmp_path)) as resolved:
        files = collect_source_files(resolved)

    assert [item.path for item in files] == ["build.gradle", "src/example/Legacy.kt"]


def test_resolves_apk_with_mocked_jadx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    apk = tmp_path / "source.apk"
    apk.write_bytes(b"synthetic apk")

    def fake_jadx(_apk: Path, destination: Path) -> None:
        shutil.copytree(DECOMPILED_APK_FIXTURE, destination)

    monkeypatch.setattr(ingest, "_run_jadx", fake_jadx)
    with resolve_source(str(apk)) as resolved:
        assert resolved.source_format == "decompiled_apk"
        files = collect_source_files(resolved)

    paths = [item.path for item in files]
    assert "resources/AndroidManifest.xml" in paths
    assert any(path.endswith("CopyManga.java") for path in paths)
    main = next(item for item in files if item.path.endswith("CopyManga.java"))
    assert "compiler noise" not in main.content


def test_compacts_decompiled_dto_but_preserves_mapping_behavior() -> None:
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

    compacted = _compact_decompiled_dto(source)

    assert "private final String pathWord;" in compacted
    assert 'pathWord -> "path_word"' in compacted
    assert "toSManga" in compacted
    assert 'manga.setUrl("/comic/" + this.pathWord);' in compacted
    assert " copy(" not in compacted
    assert "equals(" not in compacted
    assert "hashCode(" not in compacted
    assert "toString(" not in compacted
