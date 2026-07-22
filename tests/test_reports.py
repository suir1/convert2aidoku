from pathlib import Path

from convert2aidoku.models import (
    ConversionReport,
    ConversionStatus,
    StageKind,
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
