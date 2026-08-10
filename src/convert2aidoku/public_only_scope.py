from __future__ import annotations

import re

_SETTING_EXCLUSIONS = (
    (
        (
            ".enable_login",
            ".login_credentials",
            ".copy_manga_token",
            ".hot_manga_token",
        ),
        "login/authentication (excluded by public-only APK scope)",
    ),
    (
        (".web_view_link", ".web_view_link_custom", ".web_view_client"),
        "WebView login/navigation settings (excluded by public-only APK scope)",
    ),
    (
        (".lan_option",),
        "Android ChineseUtils script conversion setting (excluded by public-only APK scope)",
    ),
    (
        (".hide_default_continuous_chapter",),
        "library-update chapter hiding setting (excluded by public-only APK scope)",
    ),
    (
        (
            ".show_manga_comments",
            ".show_chapter_comments",
            ".chapter_comment_perform",
            ".chapter_comment_perform_mode",
            ".reserve_chapter_comments",
            ".chapter_comment_api",
            ".chapter_comment_api_custom",
            ".chapter_comment_api_domain",
            ".chapter_comment_api_domain_custom",
            ".comment_api_domain",
            ".comment_api_domain_custom",
        ),
        "chapter comments (excluded by public-only APK scope)",
    ),
    (
        (".extension_update_link", ".only_update_link"),
        "Android extension information preference (excluded by public-only APK scope)",
    ),
)
_AUTHENTICATED_FILTER_MARKERS = (
    "login",
    "collect",
    "bookcase",
    "登入",
    "登录",
    "收藏",
    "书柜",
    "書櫃",
)


def public_only_setting_exclusion(key: str) -> str | None:
    """Return why an Android/UI preference is outside anonymous reading scope."""
    lowered = key.casefold()
    for suffixes, reason in _SETTING_EXCLUSIONS:
        if lowered.endswith(suffixes):
            return reason
    return None


def public_only_setting_reference_exclusion(reference: str) -> str | None:
    """Classify an unresolved Option.KEY reference without inventing its storage key."""
    found = re.fullmatch(
        r"(?P<class>[A-Za-z_]\w*?)(?:Option)?\.(?P<key>KEY(?:_CUSTOM)?)",
        reference.strip(),
    )
    if found is None:
        return None
    class_name = re.sub(r"(?<!^)(?=[A-Z])", "_", found.group("class")).lower()
    custom = "_custom" if found.group("key") == "KEY_CUSTOM" else ""
    return public_only_setting_exclusion(f"source.{class_name}{custom}")


def public_only_setting_exclusions(content: str) -> list[str]:
    keys = re.findall(r'"(v\d+\.(?:pref|key)\.[^"\\]+)"', content)
    return list(
        dict.fromkeys(
            reason for key in keys if (reason := public_only_setting_exclusion(key)) is not None
        )
    )


def public_only_filter_exclusion(source_class: str, title: str) -> str | None:
    """Exclude only migration filters whose recovered label declares authenticated state."""
    if "migrate" not in source_class.casefold():
        return None
    lowered_title = title.casefold()
    if not any(marker in lowered_title for marker in _AUTHENTICATED_FILTER_MARKERS):
        return None
    return "authenticated collection/bookcase migration filter (excluded by public-only APK scope)"
