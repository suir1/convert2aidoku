from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import SecretStr

from convert2aidoku.analyzer import analyze_source
from convert2aidoku.config import AISettings
from convert2aidoku.generated_source_metadata import GeneratedSourceMetadata
from convert2aidoku.ingest import resolve_source
from convert2aidoku.models import SourceFile, SourceIR, SourceMetadata
from convert2aidoku.scaffold import create_scaffold

SIMPLE_FIXTURE = Path(__file__).parent / "fixtures" / "simple"


def provider_settings(**overrides: object) -> AISettings:
    values: dict[str, object] = {
        "base_url": "http://local/v1",
        "model": "test",
        "api_key": SecretStr("secret"),
    }
    values.update(overrides)
    return AISettings.model_validate(values)


def conversion_settings(**overrides: object) -> AISettings:
    values = {"base_url": "http://localhost/v1", "model": "fake", **overrides}
    return provider_settings(**values)


def minimal_source_ir(
    *,
    files: list[SourceFile] | None = None,
    source_id: str = "en.example",
    language: str = "en",
    **overrides: object,
) -> SourceIR:
    ir = SourceIR(
        input_ref="fixture",
        metadata=SourceMetadata(
            source_id=source_id,
            package_name="example",
            name="Example",
            language=language,
            base_url="https://example.com",
        ),
        main_class="Example",
        files=list(files or []),
    )
    if not overrides:
        return ir
    return SourceIR.model_validate({**ir.model_dump(mode="python"), **overrides})


def scaffold_project(
    parent: Path,
    *,
    fixture: Path = SIMPLE_FIXTURE,
    name: str = "project",
    transform: Callable[[SourceIR], SourceIR] | None = None,
) -> tuple[Path, SourceIR]:
    with resolve_source(str(fixture)) as resolved:
        ir = analyze_source(resolved)
        if transform is not None:
            ir = transform(ir)
        project = parent / name
        create_scaffold(project, ir, resolved)
    return project, ir


def source_metadata_project(parent: Path, *, url: str = "https://example.com") -> Path:
    source_ir = minimal_source_ir()
    source_ir = source_ir.model_copy(
        update={
            "metadata": source_ir.metadata.model_copy(update={"base_url": url}),
        }
    )
    GeneratedSourceMetadata.from_source_ir(source_ir).write(parent)
    return parent
