import json

import pytest
from pydantic import ValidationError

from convert2aidoku.errors import SecurityError
from convert2aidoku.models import (
    GeneratedFile,
    GeneratedResources,
    GenerationManifest,
    SourceFilterOption,
    SourceFilterSpec,
    normalize_generated_json_resource,
)


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


def test_static_filter_resource_is_built_from_source_ir_specs() -> None:
    manifest = GenerationManifest(
        source_struct="Source",
        files=[GeneratedFile(path="src/lib.rs", content="#![no_std]")],
    )
    spec = SourceFilterSpec(
        source_class="AudienceFilter",
        id="audience",
        title="Audience",
        kind="select",
        options=[
            SourceFilterOption(title="Male", value="male"),
            SourceFilterOption(title="Female", value="female"),
        ],
        default_index=0,
    )

    generated = GeneratedResources(manifest).with_source_filters([spec])

    resource = next(item for item in generated.files if item.path == "res/filters.json")
    filters = json.loads(resource.content)
    assert filters == [
        {
            "type": "select",
            "id": "audience",
            "title": "Audience",
            "options": ["Male", "Female"],
            "ids": ["male", "female"],
            "default": "male",
        }
    ]


def test_static_filter_smoke_samples_middle_of_descending_year_options() -> None:
    manifest = GenerationManifest(
        source_struct="Source",
        files=[
            GeneratedFile(path="src/lib.rs", content="#![no_std]"),
            GeneratedFile(
                path="res/filters.json",
                content=json.dumps(
                    [
                        {
                            "type": "select",
                            "id": "award",
                            "options": ["All", "2027", "2026", "2025", "2024", "2023"],
                            "ids": ["0", "2027", "2026", "2025", "2024", "2023"],
                            "default": "0",
                        }
                    ]
                ),
            ),
        ],
    )

    cases = GeneratedResources(manifest).static_filter_cases()

    assert cases == [{"kind": "select", "id": "award", "value": "2025"}]


def test_check_and_text_filter_resources_are_built_from_source_ir_specs() -> None:
    manifest = GenerationManifest(
        source_struct="Source",
        files=[GeneratedFile(path="src/lib.rs", content="#![no_std]")],
    )
    specs = [
        SourceFilterSpec(
            source_class="SearchToggle",
            id="search_toggle",
            title="Search as category",
            kind="check",
        ),
        SourceFilterSpec(
            source_class="CategoryFilter",
            id="category",
            title="Category",
            kind="text",
        ),
    ]

    generated = GeneratedResources(manifest).with_source_filters(specs)

    resource = next(item for item in generated.files if item.path == "res/filters.json")
    assert json.loads(resource.content) == [
        {
            "type": "check",
            "id": "search_toggle",
            "title": "Search as category",
            "default": False,
        },
        {
            "type": "text",
            "id": "category",
            "title": "Category",
            "default": "",
        },
    ]


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


def test_nested_setting_groups_are_promoted_to_schema_valid_top_level_groups() -> None:
    content = """[
        {"type":"group","key":"main","title":"Main","items":[
            {"type":"group","key":"api","title":"API","items":[
                {"type":"select","key":"domain","title":"Domain",
                 "values":["api.example"],"default":"api.example"}
            ]},
            {"type":"text","key":"user_agent","title":"User-Agent","default":""}
        ]}
    ]"""

    normalized = normalize_generated_json_resource("res/settings.json", content)
    parsed = json.loads(normalized)

    assert [group["title"] for group in parsed] == ["API", "Main"]
    assert all(item["type"] != "group" for group in parsed for item in group["items"])
    assert parsed[0]["items"][0]["key"] == "domain"
    assert parsed[1]["items"][0]["key"] == "user_agent"
    assert normalize_generated_json_resource("res/settings.json", normalized) == normalized


def test_settings_fill_required_titles_text_defaults_and_protocol_resolution_values() -> None:
    generated = GeneratedFile(
        path="res/settings.json",
        content="""[
            {"type":"group","title":"设置","items":[
                {"type":"select","key":"v2.pref.api_domain",
                 "values":["api.example"],"default":"api.example"},
                {"type":"text","key":"v2.pref.api_domain_custom"},
                {"type":"select","key":"v2.pref.resolution",
                 "values":["800","1200","1500"],
                 "titles":["800","1200","1500"],"default":"1500"},
                {"type":"text","key":"v2.key.user_agent"}
            ]}
        ]""",
    )

    items = json.loads(generated.content)[0]["items"]
    assert [item["title"] for item in items] == [
        "API Domain",
        "API Domain Custom",
        "Resolution",
        "User Agent",
    ]
    assert items[1]["default"] == ""
    assert items[2]["values"] == [
        "resolution.r800",
        "resolution.r1200",
        "resolution.r1500",
    ]
    assert items[2]["default"] == "resolution.r1500"
    assert items[3]["default"] == ""


def test_settings_protocol_normalization_is_idempotent_and_does_not_rewrite_other_selects() -> None:
    content = """[
        {"type":"group","items":[
            {"type":"select","key":"page_size","values":["20","40"],"default":"20"},
            {"type":"select","key":"resolution","values":["resolution.r800"],
             "default":"resolution.r800"}
        ]}
    ]"""

    once = normalize_generated_json_resource("res/settings.json", content)
    twice = normalize_generated_json_resource("res/settings.json", once)

    assert twice == once
    items = json.loads(once)[0]["items"]
    assert items[0]["values"] == ["20", "40"]
    assert items[0]["default"] == "20"
    assert items[1]["values"] == ["resolution.r800"]


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
