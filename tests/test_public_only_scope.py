from convert2aidoku.public_only_scope import (
    public_only_setting_exclusion,
    public_only_setting_reference_exclusion,
)


def test_public_only_scope_excludes_android_ui_and_comment_preferences() -> None:
    assert public_only_setting_exclusion("v2.key.enable_login") == (
        "login/authentication (excluded by public-only APK scope)"
    )
    assert public_only_setting_exclusion("v2.key.login_credentials") == (
        "login/authentication (excluded by public-only APK scope)"
    )
    assert public_only_setting_exclusion("v2.key.show_chapter_comments") == (
        "chapter comments (excluded by public-only APK scope)"
    )
    assert public_only_setting_exclusion("v2.pref.chapter_comment_api_domain_custom") == (
        "chapter comments (excluded by public-only APK scope)"
    )
    assert public_only_setting_exclusion("v2.pref.chapter_comment_api") == (
        "chapter comments (excluded by public-only APK scope)"
    )
    assert public_only_setting_exclusion("v2.pref.chapter_comment_api_custom") == (
        "chapter comments (excluded by public-only APK scope)"
    )
    assert public_only_setting_exclusion("v2.pref.chapter_comment_perform_mode") == (
        "chapter comments (excluded by public-only APK scope)"
    )
    assert public_only_setting_exclusion("v2.pref.comment_api_domain_custom") == (
        "chapter comments (excluded by public-only APK scope)"
    )
    assert public_only_setting_exclusion("v2.key.extension_update_link") == (
        "Android extension information preference (excluded by public-only APK scope)"
    )
    assert public_only_setting_reference_exclusion("ChapterCommentApiOption.KEY_CUSTOM") == (
        "chapter comments (excluded by public-only APK scope)"
    )


def test_public_only_scope_keeps_public_network_preferences() -> None:
    assert public_only_setting_exclusion("v2.pref.api_domain") is None
    assert public_only_setting_exclusion("v2.pref.resolution") is None
