from __future__ import annotations

import json

from convert2aidoku.decompiled_settings import (
    deterministic_decompiled_settings,
    with_deterministic_decompiled_settings,
)
from convert2aidoku.models import Capability, GeneratedFile, GenerationManifest, SourceFile
from tests.scenarios import minimal_source_ir

PREFERENCES = """
public final class PreferencesKt {
    public static Preference[] initPreferences(Context context) {
        Preference[] preferenceArr = new Preference[4];
        ListPreference domain = new ListPreference(context);
        domain.setKey(DomainOption.KEY);
        domain.setTitle("API Domain");
        domain.setSummary("Choose endpoint");
        domain.setEntries(DomainOption.INSTANCE.getENTRIES());
        domain.setEntryValues(DomainOption.INSTANCE.getENTRY_KEYS());
        domain.setDefaultValue(DomainOption.INSTANCE.getDEFAULT());
        preferenceArr[0] = (Preference) domain;
        EditTextPreference userAgent = new EditTextPreference(context);
        userAgent.setKey(PreferencesKeys.USER_AGENT);
        userAgent.setTitle("User-Agent");
        userAgent.setDefaultValue(PluginMetaData.USER_AGENT);
        preferenceArr[1] = userAgent;
        SwitchPreferenceCompat login = new SwitchPreferenceCompat(context);
        login.setKey(PreferencesKeys.ENABLE_LOGIN);
        login.setTitle("Login");
        login.setDefaultValue(false);
        preferenceArr[2] = login;
        ListPreference comments = new ListPreference(context);
        comments.setKey(ChapterCommentApiOption.KEY);
        comments.setTitle("Comment API");
        comments.setDefaultValue(ChapterCommentApiOption.INSTANCE.getDEFAULT());
        preferenceArr[3] = comments;
        return preferenceArr;
    }
}
"""

DOMAIN_OPTION = """
public enum DomainOption {
    PRIMARY("Primary", "api.example"),
    BACKUP("Backup", "backup.example");
    public static final String KEY = "v2.pref.api_domain";
    private static final String DEFAULT = new DomainOption("Primary", "api.example").entryKey;
}
"""

CONSTANTS = """
public final class PreferencesKeys {
    public static final String USER_AGENT = "v2.key.user_agent";
    public static final String ENABLE_LOGIN = "v2.key.enable_login";
}
"""

METADATA = """
public final class PluginMetaData {
    public static final String USER_AGENT = "Example/1.0";
}
"""

INLINE_JADX_PREFERENCES = """
public final class Preferences {
    public static final class ApiDomainOption {
        public static final ApiDomainOption PRIMARY =
            new ApiDomainOption("PRIMARY", 0, "www.example.com", "(Primary)");
        public static final ApiDomainOption PRIMARY_ALIAS =
            new ApiDomainOption("PRIMARY_ALIAS", 1, "www.example.com", "(Alias)");
        public static final ApiDomainOption BACKUP =
            new ApiDomainOption("BACKUP", 2, "backup.example.com", null, 2, null);
        private static final String DEFAULT;
        static {
            DEFAULT = PRIMARY.entryKey;
            arrayList.add(apiDomainOption.entryKey + ' ' + apiDomainOption.description);
        }
    }

    public enum ImageCDNDomainOption {
        DEFAULT_OPTION("default", "(Disabled)"),
        HONG_KONG("img-hk.example-cdn.com", "(Hong Kong)");
        private static final String DEFAULT;
        static {
            DEFAULT = DEFAULT_OPTION.entryKey;
            arrayList.add(imageCDNDomainOption.entryKey + ' ' + imageCDNDomainOption.description);
        }
    }

    public enum CCOption {
        Simplified("Simplified", "zh-cn"),
        Traditional("Traditional", "zh-tw");
        private static final String DEFAULT;
        static { DEFAULT = Simplified.entryKey; }
    }

    public Preference[] initPreferences(Context context) {
        EditTextPreference plugin = new EditTextPreference(context);
        plugin.setKey("v1.key.plugin_info");
        plugin.setTitle("Plugin info");
        plugin.setDefaultValue("metadata");
        ListPreference domain = new ListPreference(context);
        domain.setKey("v1.key.api_option");
        domain.setTitle("API Domain");
        domain.setEntries(ApiDomainOption.INSTANCE.getENTRY());
        domain.setEntryValues(ApiDomainOption.INSTANCE.getENTRY_KEYS());
        domain.setDefaultValue(ApiDomainOption.INSTANCE.getDEFAULT());
        ListPreference cdn = new ListPreference(context);
        cdn.setKey("v1.key.image_cdn_option");
        cdn.setTitle("Image CDN");
        cdn.setEntries(ImageCDNDomainOption.INSTANCE.getENTRY());
        cdn.setEntryValues(ImageCDNDomainOption.INSTANCE.getENTRY_KEYS());
        cdn.setDefaultValue(ImageCDNDomainOption.INSTANCE.getDEFAULT());
        SwitchPreferenceCompat watermark = new SwitchPreferenceCompat(context);
        watermark.setKey("v1.key.anti_watermark");
        watermark.setTitle("Remove watermark");
        watermark.setDefaultValue(true);
        ListPreference language = new ListPreference(context);
        language.setKey("v1.key.cc_option");
        language.setTitle("Chinese conversion");
        language.setEntries(CCOption.INSTANCE.getENTRY());
        language.setEntryValues(CCOption.INSTANCE.getENTRY_KEYS());
        language.setDefaultValue(CCOption.INSTANCE.getDEFAULT());
        return new Preference[]{
            (Preference) plugin,
            (Preference) domain,
            (Preference) cdn,
            (Preference) watermark,
            (Preference) language
        };
    }
}
"""


def _ir(preferences: str = PREFERENCES):
    return minimal_source_ir(
        source_format="decompiled_apk",
        feature_scope="public_only",
        capabilities=[Capability.SETTINGS],
        files=[
            SourceFile(path="sources/example/PreferencesKt.java", content=preferences, sha256="0"),
            SourceFile(path="sources/example/DomainOption.java", content=DOMAIN_OPTION, sha256="1"),
            SourceFile(path="sources/example/PreferencesKeys.java", content=CONSTANTS, sha256="2"),
            SourceFile(path="sources/example/PluginMetaData.java", content=METADATA, sha256="3"),
        ],
    )


def test_recovers_complete_public_decompiled_settings() -> None:
    generated = deterministic_decompiled_settings(_ir())

    assert generated is not None
    assert generated.path == "res/settings.json"
    document = json.loads(generated.content)
    assert document[0]["title"] == "Example"
    assert document[0]["items"] == [
        {
            "key": "v2.pref.api_domain",
            "title": "API Domain",
            "type": "select",
            "titles": ["Primary", "Backup"],
            "values": ["api.example", "backup.example"],
            "default": "api.example",
            "subtitle": "Choose endpoint",
        },
        {
            "key": "v2.key.user_agent",
            "title": "User-Agent",
            "type": "text",
            "default": "Example/1.0",
        },
    ]


def test_recovers_inline_jadx_preferences_and_excludes_non_reading_settings() -> None:
    ir = minimal_source_ir(
        source_format="decompiled_apk",
        feature_scope="public_only",
        capabilities=[Capability.SETTINGS],
        files=[
            SourceFile(
                path="sources/example/Preferences.java",
                content=INLINE_JADX_PREFERENCES,
                sha256="0",
            )
        ],
    )

    generated = deterministic_decompiled_settings(ir)

    assert generated is not None
    document = json.loads(generated.content)
    assert document[0]["items"] == [
        {
            "key": "v1.key.api_option",
            "title": "API Domain",
            "type": "select",
            "titles": ["www.example.com (Primary)", "backup.example.com"],
            "values": ["www.example.com", "backup.example.com"],
            "default": "www.example.com",
        },
        {
            "key": "v1.key.image_cdn_option",
            "title": "Image CDN",
            "type": "select",
            "titles": ["default (Disabled)", "img-hk.example-cdn.com (Hong Kong)"],
            "values": ["default", "img-hk.example-cdn.com"],
            "default": "default",
        },
    ]


def test_incomplete_public_setting_falls_back_to_ai() -> None:
    incomplete = PREFERENCES.replace('domain.setTitle("API Domain");', "")

    assert deterministic_decompiled_settings(_ir(incomplete)) is None


def test_preserves_an_existing_settings_resource() -> None:
    existing = GeneratedFile(
        path="res/settings.json",
        content='[{"type":"group","items":[{"type":"text","key":"existing"}]}]',
    )
    manifest = GenerationManifest(
        source_struct="Example",
        files=[GeneratedFile(path="src/lib.rs", content="#![no_std]"), existing],
    )

    assert with_deterministic_decompiled_settings(_ir(), manifest) is manifest
