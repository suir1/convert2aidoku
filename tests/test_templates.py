from pathlib import Path

from convert2aidoku.analyzer import analyze_path
from convert2aidoku.dependency_policy import AIDOKU_RS_REV
from convert2aidoku.templates import builtin_templates, match_templates

FIXTURE = Path(__file__).parent / "fixtures" / "simple"
ENCRYPTED_API_FIXTURE = Path(__file__).parent / "fixtures" / "encrypted_api"


def test_builtin_templates_are_pinned_and_source_agnostic() -> None:
    templates = builtin_templates()

    assert templates
    assert {item.aidoku_revision for item in templates} == {AIDOKU_RS_REV}
    assert all("source-specific" in item.provenance for item in templates)
    assert all(item.license_note for item in templates)


def test_matches_fixture_capabilities_and_reports_slots() -> None:
    ir = analyze_path(str(FIXTURE))

    matches = match_templates(ir)
    by_id = {item.template_id: item for item in matches}

    assert by_id["html-http-source"].ready
    assert by_id["listing-provider"].ready
    assert set(by_id["listing-provider"].matched_capabilities) >= {
        "popular",
        "latest",
    }
    assert by_id["image-request-provider"].ready
    assert not by_id["filter-query-builder"].ready
    assert not by_id["settings-resource"].ready
    assert by_id["html-http-source"].score == 1.0
    assert {slot.name for slot in by_id["html-http-source"].slots} >= {
        "list_selector",
        "page_selector",
        "key_strategy",
    }


def test_missing_capability_keeps_template_not_ready() -> None:
    ir = analyze_path(str(FIXTURE)).model_copy(update={"capabilities": []})

    matches = {item.template_id: item for item in match_templates(ir)}

    assert not matches["html-http-source"].ready
    assert matches["html-http-source"].missing_capabilities
    assert matches["html-http-source"].score == 0.0


def test_matches_encrypted_json_api_templates() -> None:
    ir = analyze_path(str(ENCRYPTED_API_FIXTURE))
    matches = {item.template_id: item for item in match_templates(ir)}

    assert matches["json-api-source"].ready
    assert matches["aes-cbc-json"].ready
    assert matches["dynamic-base-url"].ready
