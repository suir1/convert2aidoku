from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from convert2aidoku.ai import AIResult
from convert2aidoku.analyzer import analyze_source
from convert2aidoku.config import AISettings
from convert2aidoku.generated_source_metadata import GeneratedSourceMetadata
from convert2aidoku.ingest import resolve_source
from convert2aidoku.models import (
    DependencyRequest,
    GeneratedFile,
    GenerationManifest,
    OptionalTrait,
    RepairPatch,
    SourceFile,
    SourceIR,
    SourceMetadata,
)
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


def generation_manifest(
    rust: str,
    *,
    source_struct: str = "Simple",
    traits: Iterable[OptionalTrait] = (),
    resources: Mapping[str, str] | None = None,
    dependencies: Iterable[DependencyRequest] = (),
) -> GenerationManifest:
    files = [GeneratedFile(path="src/lib.rs", content=rust)]
    files.extend(
        GeneratedFile(path=path, content=content) for path, content in (resources or {}).items()
    )
    return GenerationManifest(
        source_struct=source_struct,
        implemented_traits=list(traits),
        files=files,
        dependencies=list(dependencies),
    )


@dataclass
class ScriptedAICalls:
    generate: int = 0
    repair: int = 0
    repair_patch: int = 0


def scripted_ai_client(
    *,
    generation: GenerationManifest,
    repair: GenerationManifest | None = None,
    repair_patch: RepairPatch | BaseException | None = None,
    patch_scope: str | None = None,
    patch_diagnostic: str | None = None,
) -> tuple[type[object], ScriptedAICalls]:
    """Create one isolated AI Adapter class and its call facts for a Test Scenario."""
    calls = ScriptedAICalls()

    class Adapter:
        def __init__(self, settings: AISettings):
            self.settings = settings

        def __enter__(self) -> Adapter:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def generate(self, _source_ir: object) -> AIResult[GenerationManifest]:
            calls.generate += 1
            return AIResult(value=generation, structured_output=True)

        def repair(self, _source_ir: object, **_request: object) -> AIResult:
            calls.repair += 1
            value = generation if repair is None else repair
            return AIResult(value=value, structured_output=True)

        def repair_patch(self, _source_ir: object, **request: object) -> AIResult:
            calls.repair_patch += 1
            if repair_patch is None:
                raise AssertionError("Test Scenario did not configure repair_patch")
            assert request["current_file_excerpts"]
            diagnostics = str(request["diagnostics"])
            assert diagnostics
            if patch_scope is not None:
                assert request.get("scope", "compiler") == patch_scope
            if patch_diagnostic is not None:
                assert patch_diagnostic in diagnostics
            if isinstance(repair_patch, BaseException):
                raise repair_patch
            return AIResult(value=repair_patch, structured_output=True)

    return Adapter, calls


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
