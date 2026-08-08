from __future__ import annotations

import hashlib

import pytest

from convert2aidoku.errors import InputError
from convert2aidoku.generation_context import build_generation_context, build_settings_context
from convert2aidoku.models import Capability, SourceFile, SourceIR
from tests.scenarios import minimal_source_ir


def _file(path: str, content: str) -> SourceFile:
    return SourceFile(
        path=path,
        content=content,
        sha256=hashlib.sha256(content.encode()).hexdigest(),
    )


def _ir(*files: SourceFile, source_format: str = "decompiled_apk") -> SourceIR:
    return minimal_source_ir(
        files=list(files),
        source_id="zh.example",
        language="zh",
        source_format=source_format,
    )


def test_decompiled_generation_context_keeps_behavior_and_drops_jadx_noise() -> None:
    main = _file(
        "sources/example/Example.java",
        """
        package example;
        import java.util.Objects;
        public final class Example extends HttpSource {
            private final String baseUrl = "https://example.com";
            public Example() { this.client = buildClient(); }
            public Request searchMangaRequest(int page) {
                return GET(baseUrl + "/comics?page=" + page);
            }
            public String toString() { return "generated-noise"; }
            public boolean equals(Object value) { return Objects.equals(this, value); }
            public static final class WhenMappings {
                static { int latest = 1; }
            }
        }
        """,
    )
    interceptor = _file(
        "sources/example/interceptor/UserAgentInterceptor.java",
        """
        package example.interceptor;
        public final class UserAgentInterceptor implements Interceptor {
            private final String fallback = "Mozilla/5.0";
            public Response intercept(Interceptor.Chain chain) {
                return chain.proceed(chain.request().newBuilder()
                    .header("User-Agent", fallback).build());
            }
            static final class GeneratedDto {
                public String component1() { return fallback; }
                public String copy() { return fallback; }
                public String toString() { return fallback; }
            }
        }
        """,
    )
    dto = _file(
        "sources/example/api/dto/Comic.java",
        (
            "package example;\n"
            "// C2A compacted JADX DTO: generated constructors and value methods removed.\n"
            "public final class Comic {\n"
            "    // Fields:\n"
            "    private final String pathWord;\n"
            "}\n"
        ),
    )
    manifest = _file("resources/AndroidManifest.xml", "<manifest package='example'/>")
    ir = _ir(main, interceptor, dto, manifest)

    context = build_generation_context(ir)
    payload = context.as_payload()
    evidence = {item["path"]: item["content"] for item in payload["source_evidence"]}

    assert payload["context_stats"]["mode"] == "decompiled_behavior_evidence"
    assert "searchMangaRequest" in evidence[main.path]
    assert '"/comics?page="' in evidence[main.path]
    assert "WhenMappings" in evidence[main.path]
    assert "generated-noise" not in evidence[main.path]
    assert "component1" not in evidence[interceptor.path]
    assert 'header("User-Agent", fallback)' in evidence[interceptor.path]
    assert payload["decompiled_dto_shapes"] == ["Comic { pathWord: String }"]
    assert dto.path not in evidence
    assert manifest.path not in evidence
    assert any(
        item["path"] == manifest.path and item["reason"] == "represented_in_source_ir"
        for item in payload["omitted_source_files"]
    )
    assert any(
        item["path"] == dto.path and item["reason"] == "represented_in_decompiled_dto_shapes"
        for item in payload["omitted_source_files"]
    )
    assert payload["context_stats"]["evidence_chars"] < payload["context_stats"]["original_chars"]


def test_generation_context_never_silently_truncates_essential_main_source() -> None:
    invalid_main = _file("sources/example/Example.java", "not valid java " * 100)
    ir = _ir(invalid_main)

    with pytest.raises(InputError, match="essential generation evidence exceeds"):
        build_generation_context(ir, max_chars=100)


def test_kotlin_generation_context_preserves_complete_source_files() -> None:
    kotlin = _file(
        "src/example/Example.kt",
        'class Example : HttpSource() { override val baseUrl = "https://example.com" }',
    )
    build = _file("build.gradle.kts", 'ext { lang = "zh" }')
    ir = _ir(build, kotlin, source_format="kotlin_module")

    payload = build_generation_context(ir).as_payload()

    assert payload["context_stats"]["mode"] == "complete_kotlin_source"
    assert payload["source_evidence"] == [
        build.model_dump(mode="json"),
        kotlin.model_dump(mode="json"),
    ]
    assert payload["omitted_source_files"] == []
    assert payload["decompiled_dto_shapes"] == []


def test_prompt_context_excludes_local_only_source_audit_data() -> None:
    kotlin = _file("src/example/Example.kt", "class Example : HttpSource()")
    ir = _ir(kotlin, source_format="kotlin_module").model_copy(
        update={"analysis_rule_ids": ["capability_search"]}
    )

    generation = build_generation_context(ir).as_payload()
    settings = build_settings_context(ir)

    assert "analysis_rule_ids" not in generation["source_ir"]
    assert "analysis_rule_ids" not in settings["source"]


def test_compacted_dto_outside_api_directory_is_represented_once() -> None:
    main = _file("sources/example/Example.java", "public final class Example {}")
    dto = _file(
        "sources/example/ComicList.java",
        """
        // C2A compacted JADX DTO: generated constructors and value methods removed.
        public final class ComicList {
            private final List<ComicItem> items;
            private final String next;
        }
        """,
    )

    payload = build_generation_context(_ir(main, dto)).as_payload()

    assert payload["decompiled_dto_shapes"] == [
        "ComicList { items: List<ComicItem>, next: String }"
    ]
    assert dto.path not in {item["path"] for item in payload["source_evidence"]}
    assert any(
        item["path"] == dto.path and item["reason"] == "represented_in_decompiled_dto_shapes"
        for item in payload["omitted_source_files"]
    )


def test_settings_context_keeps_only_bounded_preference_evidence() -> None:
    option = _file(
        "sources/example/PlatformOption.java",
        'enum PlatformOption { ONE("1", "Platform 1"); '
        'public static final String KEY = "platform"; '
        'private static final String DEFAULT = "1"; }',
    )
    unrelated = _file(
        "sources/example/api/ComicApi.java",
        'class ComicApi { String endpoint = "/comics"; String marker = "unrelated-body"; }',
    )

    payload = build_settings_context(_ir(option, unrelated))

    assert [item["path"] for item in payload["settings_evidence"]] == [option.path]
    assert 'KEY = "platform"' in payload["settings_evidence"][0]["content"]
    assert "unrelated-body" not in str(payload)
    assert payload["context_stats"]["evidence_chars"] <= 50_000


def test_settings_context_compacts_option_methods_and_generic_preference_usage() -> None:
    option = _file(
        "sources/example/PlatformOption.java",
        """
        public final class PlatformOption {
            NONE("None", "platform.none"),
            ONE("1", "platform.one");
            private static final String DEFAULT = "platform.one";
            public static final String KEY = "v2.pref.platform";
            public String key2value(String key) {
                String unrelatedBusinessMethod = "must-not-be-sent";
                return unrelatedBusinessMethod;
            }
        }
        """,
    )
    main = _file(
        "sources/example/Example.java",
        """
        public final class Example {
            private SharedPreferences preferences;
            void unrelated() { preferences.getString("noise", "noise"); }
            void setupPreferenceScreen(PreferenceScreen screen) {
                screen.addAll(initPreferences());
            }
        }
        """,
    )
    keys = _file(
        "sources/example/PreferencesKeys.java",
        'public final class PreferencesKeys { public static final String USER_AGENT = "ua"; }',
    )

    payload = build_settings_context(_ir(option, main, keys))
    evidence = {item["path"]: item["content"] for item in payload["settings_evidence"]}

    assert 'ONE("1", "platform.one")' in evidence[option.path]
    assert 'KEY = "v2.pref.platform"' in evidence[option.path]
    assert "must-not-be-sent" not in evidence[option.path]
    assert "setupPreferenceScreen" in evidence[main.path]
    assert evidence[main.path].count("setupPreferenceScreen") == 1
    assert 'getString("noise"' not in evidence[main.path]
    assert 'USER_AGENT = "ua"' in evidence[keys.path]


def test_settings_context_keeps_decompiled_preference_keys_defaults_and_enum_values() -> None:
    preferences = _file(
        "sources/example/Preferences.java",
        """
        public final class Preferences {
            public enum LanguageOption {
                Simplified("简体中文", "zh-cn"),
                Traditional("繁體中文", "zh-tw");
            }
            public static final ApiOption MAIN = new ApiOption("www.example.com", "主站");
            public String getDomain(SharedPreferences preferences) {
                return preferences.getString("v1.key.api", "www.example.com");
            }
            public Preference[] initPreferences(Context context) {
                ListPreference preference = new ListPreference(context);
                preference.setKey("v1.key.api");
                preference.setTitle("API 域名");
                preference.setDefaultValue("www.example.com");
                return new Preference[]{preference};
            }
            public String unrelatedBusinessMethod() { return "must-not-be-sent"; }
        }
        """,
    )

    payload = build_settings_context(_ir(preferences))
    content = payload["settings_evidence"][0]["content"]

    assert 'Simplified("简体中文", "zh-cn")' in content
    assert 'new ApiOption("www.example.com", "主站")' in content
    assert 'getString("v1.key.api", "www.example.com")' in content
    assert 'setTitle("API 域名")' in content
    assert "must-not-be-sent" not in content


def test_settings_context_removes_android_listener_bodies_from_ui_builder() -> None:
    preferences = _file(
        "sources/example/PreferencesKt.java",
        """
        public final class PreferencesKt {
            public static Preference[] initPreferences(Context context) {
                ListPreference preference = new ListPreference(context);
                preference.setKey("api_domain");
                preference.setTitle("API domain");
                preference.setDefaultValue("api.example.com");
                preference.setOnPreferenceChangeListener(value -> {
                    String listenerNoise = "must-not-be-sent";
                    return true;
                });
                return new Preference[]{preference};
            }
        }
        """,
    )

    payload = build_settings_context(_ir(preferences))
    content = payload["settings_evidence"][0]["content"]

    assert 'setKey("api_domain")' in content
    assert 'setTitle("API domain")' in content
    assert 'setDefaultValue("api.example.com")' in content
    assert "must-not-be-sent" not in content


def test_rust_generation_keeps_setting_accessors_but_omits_android_settings_ui() -> None:
    main = _file("sources/example/Example.java", "public final class Example {}")
    preferences = _file(
        "sources/example/PreferencesKt.java",
        """
        public final class PreferencesKt {
            public static String getDomain(SharedPreferences preferences) {
                return preferences.getString("api_domain", "api.example.com");
            }
            public static Preference[] initPreferences(Context context) {
                ListPreference preference = new ListPreference(context);
                preference.setKey("api_domain");
                preference.setTitle("API domain");
                return new Preference[]{preference};
            }
            private static boolean initPreferences$lambda$1(Preference preference, Object value) {
                preference.setSummary(value.toString());
                return true;
            }
        }
        """,
    )
    ir = _ir(main, preferences).model_copy(update={"capabilities": [Capability.SETTINGS]})

    payload = build_generation_context(ir).as_payload()
    evidence = {item["path"]: item["content"] for item in payload["source_evidence"]}

    assert "getDomain" in evidence[preferences.path]
    assert "initPreferences" not in evidence[preferences.path]


def test_provider_prompt_projection_summarizes_omissions_without_audit_hashes() -> None:
    main = _file("sources/example/Example.java", "public final class Example {}")
    manifest = _file("resources/AndroidManifest.xml", "<manifest/>")

    payload = build_generation_context(_ir(main, manifest)).as_prompt_payload()

    assert "sha256" not in payload["source_evidence"][0]
    assert payload["omitted_source_summary"] == {"represented_in_source_ir": 1}
    assert "omitted_source_files" not in payload
