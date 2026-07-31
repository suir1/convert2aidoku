from __future__ import annotations

from collections.abc import Iterable

from .models import Capability


def capability_rule_id(capability: Capability) -> str:
    return f"capability_{capability.value}"


CAPABILITY_RULE_IDS = frozenset(capability_rule_id(capability) for capability in Capability)

SOURCE_ANALYSIS_RULE_IDS = CAPABILITY_RULE_IDS | frozenset(
    {
        "exclude_public_only_features",
        "relative_url_keys",
        "warn_missing_input_license",
        "warn_okhttp_interceptor",
    }
)

SOURCE_BLOCK_RULE_IDS = frozenset(
    {
        "unsupported_authentication",
        "unsupported_crypto",
        "unsupported_custom_source_base",
        "unsupported_custom_web_processing",
        "unsupported_image_processing",
        "unsupported_multisrc_theme",
        "unsupported_no_standalone_http_source",
    }
)

PREFLIGHT_RULE_IDS = frozenset(
    {
        "preflight_decompiled_input",
        "preflight_deterministic_listing",
        "preflight_dynamic_base_urls",
        "preflight_dynamic_filters",
        "preflight_evidence_budget",
        "preflight_excluded_features",
        "preflight_generation_context",
        "preflight_image_headers",
        "preflight_missing_core",
        "preflight_missing_files",
        "preflight_missing_filter_contract",
        "preflight_missing_listing",
        "preflight_missing_settings_evidence",
        "preflight_public_only",
        "preflight_score_threshold",
        "preflight_supported_crypto",
    }
)


def validate_rule_ids(
    rule_ids: Iterable[str],
    catalog: frozenset[str],
    *,
    domain: str,
) -> list[str]:
    unique = list(dict.fromkeys(rule_ids))
    unknown = sorted(set(unique) - catalog)
    if unknown:
        raise ValueError(f"unregistered {domain} rules: " + ", ".join(unknown))
    return unique
