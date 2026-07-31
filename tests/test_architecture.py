import ast
import re
from pathlib import Path

from convert2aidoku.manifest_contract import CONTRACT_RULE_IDS
from convert2aidoku.scaffold import MANIFEST_PROJECTION_RULE_IDS

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


def test_manifest_rule_catalogs_match_every_call_site() -> None:
    package = Path(__file__).parents[1] / "src" / "convert2aidoku"

    contract_ids = _literal_rule_ids(package / "manifest_contract.py", "add")
    projection_ids = _literal_rule_ids(package / "scaffold.py", "projected", prefix="project_")

    assert contract_ids == CONTRACT_RULE_IDS
    assert projection_ids == MANIFEST_PROJECTION_RULE_IDS
