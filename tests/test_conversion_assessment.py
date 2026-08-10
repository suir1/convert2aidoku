from types import SimpleNamespace

import pytest

from convert2aidoku import conversion_assessment
from convert2aidoku.analyzer import analyze_path
from convert2aidoku.conversion_assessment import assess_source_ir, require_ai_eligible
from convert2aidoku.errors import UnsupportedSourceError
from convert2aidoku.models import Capability
from convert2aidoku.source_rules import PREFLIGHT_RULE_IDS
from tests.scenarios import SIMPLE_FIXTURE


def test_supported_http_source_is_eligible_with_a_bounded_token_budget() -> None:
    ir = analyze_path(str(SIMPLE_FIXTURE))

    assessment = assess_source_ir(ir)

    assert assessment.status == "ready"
    assert assessment.score >= 85
    assert assessment.eligible
    assert not assessment.blockers
    assert "preflight_image_headers" in assessment.rule_ids
    assert not assessment.blocking_rule_ids
    assert assessment.token_budget is not None
    budget = assessment.token_budget
    assert budget.initial_prompt_tokens_min < budget.initial_prompt_tokens_max
    assert budget.recommended_total_tokens_min < budget.recommended_total_tokens_max
    assert budget.recommended_total_tokens_max > budget.initial_prompt_tokens_max


def test_deterministic_generation_reports_a_zero_token_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ir = analyze_path(str(SIMPLE_FIXTURE))
    monkeypatch.setattr(
        conversion_assessment,
        "initial_generation_request_characters",
        lambda _ir: 0,
    )

    budget = assess_source_ir(ir).token_budget

    assert budget is not None
    assert budget.request_characters == 0
    assert budget.initial_prompt_tokens_min == 0
    assert budget.initial_prompt_tokens_max == 0
    assert budget.recommended_total_tokens_min == 0
    assert budget.recommended_total_tokens_max == 0


def test_missing_core_reading_behavior_is_blocked_before_ai() -> None:
    ir = analyze_path(str(SIMPLE_FIXTURE))
    ir = ir.model_copy(
        update={
            "capabilities": [
                capability for capability in ir.capabilities if capability is not Capability.PAGES
            ]
        }
    )

    assessment = assess_source_ir(ir)

    assert assessment.status == "blocked"
    assert assessment.score <= 49
    assert not assessment.eligible
    assert any("pages" in blocker for blocker in assessment.blockers)
    assert "preflight_missing_core" in assessment.blocking_rule_ids


def test_preflight_error_states_that_no_provider_request_was_made() -> None:
    ir = analyze_path(str(SIMPLE_FIXTURE)).model_copy(update={"capabilities": []})

    try:
        require_ai_eligible(ir)
    except UnsupportedSourceError as exc:
        message = str(exc)
        rule_ids = exc.rule_ids
    else:
        raise AssertionError("ineligible SourceIR unexpectedly passed preflight")

    assert "before any AI request" in message
    assert "score" in message
    assert "preflight_missing_core" in rule_ids
    assert "preflight_missing_listing" in rule_ids


def test_missing_source_files_are_a_stable_preflight_blocker() -> None:
    ir = analyze_path(str(SIMPLE_FIXTURE)).model_copy(update={"files": []})

    assessment = assess_source_ir(ir)

    assert "preflight_missing_files" in assessment.blocking_rule_ids
    assert "preflight_generation_context" not in assessment.blocking_rule_ids


def test_every_preflight_rule_has_an_executable_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = analyze_path(str(SIMPLE_FIXTURE))
    observed: set[str] = set()

    monkeypatch.setattr(
        conversion_assessment,
        "build_generation_context",
        lambda _ir: SimpleNamespace(
            context_stats={"evidence_files": 1},
            omitted_source_files=[{"reason": "generation_evidence_budget"} for _ in range(12)],
        ),
    )
    monkeypatch.setattr(
        conversion_assessment,
        "build_settings_context",
        lambda _ir: {"settings_evidence": []},
    )
    monkeypatch.setattr(
        conversion_assessment,
        "deterministic_search_listing_available",
        lambda _ir: True,
    )

    risky_capabilities = list(
        dict.fromkeys(
            [
                *base.capabilities,
                Capability.DYNAMIC_FILTERS,
                Capability.DYNAMIC_BASE_URLS,
                Capability.SETTINGS,
                Capability.ENCRYPTED_JSON,
                Capability.TRIPLE_DES_CBC,
                Capability.MD5_REQUEST_SIGNING,
                Capability.RSA_PKCS1_V15,
            ]
        )
    )
    risky = base.model_copy(
        update={
            "source_format": "decompiled_apk",
            "feature_scope": "public_only",
            "unsupported_features": [f"excluded-{index}" for index in range(4)],
            "capabilities": risky_capabilities,
            "filter_specs": [],
        }
    )
    observed.update(assess_source_ir(risky).rule_ids)

    no_filter_contract = base.model_copy(
        update={
            "capabilities": [*base.capabilities, Capability.FILTERS],
            "filter_specs": [],
        }
    )
    observed.update(assess_source_ir(no_filter_contract).rule_ids)

    blockers = base.model_copy(
        update={
            "capabilities": [
                capability
                for capability in base.capabilities
                if capability
                not in {
                    Capability.SEARCH,
                    Capability.POPULAR,
                    Capability.LATEST,
                    Capability.PAGES,
                }
            ],
            "files": [],
        }
    )
    observed.update(assess_source_ir(blockers).rule_ids)

    def fail_generation_context(_ir: object) -> None:
        raise ValueError("synthetic context failure")

    monkeypatch.setattr(
        conversion_assessment,
        "build_generation_context",
        fail_generation_context,
    )
    observed.update(assess_source_ir(base).rule_ids)

    assert observed == PREFLIGHT_RULE_IDS
