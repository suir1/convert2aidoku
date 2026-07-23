from convert2aidoku.models import SourceFile
from tests.scenarios import minimal_source_ir


def test_scenarios_return_fresh_defaults() -> None:
    first_ir = minimal_source_ir()
    second_ir = minimal_source_ir()
    first_ir.files.append(SourceFile(path="src/One.kt", content="class One", sha256="0"))

    assert second_ir.files == []
