import json

from convert2aidoku.kotlin_settings import with_kotlin_settings
from convert2aidoku.models import Capability, GeneratedFile, GenerationManifest, SourceFile
from convert2aidoku.scaffold import normalize_generation_manifest
from tests.scenarios import minimal_source_ir

PREFERENCES = """
const val CHAPTER_FILTER_PREF = "CHAPTER_FILTER"
const val CHECK_API_LIMIT_PREF = "CHECK_API_LIMIT"

fun preferencesInternal(context: Context) = arrayOf(
    ListPreference(context).apply {
        key = CHAPTER_FILTER_PREF
        title = "章節列表顯示"
        summary = "選擇卷或章節"
        entries = arrayOf("全部", "僅章節", "僅卷")
        entryValues = arrayOf("all", "chapter", "book")
        setDefaultValue("all")
    },
    SwitchPreferenceCompat(context).apply {
        key = CHECK_API_LIMIT_PREF
        title = "檢查 API 上限"
        setDefaultValue(true)
    },
)
"""


def _ir():
    return minimal_source_ir(
        capabilities=[Capability.SETTINGS],
        files=[SourceFile(path="src/Preferences.kt", content=PREFERENCES, sha256="0")],
    )


def test_empty_ai_settings_are_replaced_from_standard_kotlin_preferences() -> None:
    manifest = GenerationManifest(
        source_struct="Example",
        files=[
            GeneratedFile(path="src/lib.rs", content="fn source() {}"),
            GeneratedFile(
                path="res/settings.json",
                content='[{"type":"group","items":[]}]',
            ),
        ],
    )

    restored = with_kotlin_settings(_ir(), manifest)
    settings = json.loads(
        next(file.content for file in restored.files if file.path == "res/settings.json")
    )[0]["items"]

    assert [item["key"] for item in settings] == ["CHAPTER_FILTER", "CHECK_API_LIMIT"]
    assert settings[0]["titles"] == ["全部", "僅章節", "僅卷"]
    assert settings[0]["values"] == ["all", "chapter", "book"]
    assert settings[0]["default"] == "all"
    assert settings[1]["default"] is True


def test_nonempty_ai_settings_are_preserved() -> None:
    original = '[{"type":"group","items":[{"type":"text","key":"custom"}]}]'
    manifest = GenerationManifest(
        source_struct="Example",
        files=[
            GeneratedFile(path="src/lib.rs", content="fn source() {}"),
            GeneratedFile(path="res/settings.json", content=original),
        ],
    )

    restored = with_kotlin_settings(_ir(), manifest)

    assert next(
        file.content for file in restored.files if file.path == "res/settings.json"
    ) == next(file.content for file in manifest.files if file.path == "res/settings.json")


def test_restored_setting_keys_are_projected_into_generated_rust() -> None:
    manifest = GenerationManifest(
        source_struct="Example",
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content=(
                    'fn settings() { defaults_get::<String>("chapter_filter"); '
                    'defaults_get::<bool>("check_api_limit"); }'
                ),
            ),
            GeneratedFile(
                path="res/settings.json",
                content='[{"type":"group","items":[]}]',
            ),
        ],
    )

    normalized = normalize_generation_manifest(_ir(), with_kotlin_settings(_ir(), manifest))
    rust = next(file.content for file in normalized.files if file.path == "src/lib.rs")

    assert 'defaults_get::<String>("CHAPTER_FILTER")' in rust
    assert 'defaults_get::<bool>("CHECK_API_LIMIT")' in rust


def test_recovers_multiselect_defaults_from_kotlin_set_constant() -> None:
    preferences = """
const val DESCRIPTION_PREF = "DESCRIPTION"
val DEFAULT_SET = setOf("A", "B", "C")
fun preferencesInternal(context: Context) = arrayOf(
    MultiSelectListPreference(context).apply {
        key = DESCRIPTION_PREF
        title = "作品信息顯示偏好"
        entries = arrayOf("作品公告", "作品别名", "跳轉連結")
        entryValues = arrayOf("A", "B", "C")
        setDefaultValue(DEFAULT_SET)
    },
)
"""
    ir = minimal_source_ir(
        capabilities=[Capability.SETTINGS],
        files=[SourceFile(path="src/Preferences.kt", content=preferences, sha256="0")],
    )
    manifest = GenerationManifest(
        source_struct="Example",
        files=[
            GeneratedFile(path="src/lib.rs", content="fn source() {}"),
            GeneratedFile(path="res/settings.json", content="[]"),
        ],
    )

    restored = with_kotlin_settings(ir, manifest)
    item = json.loads(
        next(file.content for file in restored.files if file.path == "res/settings.json")
    )[0]["items"][0]

    assert item["type"] == "multi-select"
    assert item["titles"] == ["作品公告", "作品别名", "跳轉連結"]
    assert item["values"] == ["A", "B", "C"]
    assert item["default"] == ["A", "B", "C"]
