from __future__ import annotations

from collections import Counter
from pathlib import Path

from .models import ConversionReport, ConversionStatus, StageKind, ValidationResult


def classify_status(validation: ValidationResult, *, live_requested: bool) -> ConversionStatus:
    if validation.blocked:
        return ConversionStatus.BLOCKED
    if validation.build_ok and validation.package_ok:
        if not validation.contract_ok:
            return ConversionStatus.BUILD_ONLY
        if not live_requested:
            return ConversionStatus.BUILD_ONLY
        if validation.live_ok:
            return ConversionStatus.VERIFIED
        if any(
            stage.kind is StageKind.LIVE_TEST and not stage.ok and not stage.skipped
            for stage in validation.stages
        ):
            return ConversionStatus.FAILED
        return ConversionStatus.BUILD_ONLY
    return ConversionStatus.FAILED


def _blocked_status_context(report: ConversionReport) -> str:
    live_output = "\n".join(
        stage.output
        for stage in report.validation.stages
        if stage.kind is StageKind.LIVE_TEST and not stage.ok
    )
    if "初始化失败" in live_output:
        return (
            "`blocked` means the remote API rejected anonymous-device initialization. The "
            "source built and packaged successfully, but live reading requires a valid User ID "
            "and Token in the generated source settings before validation can continue."
        )
    return (
        "`blocked` means this CLI/test-runner network environment could not complete live "
        "validation. It does not mean the source site is globally unavailable or unusable in a "
        "normal browser."
    )


def write_report(project: Path, report: ConversionReport) -> None:
    (project / "report.json").write_text(
        report.model_dump_json(indent=2, exclude_none=True) + "\n",
        encoding="utf-8",
    )
    normalization_rewrites: Counter[str] = Counter()
    contract_rules: Counter[str] = Counter()
    for round_result in report.ai_rounds:
        normalization_rewrites.update(round_result.normalization_rewrites)
        normalization_rewrites.update(round_result.projection_rewrites)
        contract_rules.update(round_result.contract_rule_ids)
    lines = [
        f"# Conversion report: {report.source_id}",
        "",
        f"- Status: **{report.status.value}**",
        f"- Input: `{report.input_ref}`",
        f"- Model: `{report.model or 'not used'}`",
        f"- AI rounds: {len(report.ai_rounds)}",
        f"- Normalizer rewrite hits: {sum(normalization_rewrites.values())}",
        f"- Contract rule triggers: {sum(contract_rules.values())}",
        f"- Source analysis rules: {len(report.source_analysis_rule_ids)}",
        f"- Preflight rules: {len(report.preflight_rule_ids)}",
        f"- Failed AI exchanges: {len(report.failed_ai_exchanges)}",
        "",
    ]
    if report.template_matches:
        lines.extend(["## Templates", ""])
        for match in report.template_matches:
            state = "ready" if match.ready else "missing capabilities"
            detail = f"{state}, score {match.score:.2f}, aidoku-rs {match.aidoku_revision}"
            if match.missing_capabilities:
                detail += "; missing: " + ", ".join(
                    capability.value for capability in match.missing_capabilities
                )
            lines.append(f"- `{match.template_id}` ({detail})")
    if report.status is ConversionStatus.BLOCKED:
        lines.extend(
            [
                "## Status context",
                "",
                _blocked_status_context(report),
                "",
            ]
        )
    if normalization_rewrites:
        lines.extend(["", "## Normalizer rewrites", ""])
        lines.extend(
            f"- `{rule_id}`: {count} generated file(s) changed"
            for rule_id, count in sorted(
                normalization_rewrites.items(),
                key=lambda item: (-item[1], item[0]),
            )
        )
    if contract_rules:
        lines.extend(["", "## Contract rule triggers", ""])
        lines.extend(
            f"- `{rule_id}`: {count} round(s)"
            for rule_id, count in sorted(
                contract_rules.items(),
                key=lambda item: (-item[1], item[0]),
            )
        )
    if report.source_analysis_rule_ids:
        lines.extend(["", "## Source analysis rules", ""])
        lines.extend(f"- `{rule_id}`" for rule_id in report.source_analysis_rule_ids)
    if report.preflight_rule_ids:
        lines.extend(["", "## Preflight rules", ""])
        lines.extend(f"- `{rule_id}`" for rule_id in report.preflight_rule_ids)
    lines.extend(["", "## Validation", ""])
    for stage in report.validation.stages:
        marker = "PASS" if stage.ok else "SKIP" if stage.skipped else "FAIL"
        lines.append(f"- `{marker}` {stage.name} ({stage.duration_seconds:.2f}s)")
        if not stage.ok and stage.output:
            diagnostic = stage.output[-4_000:]
            lines.extend(["", "  ```text"])
            lines.extend(f"  {line}" for line in diagnostic.splitlines())
            lines.append("  ```")
    if report.generated_files:
        lines.extend(["", "## Output files", ""])
        lines.extend(f"- `{path}`" for path in report.generated_files)
    if report.warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in report.warnings)
    if report.unsupported_features:
        lines.extend(["", "## Unsupported features", ""])
        lines.extend(f"- {item}" for item in report.unsupported_features)
    lines.extend(
        [
            "",
            "The generated code may be a derivative work. Verify licensing and redistribution "
            "rights.",
            "",
        ]
    )
    (project / "report.md").write_text("\n".join(lines), encoding="utf-8")
