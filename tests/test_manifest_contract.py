from pathlib import Path

from convert2aidoku.analyzer import analyze_source
from convert2aidoku.ingest import resolve_source
from convert2aidoku.manifest_contract import (
    ContractDiagnostic,
    ContractEvaluation,
    evaluate_manifest_contract,
)
from convert2aidoku.models import (
    Capability,
    DependencyRequest,
    GeneratedFile,
    GenerationManifest,
)

FIXTURE = Path(__file__).parent / "fixtures" / "simple"

DECOMPILED_RETRY_MESSAGE = (
    "decompiled Tachi JSON source generated no centralized one-retry helper for transient "
    "idempotent GET RequestError; reconstruct and resend the same request once, then "
    "deserialize only the successful response"
)
KOTLIN_RETRY_MESSAGE = (
    "standard Kotlin HttpSource generated no centralized one-retry helper for transient "
    "idempotent GET RequestError; reconstruct and resend the same request once, then parse "
    "only the successful response"
)
CHAPTER_REGEX_MESSAGE = (
    "generated code compiles Regex::new on every chapter parse; for fixed embedded-JSON "
    "delimiters or numeric chapter labels, use bounded string scanning so each update does "
    "not compile a regex and pull regex runtime cost into the WASM hot path"
)


def _manifest(content: str) -> GenerationManifest:
    return GenerationManifest(
        source_struct="Simple",
        files=[GeneratedFile(path="src/lib.rs", content=content)],
    )


def test_targeted_diagnostics_keep_exact_user_messages_and_structured_kinds() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        kotlin_ir = analyze_source(resolved)
    decompiled_ir = kotlin_ir.model_copy(
        update={
            "source_format": "decompiled_apk",
            "capabilities": [Capability.JSON_API],
        }
    )
    decompiled = evaluate_manifest_contract(
        decompiled_ir,
        _manifest("fn unrelated() {}").model_copy(
            update={"dependencies": [DependencyRequest(name="serde")]}
        ),
    )
    kotlin = evaluate_manifest_contract(
        kotlin_ir,
        _manifest("fn fetch(url: String) { Request::get(url).send(); }"),
    )
    chapter = evaluate_manifest_contract(
        kotlin_ir,
        _manifest('fn parse_chapters(data: &str) { Regex::new("chapters"); }'),
    )

    assert decompiled.diagnostics == (ContractDiagnostic(DECOMPILED_RETRY_MESSAGE, "retry"),)
    assert ContractDiagnostic(KOTLIN_RETRY_MESSAGE, "retry") in kotlin.diagnostics
    assert ContractDiagnostic(CHAPTER_REGEX_MESSAGE, "chapter_regex") in chapter.diagnostics
    assert decompiled.messages == [DECOMPILED_RETRY_MESSAGE]


def test_mixed_diagnostics_cannot_request_a_targeted_repair(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "lib.rs").write_text(
        "fn fetch() { request().send(); }\n",
        encoding="utf-8",
    )
    evaluation = ContractEvaluation(
        (
            ContractDiagnostic("retry required", "retry"),
            ContractDiagnostic("another contract is incomplete"),
        )
    )

    assert not evaluation.is_fully_targeted_repair
    assert evaluation.repair(tmp_path) is None


def test_targeted_repair_selects_only_functions_for_declared_kinds(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "lib.rs").write_text(
        (
            "fn request(&self) { build_request(); }\n"
            "fn fetch(&self) { self.request().send(); }\n"
            'fn parse_chapters(&self) { Regex::new("chapters"); }\n'
            "fn unrelated(&self) { preserve_everything_here(); }\n"
        ),
        encoding="utf-8",
    )
    evaluation = ContractEvaluation(
        (
            ContractDiagnostic("retry required", "retry"),
            ContractDiagnostic("chapter scan required", "chapter_regex"),
        )
    )

    repair = evaluation.repair(tmp_path)

    assert repair is not None
    assert [item["start_line"] for item in repair.excerpts] == [2, 3]
    combined = "\n".join(str(item["content"]) for item in repair.excerpts)
    assert "self.request().send()" in combined
    assert "Regex::new" in combined
    assert "preserve_everything_here" not in combined
    assert repair.diagnostics == "retry required\nchapter scan required"
