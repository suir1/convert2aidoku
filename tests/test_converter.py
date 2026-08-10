import json
from pathlib import Path

import pytest

from convert2aidoku.analyzer import analyze_source
from convert2aidoku.converter import (
    convert_source,
    validate_existing,
)
from convert2aidoku.errors import AIProviderError
from convert2aidoku.ingest import resolve_source
from convert2aidoku.manifest_contract import (
    ContractDiagnostic,
    ContractEvaluation,
    evaluate_manifest_contract,
)
from convert2aidoku.models import (
    AIFailedExchange,
    AIUsage,
    Capability,
    ChapterPageRoute,
    ChapterPageRouteVariant,
    ConversionCheckpoint,
    ConversionStatus,
    DependencyRequest,
    GeneratedFile,
    GeneratedResources,
    GenerationManifest,
    ImageUrlPolicy,
    RepairPatch,
    RouteReplacement,
    SourceFile,
    SourceFilterOption,
    SourceFilterSpec,
    ValidationResult,
    ValidationStage,
)
from tests.scenarios import (
    ScriptedAICalls,
    conversion_settings,
    generation_manifest,
    minimal_source_ir,
    scaffold_project,
    scripted_ai_client,
)

FIXTURE = Path(__file__).parent / "fixtures" / "simple"
ENCRYPTED_API_FIXTURE = Path(__file__).parent / "fixtures" / "encrypted_api"


def _contract_messages(ir, manifest) -> list[str]:
    return evaluate_manifest_contract(ir, manifest).messages


RUST_SOURCE = """#![no_std]
use aidoku::{
    Chapter, FilterValue, Manga, MangaPageResult, Page, Result, Source,
    alloc::{String, Vec}, register_source,
};

struct Simple;

impl Source for Simple {
    fn new() -> Self { Self }

    fn get_search_manga_list(
        &self,
        _query: Option<String>,
        _page: i32,
        _filters: Vec<FilterValue>,
    ) -> Result<MangaPageResult> {
        Ok(MangaPageResult::default())
    }

    fn get_manga_update(
        &self,
        manga: Manga,
        _needs_details: bool,
        _needs_chapters: bool,
    ) -> Result<Manga> {
        Ok(manga)
    }

    fn get_page_list(&self, _manga: Manga, _chapter: Chapter) -> Result<Vec<Page>> {
        Ok(Vec::new())
    }
}

register_source!(Simple);
"""


def _baseline_generation() -> GenerationManifest:
    return generation_manifest(
        RUST_SOURCE,
        traits=("ListingProvider", "ImageRequestProvider"),
        resources={"res/filters.json": "[]", "res/settings.json": "[]"},
    )


def _install_ai_scenario(
    monkeypatch: pytest.MonkeyPatch,
    *,
    generation: GenerationManifest | None = None,
    repair: GenerationManifest | None = None,
    repair_patch: RepairPatch | BaseException | None = None,
    patch_scope: str | None = None,
    patch_diagnostic: str | None = None,
) -> ScriptedAICalls:
    adapter, calls = scripted_ai_client(
        generation=generation or _baseline_generation(),
        repair=repair,
        repair_patch=repair_patch,
        patch_scope=patch_scope,
        patch_diagnostic=patch_diagnostic,
    )
    monkeypatch.setattr("convert2aidoku.converter.OpenAICompatibleClient", adapter)
    return calls


def test_conversion_orchestrates_atomic_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_ai_scenario(monkeypatch)
    monkeypatch.setattr(
        "convert2aidoku.converter.validate_project",
        lambda *_args, **_kwargs: ValidationResult(build_ok=True, package_ok=True, live_ok=True),
    )
    settings = conversion_settings()
    output = tmp_path / "generated" / "en.simple"

    outcome = convert_source(str(FIXTURE), output=output, settings=settings, live=True)

    assert outcome.report.status is ConversionStatus.VERIFIED
    assert (output / "src" / "lib.rs").is_file()
    assert (output / "report.json").is_file()
    assert outcome.report.template_matches
    assert "html-http-source" in (output / "report.md").read_text(encoding="utf-8")
    assert "secret" not in (output / "report.json").read_text()
    assert (output / ".c2a" / "manifests" / "round-01.json").is_file()
    assert not list(output.parent.glob(f".{output.name}-*"))


def test_conversion_reports_generation_and_validation_progress(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_ai_scenario(monkeypatch)
    monkeypatch.setattr(
        "convert2aidoku.converter.validate_project",
        lambda *_args, **_kwargs: ValidationResult(
            build_ok=True,
            package_ok=True,
            live_ok=True,
        ),
    )
    progress: list[str] = []

    convert_source(
        str(FIXTURE),
        output=tmp_path / "generated" / "en.simple",
        settings=conversion_settings(),
        live=True,
        progress=progress.append,
    )

    assert progress[0] == "Preparing initial generation"
    assert any("AI round 1 returned" in message for message in progress)
    assert any("round 1 validation passed" in message for message in progress)


def test_repair_stops_after_two_attempts_with_the_same_validation_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ai_calls = _install_ai_scenario(monkeypatch, repair=_baseline_generation())
    monkeypatch.setattr(
        "convert2aidoku.converter.validate_project",
        lambda *_args, **_kwargs: ValidationResult(
            stages=[
                ValidationStage(
                    name="core-live-smoke",
                    kind="live_test",
                    ok=False,
                    output="same live failure",
                )
            ],
            build_ok=True,
            package_ok=True,
        ),
    )
    progress: list[str] = []

    outcome = convert_source(
        str(FIXTURE),
        output=tmp_path / "generated" / "en.simple",
        settings=conversion_settings(max_repair_rounds=8),
        live=True,
        progress=progress.append,
    )

    assert outcome.report.status is ConversionStatus.FAILED
    assert ai_calls.repair == 2
    assert len(outcome.report.ai_rounds) == 3
    assert [round_.repair_mode for round_ in outcome.report.ai_rounds] == [
        None,
        "full",
        "full",
    ]
    assert any("unchanged validation state" in warning for warning in outcome.report.warnings)
    assert any("Repair stopped" in message for message in progress)


def test_compiler_failure_uses_at_most_two_repairs_even_when_configured_higher(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ai_calls = _install_ai_scenario(monkeypatch, repair=_baseline_generation())
    monkeypatch.setattr(
        "convert2aidoku.converter.validate_project",
        lambda *_args, **_kwargs: ValidationResult(
            stages=[
                ValidationStage(
                    name="cargo-check",
                    kind="check",
                    ok=False,
                    output="error: synthetic compiler failure",
                )
            ]
        ),
    )

    outcome = convert_source(
        str(FIXTURE),
        output=tmp_path / "generated" / "en.simple",
        settings=conversion_settings(max_repair_rounds=8),
        live=True,
    )

    assert outcome.report.status is ConversionStatus.FAILED
    assert ai_calls.repair == 2
    assert len(outcome.report.ai_rounds) == 3


def test_blocked_live_validation_skips_ai_repair_and_preserves_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ai_calls = _install_ai_scenario(monkeypatch, repair=_baseline_generation())
    monkeypatch.setattr(
        "convert2aidoku.converter.validate_project",
        lambda *_args, **_kwargs: ValidationResult(
            build_ok=True,
            package_ok=True,
            blocked=True,
            stages=[
                ValidationStage(
                    name="core-live-smoke",
                    kind="live_test",
                    ok=False,
                    blocked=True,
                    output="runner-network probe returned HTTP 403",
                )
            ],
        ),
    )
    progress: list[str] = []

    outcome = convert_source(
        str(FIXTURE),
        output=tmp_path / "generated" / "en.simple",
        settings=conversion_settings(max_repair_rounds=8),
        live=True,
        progress=progress.append,
    )

    assert outcome.report.status is ConversionStatus.BLOCKED
    assert ai_calls.repair == 0
    assert any("AI repair skipped" in item for item in progress)
    assert any("resume the saved checkpoint" in item for item in outcome.report.warnings)


def test_interrupted_conversion_resumes_saved_manifest_without_regeneration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ai_calls = _install_ai_scenario(monkeypatch)
    validation_calls = 0

    def interrupted_validation(*_args, **_kwargs) -> ValidationResult:
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 1:
            raise RuntimeError("synthetic validator interruption")
        return ValidationResult(build_ok=True, package_ok=True, live_ok=True)

    monkeypatch.setattr(
        "convert2aidoku.converter.validate_project",
        interrupted_validation,
    )
    settings = conversion_settings()
    output = tmp_path / "generated" / "en.simple"

    with pytest.raises(RuntimeError, match="validator interruption"):
        convert_source(str(FIXTURE), output=output, settings=settings, live=True)

    workspace = output.parent / f".{output.name}.c2a-work"
    assert (workspace / "manifests" / "round-01.json").is_file()
    assert "secret" not in (workspace / "checkpoint.json").read_text(encoding="utf-8")

    outcome = convert_source(
        str(FIXTURE),
        output=output,
        settings=settings,
        live=True,
        resume=True,
    )

    assert outcome.report.status is ConversionStatus.VERIFIED
    assert ai_calls.generate == 1
    assert (output / ".c2a" / "manifests" / "round-01.json").is_file()
    assert not workspace.exists()


def test_failed_initial_exchange_usage_and_diagnostic_are_checkpointed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    usage = AIUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30)

    class FailingAIClient:
        def __init__(self, _settings) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def generate(self, _source_ir):
            raise AIProviderError(
                "synthetic invalid manifest",
                usage=usage,
                warnings=["first invalid output", "second invalid output"],
            )

    monkeypatch.setattr("convert2aidoku.converter.OpenAICompatibleClient", FailingAIClient)
    output = tmp_path / "generated" / "en.simple"

    with pytest.raises(AIProviderError, match="synthetic invalid manifest"):
        convert_source(
            str(FIXTURE),
            output=output,
            settings=conversion_settings(),
            live=False,
        )

    checkpoint = ConversionCheckpoint.model_validate_json(
        (output.parent / f".{output.name}.c2a-work" / "checkpoint.json").read_text()
    )
    assert checkpoint.failed_ai_exchanges == [
        AIFailedExchange(
            purpose="generate",
            usage=usage,
            diagnostics=["first invalid output", "second invalid output"],
        )
    ]


def test_repair_preserves_source_ir_required_resources(tmp_path: Path, monkeypatch) -> None:
    original_analyze = analyze_source

    def analyze_with_required_resources(resolved):
        ir = original_analyze(resolved)
        return ir.model_copy(
            update={
                "capabilities": [
                    Capability.SEARCH,
                    Capability.DETAILS,
                    Capability.CHAPTERS,
                    Capability.PAGES,
                    Capability.FILTERS,
                    Capability.SETTINGS,
                ]
            }
        )

    monkeypatch.setattr(
        "convert2aidoku.conversion_intake.analyze_source",
        analyze_with_required_resources,
    )

    _install_ai_scenario(
        monkeypatch,
        generation=generation_manifest(
            RUST_SOURCE,
            traits=("ListingProvider", "ImageRequestProvider"),
            resources={
                "res/filters.json": (
                    '[{"type":"select","id":"filter","options":["All"],"ids":["all"]}]'
                ),
                "res/settings.json": (
                    '[{"type":"group","title":"Settings","items":'
                    '[{"type":"text","key":"example","title":"Example"}]}]'
                ),
            },
        ),
        repair=generation_manifest(
            RUST_SOURCE + '\nfn setting() { defaults_get::<String>("example"); }\n'
        ),
    )
    validations = iter(
        [
            ValidationResult(),
            ValidationResult(build_ok=True, package_ok=True, live_ok=True),
        ]
    )
    monkeypatch.setattr(
        "convert2aidoku.converter.validate_project",
        lambda *_args, **_kwargs: next(validations),
    )
    settings = conversion_settings(max_repair_rounds=1)
    output = tmp_path / "generated" / "en.simple"

    outcome = convert_source(str(FIXTURE), output=output, settings=settings, live=True)

    assert outcome.report.status is ConversionStatus.VERIFIED
    assert (output / "res" / "filters.json").is_file()
    assert (output / "res" / "settings.json").is_file()
    raw_repair = GenerationManifest.model_validate_json(
        (output / ".c2a" / "manifests" / "round-02.json").read_text(encoding="utf-8")
    )
    assert [item.path for item in raw_repair.files] == ["src/lib.rs"]
    assert any("preserved from the prior round" in item for item in outcome.report.warnings)


def test_targeted_patch_failure_falls_back_to_full_controlled_repair(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ai_calls = _install_ai_scenario(
        monkeypatch,
        repair_patch=AIProviderError("synthetic invalid patch"),
        patch_scope="compiler",
    )
    validation = ValidationResult(
        stages=[
            ValidationStage(
                name="cargo-check",
                kind="check",
                ok=False,
                output="error\n  --> src/lib.rs:1:1",
            )
        ]
    )
    monkeypatch.setattr(
        "convert2aidoku.converter.validate_project",
        lambda *_args, **_kwargs: validation,
    )
    settings = conversion_settings(max_repair_rounds=1)

    outcome = convert_source(
        str(FIXTURE),
        output=tmp_path / "generated" / "en.simple",
        settings=settings,
        live=True,
    )

    assert outcome.report.status is ConversionStatus.FAILED
    assert ai_calls.repair_patch == 1
    assert ai_calls.repair == 1
    assert len(outcome.report.ai_rounds) == 2
    assert len(outcome.report.failed_ai_exchanges) == 0
    assert any("used a full controlled repair" in warning for warning in outcome.report.warnings)
    checkpoint = ConversionCheckpoint.model_validate_json(
        (outcome.output / ".c2a" / "checkpoint.json").read_text(encoding="utf-8")
    )
    assert checkpoint.phase == "validated"
    assert checkpoint.current_manifest == "manifests/round-02.json"


def test_contract_patch_failure_falls_back_to_full_controlled_repair(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ai_calls = _install_ai_scenario(
        monkeypatch,
        generation=generation_manifest(
            RUST_SOURCE
            + "\nfn fetch(url: String) -> Result<Response> {\n"
            + "Request::get(url)?.send()\n}\n",
            traits=("ListingProvider", "ImageRequestProvider"),
            resources={"res/filters.json": "[]", "res/settings.json": "[]"},
        ),
        repair=_baseline_generation(),
        repair_patch=AIProviderError("synthetic invalid contract patch"),
        patch_scope="contract",
        patch_diagnostic="standard Kotlin HttpSource",
    )
    monkeypatch.setattr(
        "convert2aidoku.converter.validate_project",
        lambda *_args, **_kwargs: ValidationResult(
            build_ok=True,
            package_ok=True,
            live_ok=True,
        ),
    )
    settings = conversion_settings(max_repair_rounds=1)

    outcome = convert_source(
        str(FIXTURE),
        output=tmp_path / "generated" / "en.simple",
        settings=settings,
        live=True,
    )

    assert outcome.report.status is ConversionStatus.VERIFIED
    assert ai_calls.repair_patch == 1
    assert ai_calls.repair == 1
    assert [round_.purpose for round_ in outcome.report.ai_rounds] == ["generate", "repair"]
    assert len(outcome.report.failed_ai_exchanges) == 0
    assert any("used a full controlled repair" in warning for warning in outcome.report.warnings)


def test_forced_failed_conversion_preserves_existing_output(tmp_path: Path, monkeypatch) -> None:
    _install_ai_scenario(monkeypatch)
    monkeypatch.setattr(
        "convert2aidoku.converter.validate_project",
        lambda *_args, **_kwargs: ValidationResult(),
    )
    settings = conversion_settings(max_repair_rounds=0)
    output = tmp_path / "generated" / "en.simple"
    output.mkdir(parents=True)
    (output / "keep.txt").write_text("old", encoding="utf-8")

    outcome = convert_source(str(FIXTURE), output=output, settings=settings, live=False, force=True)

    assert (output / "keep.txt").read_text(encoding="utf-8") == "old"
    assert outcome.output != output
    assert (outcome.output / "report.json").is_file()


def test_contract_incomplete_build_stays_in_resumable_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_ai_scenario(monkeypatch)
    monkeypatch.setattr(
        "convert2aidoku.converter.validate_project",
        lambda *_args, **_kwargs: ValidationResult(
            build_ok=True,
            package_ok=True,
            live_ok=True,
        ),
    )
    monkeypatch.setattr(
        "convert2aidoku.converter.evaluate_manifest_contract",
        lambda *_args, **_kwargs: ContractEvaluation(
            (ContractDiagnostic("synthetic missing capability"),)
        ),
    )
    settings = conversion_settings(max_repair_rounds=0)
    output = tmp_path / "generated" / "en.simple"

    outcome = convert_source(str(FIXTURE), output=output, settings=settings, live=True)

    workspace = output.parent / f".{output.name}.c2a-work"
    assert outcome.report.status is ConversionStatus.BUILD_ONLY
    assert outcome.output == workspace / "project"
    assert workspace.is_dir()
    assert not output.exists()
    assert "synthetic missing capability" in (workspace / "checkpoint.json").read_text(
        encoding="utf-8"
    )


def test_resolved_contract_gaps_are_not_reported_as_final_warnings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_ai_scenario(
        monkeypatch,
        generation=generation_manifest(RUST_SOURCE),
        repair=_baseline_generation(),
    )
    monkeypatch.setattr(
        "convert2aidoku.converter.validate_project",
        lambda *_args, **_kwargs: ValidationResult(
            build_ok=True,
            package_ok=True,
            live_ok=True,
        ),
    )
    settings = conversion_settings()

    outcome = convert_source(
        str(FIXTURE),
        output=tmp_path / "generated" / "en.simple",
        settings=settings,
        live=True,
    )

    assert len(outcome.report.ai_rounds) == 2
    assert not any("generated no" in warning for warning in outcome.report.warnings)


def test_validate_preserves_conversion_audit_fields(tmp_path: Path, monkeypatch) -> None:
    project, _ir = scaffold_project(tmp_path, fixture=FIXTURE)
    (project / "report.json").write_text(
        '{"status":"verified","input_ref":"source","source_id":"en.simple",'
        '"provider_base_url":"http://local/v1","model":"fake",'
        '"ai_rounds":[{"round":1,"purpose":"generate","structured_output":true}],'
        '"generated_files":["src/lib.rs"],"validation":{"stages":[],'
        '"build_ok":true,"package_ok":true,"live_ok":true}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "convert2aidoku.converter.validate_project",
        lambda *_args, **_kwargs: ValidationResult(build_ok=True, package_ok=True),
    )

    report = validate_existing(project, live=False)

    assert report.model == "fake"
    assert report.generated_files == ["src/lib.rs"]


def test_relative_key_requests_are_contract_gaps() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(update={"relative_url_keys": True})
    manifest = generation_manifest(
        "fn request(url: String) {}\nfn update(manga: Manga) { self.request(manga.key.clone()); }"
    )

    gaps = _contract_messages(ir, manifest)

    assert any("absolute_url helper" in gap for gap in gaps)
    assert any("passes Manga.key" in gap for gap in gaps)


def test_chapter_page_route_replacement_is_a_contract_gap() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={
                "chapter_page_routes": [
                    ChapterPageRoute(
                        source_method="content(fix(chapter.url))",
                        chapter_key_template="/comic/{comic_path}/chapter/{chapter_id}",
                        endpoint_template="/api/v3/comic/{normalized_chapter_key}",
                        variants=[
                            ChapterPageRouteVariant(
                                name="default",
                                condition="copy API domain",
                                is_default=True,
                                strip_prefix="/comic/",
                                replacements=[RouteReplacement(old="/chapter/", new="/chapter2/")],
                            )
                        ],
                    )
                ]
            }
        )
    manifest = generation_manifest(
        'fn page(key: &str) { let _ = key.trim_start_matches("/comic/"); }'
    )

    gaps = _contract_messages(ir, manifest)

    assert any("'/chapter/' -> '/chapter2/'" in gap for gap in gaps)


def test_cover_urls_and_chapter_resolution_scope_are_contract_gaps() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={
                "image_url_policy": ImageUrlPolicy(
                    preserve_cover_urls=True,
                    chapter_resolution_regex=r"\d+(?=x\.(?:jpg|webp)$)",
                )
            }
        )
    manifest = generation_manifest(
        "fn image_resolution(url: String) -> String { url }\n"
        "fn comic_to_manga(comic: Comic) -> Manga { Manga { "
        "cover: Some(self.image_resolution(comic.cover)), "
        "..Default::default() } }"
    )

    gaps = _contract_messages(ir, manifest)

    assert any("cover URL" in gap for gap in gaps)
    assert any("chapter image resolution" in gap for gap in gaps)


def test_recovered_filter_contract_rejects_wrong_types_values_and_defaults() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={
                "capabilities": [Capability.FILTERS],
                "filter_specs": [
                    SourceFilterSpec(
                        source_class="AudienceFilter",
                        id="audience",
                        title="Audience",
                        kind="select",
                        options=[
                            SourceFilterOption(title="Male", value="male"),
                            SourceFilterOption(title="Female", value="female"),
                        ],
                        default_index=0,
                    ),
                    SourceFilterSpec(
                        source_class="SortFilter",
                        id="sort",
                        title="Sort",
                        kind="sort",
                        options=[
                            SourceFilterOption(title="Popular", value="popular"),
                            SourceFilterOption(title="Updated", value="datetime_updated"),
                        ],
                        default_index=1,
                        default_ascending=True,
                    ),
                ],
            }
        )
    manifest = generation_manifest(
        "fn filter(filters: Vec<FilterValue>) { "
        "if let FilterValue::Select { .. } = filters[0] {} }",
        resources={
            "res/filters.json": (
                '[{"type":"select","id":"audience","title":"Audience",'
                '"options":["Male","Female"],"ids":["male_default","female"]},'
                '{"type":"select","id":"sort","title":"Sort",'
                '"options":["Popular","Updated"],'
                '"ids":["popular","datetime_updated"]}]'
            )
        },
    )

    gaps = _contract_messages(ir, manifest)

    assert any("audience" in gap and "site values" in gap for gap in gaps)
    assert any("audience" in gap and "default" in gap for gap in gaps)
    assert any("sort" in gap and "type" in gap for gap in gaps)
    assert any("FilterValue::Sort" in gap for gap in gaps)


def test_detail_api_response_envelope_is_a_contract_gap() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={
                "files": [
                    SourceFile(
                        path="ComicDetailResult.java",
                        sha256="0",
                        content=(
                            "Reflection.typeOf(ApiResponse.class, "
                            "KTypeProjection.Companion.invariant("
                            "Reflection.typeOf(ComicDetailResult.class)))"
                        ),
                    )
                ]
            }
        )
    manifest = generation_manifest(
        "fn get_json<T>(&self, key: &str) -> Result<T> { "
        "self.request(key)?.send()?.get_json_owned() }\n"
        "fn detail(&self, key: &str) -> Result<DetailResult> { self.get_json(key) }"
    )

    gaps = _contract_messages(ir, manifest)

    assert any("ApiResponse<ComicDetailResult>" in gap for gap in gaps)
    assert any("response.results" in gap for gap in gaps)


def test_rank_item_comic_wrapper_is_a_contract_gap() -> None:
    ir = minimal_source_ir(
        files=[
            SourceFile(
                path="RankResult.java",
                sha256="0",
                content="class RankResult { private final List<ListItem> list; }",
            ),
            SourceFile(
                path="ListItem.java",
                sha256="0",
                content="class ListItem { private final ComicSummary comic; }",
            ),
        ]
    )
    manifest = generation_manifest(
        """
fn get_search_manga_list(&self, url: String) -> Result<MangaPageResult> {
    let rank_path = "/api/v3/ranks?type=1";
    let response: ApiResponse<PageResult<Comic>> = self.json(url)?;
    Ok(MangaPageResult {
        entries: response.results.list.into_iter().map(Self::manga).collect(),
        has_next_page: false,
    })
}
"""
    )

    gaps = _contract_messages(ir, manifest)

    assert any("RankResult.list" in gap and "ListItem.comic" in gap for gap in gaps)


def test_recovered_filter_defaults_are_injected_deterministically() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={
                "filter_specs": [
                    SourceFilterSpec(
                        source_class="AudienceFilter",
                        id="audience",
                        title="Audience",
                        kind="select",
                        options=[
                            SourceFilterOption(title="Male", value="male"),
                            SourceFilterOption(title="Female", value="female"),
                        ],
                        default_index=0,
                    ),
                    SourceFilterSpec(
                        source_class="SortFilter",
                        id="sort",
                        title="Sort",
                        kind="sort",
                        options=[
                            SourceFilterOption(title="Popular", value="popular"),
                            SourceFilterOption(title="Updated", value="datetime_updated"),
                        ],
                        default_index=1,
                        default_ascending=True,
                    ),
                ]
            }
        )
    manifest = generation_manifest(
        RUST_SOURCE,
        resources={
            "res/filters.json": (
                '[{"type":"select","id":"audience","options":["Male","Female"],'
                '"ids":["male","female"]},'
                '{"type":"sort","id":"sort","options":["Popular","Updated"]}]'
            )
        },
    )

    effective = GeneratedResources(manifest).with_defaults(filter_specs=ir.filter_specs)
    filters_file = next(item for item in effective.files if item.path == "res/filters.json")
    filters = json.loads(filters_file.content)
    original_filters = json.loads(
        next(item.content for item in manifest.files if item.path == "res/filters.json")
    )

    assert all("default" not in item for item in original_filters)
    assert filters[0]["default"] == "male"
    assert filters[1]["default"] == {"index": 1, "ascending": True}
    assert filters[1]["canAscend"] is True


def test_empty_declared_resources_are_contract_gaps() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(update={"capabilities": ["filters", "settings"]})
    manifest = generation_manifest(
        RUST_SOURCE,
        resources={"res/filters.json": "[]", "res/settings.json": "[]"},
    )

    gaps = _contract_messages(ir, manifest)

    assert "source declares filters but generated an empty res/filters.json" in gaps
    assert "source declares settings but generated an empty res/settings.json" in gaps


def test_declared_dynamic_filters_and_deep_links_require_providers() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={"capabilities": [Capability.DYNAMIC_FILTERS, Capability.DEEP_LINKS]}
        )
    manifest = generation_manifest(RUST_SOURCE)

    gaps = _contract_messages(ir, manifest)

    assert "source fetches dynamic filters but generated no DynamicFilters provider" in gaps
    assert "source declares deep links but generated no DeepLinkHandler" in gaps


def test_complete_dynamic_filters_do_not_require_a_duplicate_static_resource() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={"capabilities": [Capability.FILTERS, Capability.DYNAMIC_FILTERS]}
        )
    manifest = generation_manifest(
        "fn get_dynamic_filters(&self) -> Result<Vec<Filter>> { Ok(Vec::new()) }",
        traits=("DynamicFilters",),
    )

    gaps = _contract_messages(ir, manifest)

    assert not [gap for gap in gaps if "res/filters.json" in gap]


def test_dynamic_filters_cannot_deserialize_aidoku_filter_from_json() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={"capabilities": [Capability.DYNAMIC_FILTERS]}
        )
    manifest = generation_manifest(
        'fn get_dynamic_filters(&self) -> Result<Vec<Filter>> { serde_json::from_str("[]") }',
        traits=("DynamicFilters",),
    )

    gaps = _contract_messages(ir, manifest)

    assert any("Filter is not Deserialize" in gap for gap in gaps)


def test_graphql_dynamic_filters_cannot_repeat_a_full_listing_request() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={"capabilities": [Capability.DYNAMIC_FILTERS, Capability.JSON_API]}
        )
    manifest = generation_manifest(
        'fn listing_query(&self) { let query = "query comics { comics { '
        'id title description } allCategory { id name } }"; }\n'
        "fn get_dynamic_filters(&self) { self.listing_query(); }\n"
        "fn get_search_manga_list(&self) { self.listing_query(); }\n",
        traits=("DynamicFilters",),
        dependencies=(DependencyRequest(name="serde"),),
    )

    gaps = _contract_messages(ir, manifest)

    assert any("full manga listing" in gap for gap in gaps)
    assert any("detail-only fields" in gap for gap in gaps)


def test_context_dependent_image_headers_require_page_context() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={"capabilities": [Capability.IMAGE_HEADERS]}
        )
    manifest = generation_manifest(
        "fn get_page_list(&self) { PageContent::url(image_url); }\n"
        "fn get_image_request(&self, context: Option<PageContext>) {\n"
        'if let Some(context) = context { context.get("referer"); }\n'
        "}\n",
        traits=("ImageRequestProvider",),
    )

    gaps = _contract_messages(ir, manifest)

    assert any("PageContent::url_context" in gap for gap in gaps)


def test_cookie_jar_input_requires_a_representable_cookie_session() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={
                "capabilities": [Capability.SETTINGS, Capability.IMAGE_HEADERS],
                "files": [
                    SourceFile(
                        path="src/Source.kt",
                        sha256="0",
                        content=(
                            "client.cookieJar.loadForRequest(url).find { it.name == "
                            '"komiic-access-token" }'
                        ),
                    )
                ],
            }
        )
    manifest = generation_manifest(
        "fn request() {}",
        traits=("ImageRequestProvider",),
        resources={"res/settings.json": '[{"type":"group","title":"Source","items":[]}]'},
    )

    gaps = _contract_messages(ir, manifest)

    assert any("Cookie session" in gap for gap in gaps)


def test_optional_cookie_refresh_does_not_block_anonymous_public_requests() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={
                "files": [
                    SourceFile(
                        path="src/Source.kt",
                        sha256="0",
                        content=(
                            "val cookie = client.cookieJar.loadForRequest(url)"
                            '.find { it.name == "access-token" } ?: return'
                        ),
                    )
                ],
            }
        )
    manifest = generation_manifest("fn request() {}")

    gaps = _contract_messages(ir, manifest)

    assert not [gap for gap in gaps if "Cookie session" in gap]


@pytest.mark.parametrize("missing_request", ["api", "image"])
def test_cookie_session_must_cover_api_and_image_requests(missing_request: str) -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={
                "capabilities": [Capability.SETTINGS, Capability.IMAGE_HEADERS],
                "files": [
                    SourceFile(
                        path="src/Source.kt",
                        sha256="0",
                        content="client.cookieJar.loadForRequest(url)",
                    )
                ],
            }
        )
    api_header = "" if missing_request == "api" else '.header("Cookie", cookie)'
    image_header = "" if missing_request == "image" else '.header("Cookie", cookie)'
    manifest = generation_manifest(
        f"fn post_query(&self) {{ Request::post(url){api_header}; }}\n"
        "fn get_image_request(&self) { "
        f"Request::get(url){image_header}; }}\n",
        traits=("ImageRequestProvider",),
        resources={
            "res/settings.json": (
                '[{"type":"group","title":"Source","items":'
                '[{"type":"text","key":"cookie","title":"Cookie"}]}]'
            )
        },
    )

    gaps = _contract_messages(ir, manifest)

    assert any("Cookie session" in gap for gap in gaps)


def test_optimized_graphql_manifest_has_no_performance_contract_gaps() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={
                "capabilities": [
                    Capability.DYNAMIC_FILTERS,
                    Capability.JSON_API,
                    Capability.SETTINGS,
                    Capability.IMAGE_HEADERS,
                    Capability.DETAILS,
                    Capability.CHAPTERS,
                ],
                "files": [
                    SourceFile(
                        path="src/Source.kt",
                        sha256="0",
                        content="client.cookieJar.loadForRequest(url)",
                    )
                ],
            }
        )
    manifest = generation_manifest(
        'fn listing_query(&self) { "query { comics { id title } }"; }\n'
        'fn category_query(&self) { "query { allCategory { id name } }"; }\n'
        "fn get_dynamic_filters(&self) { self.category_query(); }\n"
        "fn get_search_manga_list(&self) { self.listing_query(); }\n"
        'fn post_query(&self) { Request::post(url).header("Cookie", cookie); }\n'
        "fn get_page_list(&self) { PageContent::url_context(image_url, context); }\n"
        "fn get_image_request(&self, context: Option<PageContext>) { "
        'context.get("referer"); Request::get(url).header("Cookie", cookie); }\n'
        "fn manga_query(&self, needs_details: bool, needs_chapters: bool) {\n"
        "match (needs_details, needs_chapters) {\n"
        '(true, true) => "comicById chaptersByComicId",\n'
        '(true, false) => "comicById",\n'
        '(false, true) => "chaptersByComicId",\n'
        '_ => "",\n}\n}\n'
        "fn get_manga_update(&self, needs_details: bool, needs_chapters: bool) {\n"
        "self.manga_query(needs_details, needs_chapters);\n}\n",
        traits=("DynamicFilters", "ImageRequestProvider"),
        dependencies=(DependencyRequest(name="serde"),),
        resources={
            "res/settings.json": (
                '[{"type":"group","title":"Source","items":'
                '[{"type":"text","key":"cookie","title":"Cookie"}]}]'
            )
        },
    )

    gaps = _contract_messages(ir, manifest)

    performance_markers = (
        "full manga listing",
        "detail-only fields",
        "PageContent::url_context",
        "Cookie session",
        "only the data requested",
    )
    assert not [gap for gap in gaps if any(marker in gap for marker in performance_markers)]


def test_manga_update_query_must_respect_requested_data_flags() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={"capabilities": [Capability.DETAILS, Capability.CHAPTERS]}
        )
    manifest = generation_manifest(
        'fn manga_query(&self) { let query = "query { comicById { id } '
        'chaptersByComicId { id } }"; }\n'
        "fn get_manga_update(&self, needs_details: bool, needs_chapters: bool) {\n"
        "if !needs_details && !needs_chapters { return; }\n"
        "self.manga_query();\n"
        "}\n"
    )

    gaps = _contract_messages(ir, manifest)

    assert any("only the data requested" in gap for gap in gaps)

    conditional = manifest.model_copy(
        update={
            "files": [
                GeneratedFile(
                    path="src/lib.rs",
                    content=(
                        "fn manga_query(&self, needs_details: bool, needs_chapters: bool) {\n"
                        "match (needs_details, needs_chapters) {\n"
                        '(true, true) => "comicById chaptersByComicId",\n'
                        '(true, false) => "comicById",\n'
                        '(false, true) => "chaptersByComicId",\n'
                        '_ => "",\n}\n}\n'
                        "fn get_manga_update(&self, needs_details: bool, needs_chapters: bool) {\n"
                        "self.manga_query(needs_details, needs_chapters);\n}\n"
                    ),
                )
            ]
        }
    )

    resolved_gaps = _contract_messages(ir, conditional)

    assert not any("only the data requested" in gap for gap in resolved_gaps)


def test_rest_chapter_helper_cannot_repeat_the_detail_request() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={
                "capabilities": [
                    Capability.JSON_API,
                    Capability.DETAILS,
                    Capability.CHAPTERS,
                ]
            }
        )
    repeated = generation_manifest(
        "fn fetch_chapters(&self, id: &str) {\n"
        'self.get(format!("/comic2/{id}"));\n}\n'
        "fn get_manga_update(&self, needs_details: bool, needs_chapters: bool) {\n"
        'if needs_details { self.get(format!("/comic2/{id}")); }\n'
        "if needs_chapters { self.fetch_chapters(id); }\n}\n",
        dependencies=(DependencyRequest(name="serde"),),
    )
    reused = repeated.model_copy(
        update={
            "files": [
                GeneratedFile(
                    path="src/lib.rs",
                    content=(
                        "fn fetch_chapters(&self, detail: &Detail) { parse(detail); }\n"
                        "fn get_manga_update(\n"
                        "    &self, needs_details: bool, needs_chapters: bool,\n"
                        ") {\n"
                        "let detail = if needs_details || needs_chapters {\n"
                        'Some(self.get(format!("/comic2/{id}")))\n'
                        "} else { None };\n"
                        "if needs_details { apply(detail.as_ref().unwrap()); }\n"
                        "if needs_chapters { "
                        "self.fetch_chapters(detail.as_ref().unwrap()); }\n}\n"
                    ),
                )
            ]
        }
    )

    repeated_gaps = _contract_messages(ir, repeated)
    reused_gaps = _contract_messages(ir, reused)

    assert any("same REST detail route twice" in gap for gap in repeated_gaps)
    assert not any("same REST detail route twice" in gap for gap in reused_gaps)


def test_dynamic_filter_ids_must_be_read_in_search_mapping() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={"capabilities": [Capability.DYNAMIC_FILTERS]}
        )
    without_mapping = generation_manifest(
        "fn get_dynamic_filters(&self) -> Result<Vec<Filter>> {\n"
        'Ok(vec![SelectFilter { id: "theme".into(), '
        "..Default::default() }.into()])\n}\n"
        "fn get_search_manga_list(&self, filters: Vec<FilterValue>) {\n"
        "let _ = filters;\n}\n",
        traits=("DynamicFilters",),
    )
    with_mapping = without_mapping.model_copy(
        update={
            "files": [
                GeneratedFile(
                    path="src/lib.rs",
                    content=(
                        without_mapping.files[0].content
                        + "fn selected_theme(filters: &[FilterValue]) {\n"
                        + 'let theme = selected(filters, "theme");\n}\n'
                    ).replace(
                        "let _ = filters;",
                        "let theme = selected_theme(&filters);",
                    ),
                )
            ]
        }
    )

    bad_gaps = _contract_messages(ir, without_mapping)
    good_gaps = _contract_messages(ir, with_mapping)

    assert any("dynamic filter 'theme' is never read" in gap for gap in bad_gaps)
    assert not any("dynamic filter 'theme' is never read" in gap for gap in good_gaps)


def test_decompiled_json_source_requires_idempotent_get_retry() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={
                "source_format": "decompiled_apk",
                "capabilities": [Capability.JSON_API],
            }
        )
    without_retry = generation_manifest(
        RUST_SOURCE,
        dependencies=(DependencyRequest(name="serde"),),
    )
    with_retry = without_retry.model_copy(
        update={
            "files": [
                GeneratedFile(
                    path="src/lib.rs",
                    content=(
                        RUST_SOURCE
                        + "\nfn get_json(url: String) -> Result<Value> {\n"
                        + "let response = match request(url.clone())?.send() {\n"
                        + "Ok(response) => response, Err(_) => request(url)?.send()?, };\n"
                        + "response.get_json_owned()\n}\n"
                    ),
                )
            ]
        }
    )

    bad_gaps = _contract_messages(ir, without_retry)
    good_gaps = _contract_messages(ir, with_retry)

    assert any("one-retry helper" in gap for gap in bad_gaps)
    assert not any("one-retry helper" in gap for gap in good_gaps)


def test_kotlin_http_source_requires_idempotent_get_retry() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved)
    without_retry = generation_manifest(
        "fn fetch(&self, url: String) -> Result<Response> {\nRequest::get(url)?.send()\n}\n"
    )
    with_retry = without_retry.model_copy(
        update={
            "files": [
                GeneratedFile(
                    path="src/lib.rs",
                    content=(
                        "fn fetch(&self, url: String) -> Result<Response> {\n"
                        "let response = match Request::get(url.clone())?.send() {\n"
                        "Ok(response) => response,\n"
                        "Err(_) => Request::get(url)?.send()?,\n};\n"
                        "Ok(response)\n}\n"
                    ),
                )
            ]
        }
    )

    bad_gaps = _contract_messages(ir, without_retry)
    good_gaps = _contract_messages(ir, with_retry)

    assert any("Kotlin HttpSource" in gap and "one-retry" in gap for gap in bad_gaps)
    assert not any("Kotlin HttpSource" in gap and "one-retry" in gap for gap in good_gaps)


def test_post_api_does_not_inherit_get_retry_requirement_from_image_provider() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved)
    manifest = generation_manifest(
        "fn post_query(&self, url: String) -> Result<Response> {\n"
        "Request::post(url).send()\n}\n"
        "fn get_image_request(&self, url: String) -> Result<Request> {\n"
        "Request::get(url)\n}\n",
        traits=("ImageRequestProvider",),
    )

    gaps = _contract_messages(ir, manifest)

    assert not any("Kotlin HttpSource" in gap and "one-retry" in gap for gap in gaps)


def test_chapter_parser_cannot_compile_regex_on_every_request() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved)
    manifest = generation_manifest(
        "fn parse_chapters(&self, data: &str) {\n"
        'let expression = Regex::new(r"chapters: \\[\\{{.*?\\}}\\]").unwrap();\n'
        "let _ = expression.find(data);\n}\n",
        dependencies=(DependencyRequest(name="regex"),),
    )

    gaps = _contract_messages(ir, manifest)

    assert any("compiles Regex::new on every chapter parse" in gap for gap in gaps)


def test_chapter_metadata_and_legacy_settings_are_contract_gaps() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={
                "files": [
                    SourceFile(
                        path="src/Example.kt",
                        sha256="0",
                        content=(
                            "fun chapters(): List<SChapter> = values.map { "
                            "SChapter.create().apply {"
                            ' url = "/chapters/1"; date_upload = 1; scanlator = "group" } }\n'
                            'when (value) { "zh-hant" -> ""; "zh-hans" -> "cn" }'
                        ),
                    )
                ]
            }
        )
    manifest = generation_manifest(RUST_SOURCE)

    gaps = _contract_messages(ir, manifest)

    assert any("date_uploaded" in gap for gap in gaps)
    assert any("scanlators" in gap for gap in gaps)
    assert any("Chapter values omit url" in gap for gap in gaps)
    legacy_gap = next(gap for gap in gaps if "legacy input values" in gap)
    assert "zh-hant" in legacy_gap
    assert "zh-hans" in legacy_gap


def test_encrypted_json_api_dependencies_and_base_url_are_contract_gaps() -> None:
    with resolve_source(str(ENCRYPTED_API_FIXTURE)) as resolved:
        ir = analyze_source(resolved)
    manifest = generation_manifest(RUST_SOURCE)

    gaps = _contract_messages(ir, manifest)

    assert "JSON API source generated no pinned serde dependency" in gaps
    assert any("aes, cbc, serde, serde_json" in gap for gap in gaps)
    assert "encrypted JSON source requested neither hex nor base64 decoding" in gaps
    assert "dynamic base URL source generated no res/settings.json" in gaps
    assert "dynamic base URL source generated no validated defaults_get resolver" in gaps


def test_triple_des_request_requires_dependencies_and_live_millisecond_time() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved)
    ir = ir.model_copy(update={"capabilities": [*ir.capabilities, Capability.TRIPLE_DES_CBC]})
    manifest = generation_manifest(RUST_SOURCE + '\nfn sign() { let time = "0"; }\n')

    gaps = _contract_messages(ir, manifest)

    assert any("base64, cbc, des" in gap for gap in gaps)
    assert any("live millisecond Unix timestamp" in gap for gap in gaps)
