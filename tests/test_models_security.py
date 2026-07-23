import json

import pytest
from pydantic import ValidationError

from convert2aidoku.errors import SecurityError
from convert2aidoku.models import GeneratedFile, GenerationManifest


@pytest.mark.parametrize(
    "path",
    ["../Cargo.toml", "/tmp/escape.rs", "src/../../escape.rs", "Cargo.toml", "src/x.kt"],
)
def test_rejects_unsafe_generated_paths(path: str) -> None:
    with pytest.raises((SecurityError, ValidationError)):
        GeneratedFile(path=path, content="x")


def test_manifest_requires_lib_rs() -> None:
    with pytest.raises(ValidationError, match="src/lib.rs"):
        GenerationManifest(
            source_struct="Source",
            files=[GeneratedFile(path="src/net.rs", content="")],
        )


def test_accepts_safe_manifest() -> None:
    manifest = GenerationManifest(
        source_struct="Source",
        files=[GeneratedFile(path="src/lib.rs", content="#![no_std]")],
    )
    assert manifest.files[0].path == "src/lib.rs"


def test_manifest_rejects_unknown_optional_trait() -> None:
    with pytest.raises(ValidationError, match="ListingProvider"):
        GenerationManifest(
            source_struct="Source",
            implemented_traits=["MangaProvider"],
            files=[GeneratedFile(path="src/lib.rs", content="#![no_std]")],
        )


def test_filter_object_options_are_normalized_to_parallel_arrays() -> None:
    generated = GeneratedFile(
        path="res/filters.json",
        content=(
            '[{"type":"select","id":"sort","options":['
            '{"title":"Latest","value":""},'
            '{"title":"Popular","value":"popular"}]}]'
        ),
    )

    assert '"options": [\n\t\t\t"Latest",' in generated.content
    assert '"ids": [\n\t\t\t"",' in generated.content


def test_filter_rejects_non_string_options() -> None:
    with pytest.raises(ValueError, match="options must be an array of strings"):
        GeneratedFile(
            path="res/filters.json",
            content='[{"type":"select","options":[1]}]',
        )


def test_setting_object_options_are_normalized_to_titles_and_values() -> None:
    generated = GeneratedFile(
        path="res/settings.json",
        content=(
            '[{"type":"select","key":"language","title":"Language","default":"",'
            '"options":[{"title":"Traditional","value":""},'
            '{"title":"Simplified","value":"cn"}]}]'
        ),
    )

    parsed = json.loads(generated.content)
    setting = parsed[0]["items"][0]
    assert parsed[0]["type"] == "group"
    assert setting["titles"] == ["Traditional", "Simplified"]
    assert setting["values"] == ["", "cn"]
    assert '"options"' not in generated.content


def test_setting_id_is_normalized_to_aidoku_key() -> None:
    generated = GeneratedFile(
        path="res/settings.json",
        content=(
            '[{"type":"group","items":[{"type":"select",'
            '"id":"v2.pref.platform","title":"Platform",'
            '"titles":["1"],"values":["platform.one"],'
            '"default":"platform.one"}]}]'
        ),
    )

    setting = json.loads(generated.content)[0]["items"][0]
    assert setting["key"] == "v2.pref.platform"
    assert "id" not in setting


def test_grouped_setting_options_are_normalized_recursively() -> None:
    generated = GeneratedFile(
        path="res/settings.json",
        content=(
            '[{"type":"group","items":[{"type":"select","key":"language",'
            '"title":"Language","options":[{"title":"Traditional","value":""}]}]}]'
        ),
    )

    parsed = json.loads(generated.content)
    assert parsed[0]["items"][0]["titles"] == ["Traditional"]
    assert parsed[0]["items"][0]["values"] == [""]


def test_settings_reject_mixed_grouped_and_ungrouped_top_level_items() -> None:
    with pytest.raises(ValueError, match="top-level items must all be groups"):
        GeneratedFile(
            path="res/settings.json",
            content=(
                '[{"type":"group","items":[]},'
                '{"type":"select","key":"language","title":"Language",'
                '"values":[""]}]'
            ),
        )


def test_setting_default_must_be_one_of_values() -> None:
    with pytest.raises(ValueError, match="default must be one of values"):
        GeneratedFile(
            path="res/settings.json",
            content=(
                '[{"type":"select","key":"language","title":"Language",'
                '"values":["cn"],"default":""}]'
            ),
        )
