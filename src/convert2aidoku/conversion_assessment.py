from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .ai import initial_generation_request_characters
from .errors import C2AError, UnsupportedSourceError
from .generation_context import build_generation_context, build_settings_context
from .listing_renderer import deterministic_search_listing_available
from .models import Capability, SourceIR
from .source_rules import PREFLIGHT_RULE_IDS, validate_rule_ids

AssessmentStatus = Literal["ready", "caution", "blocked"]

_CORE_CAPABILITIES = (
    Capability.DETAILS,
    Capability.CHAPTERS,
    Capability.PAGES,
)
_LISTING_CAPABILITIES = (
    Capability.SEARCH,
    Capability.POPULAR,
    Capability.LATEST,
)
_CRYPTO_CAPABILITIES = (
    Capability.ENCRYPTED_JSON,
    Capability.TRIPLE_DES_CBC,
    Capability.MD5_REQUEST_SIGNING,
    Capability.RSA_PKCS1_V15,
)


class TokenBudgetEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["request_characters"] = "request_characters"
    request_characters: int = Field(ge=0)
    initial_prompt_tokens_min: int = Field(ge=0)
    initial_prompt_tokens_max: int = Field(ge=0)
    recommended_total_tokens_min: int = Field(ge=0)
    recommended_total_tokens_max: int = Field(ge=0)


class ConversionAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AssessmentStatus
    score: int = Field(ge=0, le=100)
    eligible: bool
    strengths: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    blocking_rule_ids: list[str] = Field(default_factory=list)
    token_budget: TokenBudgetEstimate | None = None


def _token_budget(request_characters: int) -> TokenBudgetEstimate:
    prompt_min = max(1_000, round(request_characters / 5.7))
    prompt_max = max(prompt_min + 1_000, round(request_characters / 3.2))
    total_min = prompt_min + max(3_000, round(prompt_min * 0.12))
    total_max = prompt_max + max(12_000, round(prompt_max * 0.32))
    return TokenBudgetEstimate(
        request_characters=request_characters,
        initial_prompt_tokens_min=prompt_min,
        initial_prompt_tokens_max=prompt_max,
        recommended_total_tokens_min=total_min,
        recommended_total_tokens_max=total_max,
    )


def assess_source_ir(ir: SourceIR) -> ConversionAssessment:
    """Assess evidence readiness before any provider request is allowed."""
    score = 100
    strengths: list[str] = []
    risks: list[str] = []
    blockers: list[str] = []
    rule_ids: list[str] = []
    blocking_rule_ids: list[str] = []

    def record(rule_id: str, *, blocking: bool = False) -> None:
        rule_ids.append(rule_id)
        if blocking:
            blocking_rule_ids.append(rule_id)

    missing_core = [
        capability.value for capability in _CORE_CAPABILITIES if capability not in ir.capabilities
    ]
    if missing_core:
        score -= 22 * len(missing_core)
        blockers.append("missing core reading behavior: " + ", ".join(missing_core))
        record("preflight_missing_core", blocking=True)
    else:
        strengths.append("details, chapters, and pages were recovered")

    if any(capability in ir.capabilities for capability in _LISTING_CAPABILITIES):
        strengths.append("at least one manga listing entry point was recovered")
    else:
        score -= 18
        blockers.append("no search, popular, or latest listing entry point was recovered")
        record("preflight_missing_listing", blocking=True)

    if not ir.files:
        score -= 35
        blockers.append("analysis produced no source evidence files")
        record("preflight_missing_files", blocking=True)

    token_budget: TokenBudgetEstimate | None = None
    try:
        context = build_generation_context(ir)
        request_characters = initial_generation_request_characters(ir)
        token_budget = _token_budget(request_characters)
        strengths.append(
            f"generation context contains {context.context_stats['evidence_files']} evidence files"
        )
        budget_omissions = [
            item
            for item in context.omitted_source_files
            if item.get("reason") == "generation_evidence_budget"
        ]
        if budget_omissions:
            score -= min(16, 4 + len(budget_omissions))
            risks.append(
                f"{len(budget_omissions)} source files exceed the bounded generation context"
            )
            record("preflight_evidence_budget")
    except (C2AError, ValueError) as exc:
        score -= 35
        blockers.append(f"generation context cannot be constructed: {exc}")
        record("preflight_generation_context", blocking=True)

    if ir.source_format == "decompiled_apk":
        score -= 5
        risks.append("APK behavior is recovered from JADX output and requires live validation")
        record("preflight_decompiled_input")
    if ir.feature_scope == "public_only":
        score -= 2
        risks.append("authenticated and Android-only features are outside public-reading scope")
        record("preflight_public_only")
    if ir.unsupported_features:
        score -= min(4, len(ir.unsupported_features))
        risks.append(f"{len(ir.unsupported_features)} optional features will be excluded")
        record("preflight_excluded_features")

    if Capability.DYNAMIC_FILTERS in ir.capabilities:
        score -= 2
        risks.append("dynamic filters require an additional live endpoint")
        record("preflight_dynamic_filters")
    if Capability.DYNAMIC_BASE_URLS in ir.capabilities:
        score -= 2
        risks.append("runtime domain selection must remain inside a recovered allowlist")
        record("preflight_dynamic_base_urls")
    if Capability.IMAGE_HEADERS in ir.capabilities:
        score -= 2
        risks.append("page images require source-specific request behavior")
        record("preflight_image_headers")
    crypto = [
        capability.value for capability in _CRYPTO_CAPABILITIES if capability in ir.capabilities
    ]
    if crypto:
        score -= 5 * len(crypto)
        risks.append(
            "supported cryptography still increases implementation risk: " + ", ".join(crypto)
        )
        record("preflight_supported_crypto")

    if Capability.FILTERS in ir.capabilities and not ir.filter_specs:
        if Capability.DYNAMIC_FILTERS not in ir.capabilities:
            score -= 8
            risks.append(
                "filter capability was detected but no stable filter contract was recovered"
            )
            record("preflight_missing_filter_contract")
    elif ir.filter_specs:
        strengths.append(f"{len(ir.filter_specs)} stable filter contracts were recovered")

    if Capability.SETTINGS in ir.capabilities:
        settings = build_settings_context(ir)
        if not settings["settings_evidence"]:
            score -= 6
            risks.append("settings were detected but no focused settings evidence was recovered")
            record("preflight_missing_settings_evidence")
        else:
            strengths.append("focused source-setting evidence was recovered")

    if deterministic_search_listing_available(ir):
        score += 6
        strengths.append("listing Rust can be rendered deterministically without AI")
        record("preflight_deterministic_listing")

    score = max(0, min(100, score))
    if blockers:
        score = min(score, 49)
    elif score < 60:
        record("preflight_score_threshold", blocking=True)
    eligible = not blockers and score >= 60
    status: AssessmentStatus
    if not eligible:
        status = "blocked"
    elif score >= 85:
        status = "ready"
    else:
        status = "caution"
    rule_ids = validate_rule_ids(rule_ids, PREFLIGHT_RULE_IDS, domain="preflight")
    blocking_rule_ids = validate_rule_ids(
        blocking_rule_ids,
        PREFLIGHT_RULE_IDS,
        domain="preflight blocker",
    )
    return ConversionAssessment(
        status=status,
        score=score,
        eligible=eligible,
        strengths=list(dict.fromkeys(strengths)),
        risks=list(dict.fromkeys(risks)),
        blockers=list(dict.fromkeys(blockers)),
        rule_ids=rule_ids,
        blocking_rule_ids=blocking_rule_ids,
        token_budget=token_budget,
    )


def require_ai_eligible(ir: SourceIR) -> ConversionAssessment:
    assessment = assess_source_ir(ir)
    if assessment.eligible:
        return assessment
    detail = "; ".join(assessment.blockers or assessment.risks)
    raise UnsupportedSourceError(
        f"conversion preflight blocked before any AI request (score {assessment.score}/100): "
        + detail,
        rule_ids=tuple(assessment.blocking_rule_ids),
    )
