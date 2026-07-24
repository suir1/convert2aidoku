from __future__ import annotations

import json
from pathlib import Path

import pytest

from convert2aidoku import generated_source_metadata
from convert2aidoku.generated_source_metadata import GeneratedSourceMetadata
from convert2aidoku.models import ContentRating, GeneratedFile, GenerationManifest
from tests.scenarios import minimal_source_ir


def _manifest(rust: str) -> GenerationManifest:
    return GenerationManifest(
        source_struct="Example",
        files=[GeneratedFile(path="src/lib.rs", content=rust)],
    )


def test_source_ir_metadata_writes_current_aidoku_shape(tmp_path: Path) -> None:
    source_ir = minimal_source_ir(language="zh", source_id="zh.example")
    source_ir = source_ir.model_copy(
        update={
            "metadata": source_ir.metadata.model_copy(
                update={
                    "name": "示例漫画",
                    "version": 7,
                    "content_rating": ContentRating.NSFW,
                }
            )
        }
    )

    GeneratedSourceMetadata.from_source_ir(source_ir).write(tmp_path)

    document = json.loads(GeneratedSourceMetadata.path(tmp_path).read_text(encoding="utf-8"))
    assert document == {
        "info": {
            "id": "zh.example",
            "name": "示例漫画",
            "version": 7,
            "url": "https://example.com",
            "contentRating": 2,
            "languages": ["zh"],
        }
    }
    loaded = GeneratedSourceMetadata.load(tmp_path)
    assert loaded.source_id == "zh.example"
    assert loaded.site_url == "https://example.com"
    assert loaded.version == 7


@pytest.mark.parametrize(("minimum", "expected"), [(2, 5), (10, 10)])
def test_version_bump_preserves_unknown_aidoku_fields(
    tmp_path: Path,
    minimum: int,
    expected: int,
) -> None:
    path = GeneratedSourceMetadata.path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "info": {
                    "id": "en.example",
                    "version": 4,
                    "url": "https://example.com",
                    "customInfo": {"enabled": True},
                },
                "customRoot": ["preserve"],
            }
        ),
        encoding="utf-8",
    )

    GeneratedSourceMetadata.load(tmp_path).with_bumped_version(minimum).write(tmp_path)

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["info"]["version"] == expected
    assert document["info"]["customInfo"] == {"enabled": True}
    assert document["customRoot"] == ["preserve"]
    assert not list(path.parent.glob(".source.json.tmp-*"))


def test_manifest_requirements_select_highest_host_version_and_can_clear_it() -> None:
    metadata = GeneratedSourceMetadata.from_source_ir(minimal_source_ir())
    date = metadata.with_manifest_requirements(
        _manifest('fn date() { parse_date("2026-01-01", "yyyy-MM-dd"); }')
    )
    timeout = date.with_manifest_requirements(
        _manifest("fn request() { request.set_timeout(30.0); parse_local_date(value); }")
    )
    cleared = timeout.with_manifest_requirements(_manifest("fn ordinary() {}"))

    assert date.minimum_app_version == "0.7.1"
    assert timeout.minimum_app_version == "0.8.3"
    assert cleared.minimum_app_version is None


@pytest.mark.parametrize("document", [[], {}, {"info": []}])
def test_load_rejects_invalid_aidoku_document_shape(
    tmp_path: Path,
    document: object,
) -> None:
    path = GeneratedSourceMetadata.path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="object"):
        GeneratedSourceMetadata.load(tmp_path)


def test_atomic_write_cleans_temporary_file_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = GeneratedSourceMetadata.from_source_ir(minimal_source_ir())

    def deny_replace(*_args: object) -> None:
        raise PermissionError("read only")

    monkeypatch.setattr(generated_source_metadata.os, "replace", deny_replace)

    with pytest.raises(PermissionError, match="read only"):
        metadata.write(tmp_path)

    resource = GeneratedSourceMetadata.path(tmp_path).parent
    assert not GeneratedSourceMetadata.path(tmp_path).exists()
    assert not list(resource.glob(".source.json.tmp-*"))
