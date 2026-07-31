import ast
import re
from pathlib import Path

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
