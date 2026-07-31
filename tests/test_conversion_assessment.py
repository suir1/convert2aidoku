from convert2aidoku.analyzer import analyze_path
from convert2aidoku.conversion_assessment import assess_source_ir, require_ai_eligible
from convert2aidoku.errors import UnsupportedSourceError
from convert2aidoku.models import Capability
from tests.scenarios import SIMPLE_FIXTURE


def test_supported_http_source_is_eligible_with_a_bounded_token_budget() -> None:
    ir = analyze_path(str(SIMPLE_FIXTURE))

    assessment = assess_source_ir(ir)

    assert assessment.status == "ready"
    assert assessment.score >= 85
    assert assessment.eligible
    assert not assessment.blockers
    assert assessment.token_budget is not None
    budget = assessment.token_budget
    assert budget.initial_prompt_tokens_min < budget.initial_prompt_tokens_max
    assert budget.recommended_total_tokens_min < budget.recommended_total_tokens_max
    assert budget.recommended_total_tokens_max > budget.initial_prompt_tokens_max


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


def test_preflight_error_states_that_no_provider_request_was_made() -> None:
    ir = analyze_path(str(SIMPLE_FIXTURE)).model_copy(update={"capabilities": []})

    try:
        require_ai_eligible(ir)
    except UnsupportedSourceError as exc:
        message = str(exc)
    else:
        raise AssertionError("ineligible SourceIR unexpectedly passed preflight")

    assert "before any AI request" in message
    assert "score" in message
