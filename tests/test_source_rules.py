import pytest

from convert2aidoku.source_rules import (
    PREFLIGHT_RULE_IDS,
    SOURCE_ANALYSIS_RULE_IDS,
    SOURCE_BLOCK_RULE_IDS,
    validate_rule_ids,
)


def test_source_rule_domains_are_distinct() -> None:
    assert SOURCE_BLOCK_RULE_IDS.isdisjoint(SOURCE_ANALYSIS_RULE_IDS)
    assert SOURCE_BLOCK_RULE_IDS.isdisjoint(PREFLIGHT_RULE_IDS)
    assert SOURCE_ANALYSIS_RULE_IDS.isdisjoint(PREFLIGHT_RULE_IDS)


def test_rule_validation_preserves_order_and_rejects_unknown_ids() -> None:
    rule_ids = list(SOURCE_BLOCK_RULE_IDS)[:2]

    assert (
        validate_rule_ids(
            [*rule_ids, rule_ids[0]],
            SOURCE_BLOCK_RULE_IDS,
            domain="source blocker",
        )
        == rule_ids
    )
    with pytest.raises(ValueError, match="unregistered source blocker rules"):
        validate_rule_ids(["unknown"], SOURCE_BLOCK_RULE_IDS, domain="source blocker")
