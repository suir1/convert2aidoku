import pytest

from convert2aidoku.normalization_trace import NormalizationTrace


def test_trace_counts_only_rewrites_that_change_content() -> None:
    trace = NormalizationTrace()

    unchanged = trace.apply("same", "value", lambda value: value)
    changed = trace.apply("upper", "value", str.upper)
    trace.apply("upper", "other", str.upper)

    assert unchanged == "value"
    assert changed == "VALUE"
    assert trace.counts == {"upper": 2}
    assert trace.rule_ids == {"same", "upper"}


def test_trace_merges_counts_without_exposing_mutable_state() -> None:
    trace = NormalizationTrace()
    trace.merge({"second": 2, "first": 1})

    counts = trace.counts
    counts["first"] = 99

    assert trace.counts == {"first": 1, "second": 2}


def test_trace_rejects_unstable_ids_and_negative_counts() -> None:
    trace = NormalizationTrace()

    with pytest.raises(ValueError, match="invalid normalization rule id"):
        trace.apply("Not Stable", "value", str.upper)
    with pytest.raises(ValueError, match="cannot be negative"):
        trace.merge({"valid": -1})
