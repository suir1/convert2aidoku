import ast
import re
from pathlib import Path

from convert2aidoku.manifest_contract import CONTRACT_RULE_IDS
from convert2aidoku.models import Capability, ValidationBlocker
from convert2aidoku.scaffold import MANIFEST_PROJECTION_RULE_IDS
from convert2aidoku.source_rules import (
    CAPABILITY_RULE_IDS,
    PREFLIGHT_RULE_IDS,
    SOURCE_ANALYSIS_RULE_IDS,
    SOURCE_BLOCK_RULE_IDS,
    capability_rule_id,
)

_SOURCE_ID = re.compile(r"^[a-z]{2}\.[a-z][a-z0-9_-]+$")


def test_conversion_package_does_not_dispatch_on_literal_source_ids() -> None:
    """Source-specific benchmark knowledge must not leak into the conversion core."""
    package = Path(__file__).parents[1] / "src" / "convert2aidoku"
    violations: list[str] = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _SOURCE_ID.fullmatch(node.value)
            ):
                violations.append(f"{path.name}:{node.lineno}: {node.value}")

    assert violations == []


def _literal_rule_ids(path: Path, function_name: str, *, prefix: str = "") -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        prefix + node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == function_name
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }


def _literal_prefixed_strings(paths: list[Path], prefixes: tuple[str, ...]) -> set[str]:
    values: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        values.update(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith(prefixes)
        )
    return values


def _referenced_enum_members(paths: list[Path], enum_name: str) -> set[str]:
    members: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        members.update(
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == enum_name
        )
    return members


def test_manifest_rule_catalogs_match_every_call_site() -> None:
    package = Path(__file__).parents[1] / "src" / "convert2aidoku"

    contract_ids = _literal_rule_ids(package / "manifest_contract.py", "add")
    projection_ids = _literal_rule_ids(package / "scaffold.py", "projected", prefix="project_")

    assert contract_ids == CONTRACT_RULE_IDS
    assert projection_ids == MANIFEST_PROJECTION_RULE_IDS


def test_source_rule_catalogs_match_capabilities_and_preflight_call_sites() -> None:
    package = Path(__file__).parents[1] / "src" / "convert2aidoku"
    preflight_ids = _literal_rule_ids(package / "conversion_assessment.py", "record")
    analysis_paths = [package / "kotlin_analysis.py", package / "decompiled_analysis.py"]
    blocker_paths = [*analysis_paths, package / "decompiled_input.py"]
    extra_analysis_ids = _literal_prefixed_strings(
        analysis_paths,
        ("exclude_", "relative_", "warn_"),
    )
    blocker_ids = _literal_prefixed_strings(blocker_paths, ("unsupported_",))

    assert {capability_rule_id(item) for item in Capability} == CAPABILITY_RULE_IDS
    assert CAPABILITY_RULE_IDS | extra_analysis_ids == SOURCE_ANALYSIS_RULE_IDS
    assert blocker_ids == SOURCE_BLOCK_RULE_IDS
    assert preflight_ids == PREFLIGHT_RULE_IDS


def test_validation_blocker_catalog_matches_policy_call_sites() -> None:
    package = Path(__file__).parents[1] / "src" / "convert2aidoku"
    policy_members = _referenced_enum_members(
        [package / "validation_policy.py", package / "validator.py"],
        "ValidationBlocker",
    )

    assert policy_members == {blocker.name for blocker in ValidationBlocker}
