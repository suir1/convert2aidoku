from pathlib import Path

from convert2aidoku.models import (
    ConversionReport,
    ConversionStatus,
    StageKind,
    ValidationBlocker,
    ValidationResult,
    ValidationStage,
)
from convert2aidoku.reports import classify_status, write_report
from convert2aidoku.templates import builtin_templates


def test_status_classification() -> None:
    verified = ValidationResult(build_ok=True, package_ok=True, live_ok=True)
    assert classify_status(verified, live_requested=True) is ConversionStatus.VERIFIED

    build = ValidationResult(build_ok=True, package_ok=True)
    assert classify_status(build, live_requested=False) is ConversionStatus.BUILD_ONLY

    blocked = ValidationResult(build_ok=True, blocked=True)
    assert classify_status(blocked, live_requested=True) is ConversionStatus.BLOCKED

    incomplete = ValidationResult(build_ok=True, package_ok=True, live_ok=True, contract_ok=False)
    assert classify_status(incomplete, live_requested=True) is ConversionStatus.BUILD_ONLY

    unverified_package = ValidationResult(build_ok=True, package_ok=False)
    assert classify_status(unverified_package, live_requested=False) is ConversionStatus.FAILED

    live_failure = ValidationResult(
        build_ok=True,
        package_ok=True,
        stages=[
            ValidationStage(
                name="core-live-smoke",
                kind=StageKind.LIVE_TEST,
                ok=False,
                output="search/list returned no manga",
            )
        ],
    )
    assert classify_status(live_failure, live_requested=True) is ConversionStatus.FAILED

    assert classify_status(ValidationResult(), live_requested=True) is ConversionStatus.FAILED


def test_blocked_report_describes_runner_environment(tmp_path: Path) -> None:
    report = ConversionReport(
        status=ConversionStatus.BLOCKED,
        input_ref="source",
        source_id="en.example",
        validation=ValidationResult(blocked=True),
    )

    write_report(tmp_path, report)

    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "CLI/test-runner network environment" in markdown
    assert "does not mean the source site is globally unavailable" in markdown


def test_blocked_report_describes_anonymous_initialization_requirement(tmp_path: Path) -> None:
    report = ConversionReport(
        status=ConversionStatus.BLOCKED,
        input_ref="source",
        source_id="zh.manhuaren",
        validation=ValidationResult(
            blocked=True,
            blocker_reason=ValidationBlocker.ANONYMOUS_INITIALIZATION,
            stages=[
                ValidationStage(
                    name="core-live-smoke",
                    kind=StageKind.LIVE_TEST,
                    ok=False,
                    output='errorResponse: {"message":"初始化失败"}',
                    blocked=True,
                    blocker_reason=ValidationBlocker.ANONYMOUS_INITIALIZATION,
                )
            ],
        ),
    )

    write_report(tmp_path, report)

    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "remote API rejected anonymous-device initialization" in markdown
    assert "valid User ID and Token" in markdown
    assert "Validation blocker: anonymous_initialization" in markdown


def test_report_lists_template_matches(tmp_path: Path) -> None:
    template = builtin_templates()[0]
    report = ConversionReport(
        status=ConversionStatus.BUILD_ONLY,
        input_ref="source",
        source_id="en.example",
        template_matches=[
            {
                "template_id": template.template_id,
                "aidoku_revision": template.aidoku_revision,
                "ready": True,
                "score": 1.0,
                "slots": template.slots,
                "provenance": template.provenance,
                "license_note": template.license_note,
            }
        ],
        validation=ValidationResult(),
    )

    write_report(tmp_path, report)

    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "## Templates" in markdown
    assert "html-http-source" in markdown


def test_report_aggregates_deterministic_rewrites_across_ai_rounds(tmp_path: Path) -> None:
    report = ConversionReport(
        status=ConversionStatus.BUILD_ONLY,
        input_ref="source",
        source_id="en.example",
        ai_rounds=[
            {
                "round": 1,
                "purpose": "generate",
                "structured_output": True,
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                    "reasoning_tokens": 30,
                    "cached_prompt_tokens": 40,
                },
                "normalization_rewrites": {"safe_std_paths": 2, "allow_dead_code": 1},
                "projection_rewrites": {"setting_defaults": 2},
                "contract_rule_ids": ["missing_retry", "missing_settings"],
            },
            {
                "round": 2,
                "purpose": "repair",
                "repair_mode": "compiler_patch",
                "structured_output": True,
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                    "reasoning_tokens": 5,
                    "cached_prompt_tokens": 10,
                },
                "normalization_rewrites": {"safe_std_paths": 1},
                "contract_rule_ids": ["missing_retry"],
            },
        ],
        source_analysis_rule_ids=["capability_search", "relative_url_keys"],
        preflight_rule_ids=["preflight_deterministic_listing"],
        failed_ai_exchanges=[
            {
                "purpose": "repair",
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "reasoning_tokens": 4,
                    "cached_prompt_tokens": 3,
                },
                "diagnostics": ["invalid patch"],
            }
        ],
        validation=ValidationResult(),
    )

    write_report(tmp_path, report)

    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Normalizer rewrite hits: 6" in markdown
    assert (
        "AI tokens: 195 total (130 prompt, 65 completion, 39 reasoning, 53 cached prompt)"
        in markdown
    )
    assert "Repair rounds: 1 (1 targeted, 0 full, 0 unclassified)" in markdown
    assert "Contract rule triggers: 3" in markdown
    assert "Source analysis rules: 2" in markdown
    assert "Preflight rules: 1" in markdown
    assert "`safe_std_paths`: 3 generated file(s) changed" in markdown
    assert "`setting_defaults`: 2 generated file(s) changed" in markdown
    assert "`allow_dead_code`: 1 generated file(s) changed" in markdown
    assert "`missing_retry`: 2 round(s)" in markdown
    assert "`missing_settings`: 1 round(s)" in markdown
    assert "`capability_search`" in markdown
    assert "`preflight_deterministic_listing`" in markdown


def test_report_distinguishes_deterministic_generation_from_ai_calls(tmp_path: Path) -> None:
    report = ConversionReport(
        status=ConversionStatus.VERIFIED,
        input_ref="source",
        source_id="en.example",
        ai_rounds=[
            {
                "round": 1,
                "purpose": "generate",
                "structured_output": True,
                "provider_called": False,
            }
        ],
        validation=ValidationResult(build_ok=True, package_ok=True, live_ok=True),
    )

    write_report(tmp_path, report)

    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "AI calls: 0" in markdown
    assert "Model: `not used`" in markdown
