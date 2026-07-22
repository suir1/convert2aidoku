import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from convert2aidoku.ai import AIResult
from convert2aidoku.analyzer import analyze_source
from convert2aidoku.config import AISettings
from convert2aidoku.converter import (
    _apply_repair_patch,
    _capability_gaps,
    _diagnostic_file_excerpts,
    _repair_diagnostics,
    _should_repair,
    _with_live_validated_setting_defaults,
    _with_recovered_filter_defaults,
    convert_source,
    validate_existing,
)
from convert2aidoku.errors import AIProviderError
from convert2aidoku.ingest import resolve_source
from convert2aidoku.models import (
    Capability,
    ChapterPageRoute,
    ChapterPageRouteVariant,
    ConversionStatus,
    DependencyRequest,
    GeneratedFile,
    GenerationManifest,
    ImageUrlPolicy,
    RepairPatch,
    RouteReplacement,
    SourceFile,
    SourceFilterOption,
    SourceFilterSpec,
    ValidationResult,
)
from convert2aidoku.scaffold import create_scaffold

FIXTURE = Path(__file__).parent / "fixtures" / "simple"
ENCRYPTED_API_FIXTURE = Path(__file__).parent / "fixtures" / "encrypted_api"


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


class FakeClient:
    def __init__(self, settings: AISettings):
        self.settings = settings

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def generate(self, _ir: object) -> AIResult:
        return AIResult(
            manifest=GenerationManifest(
                source_struct="Simple",
                implemented_traits=["ListingProvider", "ImageRequestProvider"],
                files=[
                    GeneratedFile(path="src/lib.rs", content=RUST_SOURCE),
                    GeneratedFile(path="res/filters.json", content="[]"),
                    GeneratedFile(path="res/settings.json", content="[]"),
                ],
            ),
            structured_output=True,
        )

    def repair(
        self,
        _ir: object,
        *,
        current_files: object,
        diagnostics: str,
        manifest_history: object | None = None,
    ) -> AIResult:
        return self.generate(_ir)


class RepairingGapClient(FakeClient):
    def generate(self, _ir: object) -> AIResult:
        return AIResult(
            manifest=GenerationManifest(
                source_struct="Simple",
                files=[GeneratedFile(path="src/lib.rs", content=RUST_SOURCE)],
            ),
            structured_output=True,
        )

    def repair(
        self,
        _ir: object,
        *,
        current_files: object,
        diagnostics: str,
        manifest_history: object | None = None,
    ) -> AIResult:
        return FakeClient.generate(self, _ir)


class CountingClient(FakeClient):
    generate_calls = 0

    def generate(self, _ir: object) -> AIResult:
        type(self).generate_calls += 1
        return super().generate(_ir)


class ResourceDroppingRepairClient(FakeClient):
    def generate(self, _ir: object) -> AIResult:
        result = super().generate(_ir)
        return AIResult(
            manifest=result.manifest.model_copy(
                update={
                    "files": [
                        item for item in result.manifest.files if not item.path.startswith("res/")
                    ]
                    + [
                        GeneratedFile(
                            path="res/filters.json",
                            content=(
                                '[{"type":"select","id":"filter","options":["All"],"ids":["all"]}]'
                            ),
                        ),
                        GeneratedFile(
                            path="res/settings.json",
                            content='[{"type":"group","title":"Settings","items":[]}]',
                        ),
                    ]
                }
            ),
            structured_output=True,
        )

    def repair(
        self,
        _ir: object,
        *,
        current_files: object,
        diagnostics: str,
        manifest_history: object | None = None,
    ) -> AIResult:
        return AIResult(
            manifest=GenerationManifest(
                source_struct="Simple",
                files=[GeneratedFile(path="src/lib.rs", content=RUST_SOURCE)],
            ),
            structured_output=True,
        )


def test_conversion_orchestrates_atomic_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("convert2aidoku.converter.OpenAICompatibleClient", FakeClient)
    monkeypatch.setattr(
        "convert2aidoku.converter.validate_project",
        lambda *_args, **_kwargs: ValidationResult(build_ok=True, package_ok=True, live_ok=True),
    )
    settings = AISettings(
        base_url="http://localhost/v1",
        model="fake",
        api_key=SecretStr("secret"),
    )
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


def test_interrupted_conversion_resumes_saved_manifest_without_regeneration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    CountingClient.generate_calls = 0
    monkeypatch.setattr("convert2aidoku.converter.OpenAICompatibleClient", CountingClient)
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
    settings = AISettings(
        base_url="http://localhost/v1",
        model="fake",
        api_key=SecretStr("secret"),
    )
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
    assert CountingClient.generate_calls == 1
    assert (output / ".c2a" / "manifests" / "round-01.json").is_file()
    assert not workspace.exists()


def test_repair_preserves_source_ir_required_resources(tmp_path: Path, monkeypatch) -> None:
    original_analyze = analyze_source
    monkeypatch.setattr(
        "convert2aidoku.converter.analyze_source",
        lambda resolved: original_analyze(resolved).model_copy(
            update={"capabilities": [Capability.FILTERS, Capability.SETTINGS]}
        ),
    )
    monkeypatch.setattr(
        "convert2aidoku.converter.OpenAICompatibleClient",
        ResourceDroppingRepairClient,
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
    settings = AISettings(
        base_url="http://localhost/v1",
        model="fake",
        api_key=SecretStr("secret"),
        max_repair_rounds=1,
    )
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


def test_forced_failed_conversion_preserves_existing_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("convert2aidoku.converter.OpenAICompatibleClient", FakeClient)
    monkeypatch.setattr(
        "convert2aidoku.converter.validate_project",
        lambda *_args, **_kwargs: ValidationResult(),
    )
    settings = AISettings(
        base_url="http://localhost/v1",
        model="fake",
        api_key=SecretStr("secret"),
        max_repair_rounds=0,
    )
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
    monkeypatch.setattr("convert2aidoku.converter.OpenAICompatibleClient", FakeClient)
    monkeypatch.setattr(
        "convert2aidoku.converter.validate_project",
        lambda *_args, **_kwargs: ValidationResult(
            build_ok=True,
            package_ok=True,
            live_ok=True,
        ),
    )
    monkeypatch.setattr(
        "convert2aidoku.converter._capability_gaps",
        lambda *_args, **_kwargs: ["synthetic missing capability"],
    )
    settings = AISettings(
        base_url="http://localhost/v1",
        model="fake",
        api_key=SecretStr("secret"),
        max_repair_rounds=0,
    )
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
    monkeypatch.setattr("convert2aidoku.converter.OpenAICompatibleClient", RepairingGapClient)
    monkeypatch.setattr(
        "convert2aidoku.converter.validate_project",
        lambda *_args, **_kwargs: ValidationResult(
            build_ok=True,
            package_ok=True,
            live_ok=True,
        ),
    )
    settings = AISettings(
        base_url="http://localhost/v1",
        model="fake",
        api_key=SecretStr("secret"),
    )

    outcome = convert_source(
        str(FIXTURE),
        output=tmp_path / "generated" / "en.simple",
        settings=settings,
        live=True,
    )

    assert len(outcome.report.ai_rounds) == 2
    assert not any("generated no" in warning for warning in outcome.report.warnings)


def test_validate_preserves_conversion_audit_fields(tmp_path: Path, monkeypatch) -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        project = tmp_path / "project"
        create_scaffold(project, analyze_source(resolved), resolved)
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
    manifest = GenerationManifest(
        source_struct="Simple",
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content=(
                    "fn request(url: String) {}\n"
                    "fn update(manga: Manga) { self.request(manga.key.clone()); }"
                ),
            )
        ],
    )

    gaps = _capability_gaps(ir, manifest)

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
    manifest = GenerationManifest(
        source_struct="Simple",
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content='fn page(key: &str) { let _ = key.trim_start_matches("/comic/"); }',
            )
        ],
    )

    gaps = _capability_gaps(ir, manifest)

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
    manifest = GenerationManifest(
        source_struct="Simple",
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content=(
                    "fn image_resolution(url: String) -> String { url }\n"
                    "fn comic_to_manga(comic: Comic) -> Manga { Manga { "
                    "cover: Some(self.image_resolution(comic.cover)), "
                    "..Default::default() } }"
                ),
            )
        ],
    )

    gaps = _capability_gaps(ir, manifest)

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
    manifest = GenerationManifest(
        source_struct="Simple",
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content="fn filter(filters: Vec<FilterValue>) { "
                "if let FilterValue::Select { .. } = filters[0] {} }",
            ),
            GeneratedFile(
                path="res/filters.json",
                content=(
                    '[{"type":"select","id":"audience","title":"Audience",'
                    '"options":["Male","Female"],"ids":["male_default","female"]},'
                    '{"type":"select","id":"sort","title":"Sort",'
                    '"options":["Popular","Updated"],'
                    '"ids":["popular","datetime_updated"]}]'
                ),
            ),
        ],
    )

    gaps = _capability_gaps(ir, manifest)

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
    manifest = GenerationManifest(
        source_struct="Simple",
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content=(
                    "fn detail(&self, key: &str) -> Result<DetailResult> { "
                    "self.request(key)?.send()?.get_json_owned() }"
                ),
            )
        ],
    )

    gaps = _capability_gaps(ir, manifest)

    assert any("ApiResponse<ComicDetailResult>" in gap for gap in gaps)
    assert any("response.results" in gap for gap in gaps)


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
    manifest = GenerationManifest(
        source_struct="Simple",
        files=[
            GeneratedFile(path="src/lib.rs", content=RUST_SOURCE),
            GeneratedFile(
                path="res/filters.json",
                content=(
                    '[{"type":"select","id":"audience","options":["Male","Female"],'
                    '"ids":["male","female"]},'
                    '{"type":"sort","id":"sort","options":["Popular","Updated"]}]'
                ),
            ),
        ],
    )

    effective = _with_recovered_filter_defaults(ir, manifest)
    filters_file = next(item for item in effective.files if item.path == "res/filters.json")
    filters = json.loads(filters_file.content)

    assert filters[0]["default"] == "male"
    assert filters[1]["default"] == {"index": 1, "ascending": True}
    assert filters[1]["canAscend"] is True


def test_empty_declared_resources_are_contract_gaps() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(update={"capabilities": ["filters", "settings"]})
    manifest = GenerationManifest(
        source_struct="Simple",
        files=[
            GeneratedFile(path="src/lib.rs", content=RUST_SOURCE),
            GeneratedFile(path="res/filters.json", content="[]"),
            GeneratedFile(path="res/settings.json", content="[]"),
        ],
    )

    gaps = _capability_gaps(ir, manifest)

    assert "source declares filters but generated an empty res/filters.json" in gaps
    assert "source declares settings but generated an empty res/settings.json" in gaps


def test_declared_dynamic_filters_and_deep_links_require_providers() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={"capabilities": [Capability.DYNAMIC_FILTERS, Capability.DEEP_LINKS]}
        )
    manifest = GenerationManifest(
        source_struct="Simple",
        files=[GeneratedFile(path="src/lib.rs", content=RUST_SOURCE)],
    )

    gaps = _capability_gaps(ir, manifest)

    assert "source fetches dynamic filters but generated no DynamicFilters provider" in gaps
    assert "source declares deep links but generated no DeepLinkHandler" in gaps


def test_dynamic_filters_cannot_deserialize_aidoku_filter_from_json() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={"capabilities": [Capability.DYNAMIC_FILTERS]}
        )
    manifest = GenerationManifest(
        source_struct="Simple",
        implemented_traits=["DynamicFilters"],
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content=(
                    "fn get_dynamic_filters(&self) -> Result<Vec<Filter>> { "
                    'serde_json::from_str("[]") }'
                ),
            )
        ],
    )

    gaps = _capability_gaps(ir, manifest)

    assert any("Filter is not Deserialize" in gap for gap in gaps)


def test_dynamic_filter_ids_must_be_read_in_search_mapping() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved).model_copy(
            update={"capabilities": [Capability.DYNAMIC_FILTERS]}
        )
    without_mapping = GenerationManifest(
        source_struct="Simple",
        implemented_traits=["DynamicFilters"],
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content=(
                    "fn get_dynamic_filters(&self) -> Result<Vec<Filter>> {\n"
                    'Ok(vec![SelectFilter { id: "theme".into(), '
                    "..Default::default() }.into()])\n}\n"
                    "fn get_search_manga_list(&self, filters: Vec<FilterValue>) {\n"
                    "let _ = filters;\n}\n"
                ),
            )
        ],
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

    bad_gaps = _capability_gaps(ir, without_mapping)
    good_gaps = _capability_gaps(ir, with_mapping)

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
    without_retry = GenerationManifest(
        source_struct="Simple",
        files=[GeneratedFile(path="src/lib.rs", content=RUST_SOURCE)],
        dependencies=[DependencyRequest(name="serde")],
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

    bad_gaps = _capability_gaps(ir, without_retry)
    good_gaps = _capability_gaps(ir, with_retry)

    assert any("one-retry helper" in gap for gap in bad_gaps)
    assert not any("one-retry helper" in gap for gap in good_gaps)


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
    manifest = GenerationManifest(
        source_struct="Simple",
        files=[GeneratedFile(path="src/lib.rs", content=RUST_SOURCE)],
    )

    gaps = _capability_gaps(ir, manifest)

    assert any("date_uploaded" in gap for gap in gaps)
    assert any("scanlators" in gap for gap in gaps)
    assert any("Chapter values omit url" in gap for gap in gaps)
    legacy_gap = next(gap for gap in gaps if "legacy input values" in gap)
    assert "zh-hant" in legacy_gap
    assert "zh-hans" in legacy_gap


def test_encrypted_json_api_dependencies_and_base_url_are_contract_gaps() -> None:
    with resolve_source(str(ENCRYPTED_API_FIXTURE)) as resolved:
        ir = analyze_source(resolved)
    manifest = GenerationManifest(
        source_struct="Simple",
        files=[GeneratedFile(path="src/lib.rs", content=RUST_SOURCE)],
    )

    gaps = _capability_gaps(ir, manifest)

    assert "JSON API source generated no pinned serde dependency" in gaps
    assert any("aes, cbc, serde, serde_json" in gap for gap in gaps)
    assert "encrypted JSON source requested neither hex nor base64 decoding" in gaps
    assert "dynamic base URL source generated no res/settings.json" in gaps
    assert "dynamic base URL source generated no validated defaults_get resolver" in gaps


def test_triple_des_request_requires_dependencies_and_live_millisecond_time() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved)
    ir = ir.model_copy(update={"capabilities": [*ir.capabilities, Capability.TRIPLE_DES_CBC]})
    manifest = GenerationManifest(
        source_struct="Simple",
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content=RUST_SOURCE + '\nfn sign() { let time = "0"; }\n',
            )
        ],
    )

    gaps = _capability_gaps(ir, manifest)

    assert any("base64, cbc, des" in gap for gap in gaps)
    assert any("live millisecond Unix timestamp" in gap for gap in gaps)


def test_blocked_validation_repairs_only_when_contract_has_gaps() -> None:
    validation = ValidationResult(build_ok=True, package_ok=True, blocked=True)

    assert not _should_repair(validation, [], live=True)
    assert _should_repair(validation, ["relative URL gap"], live=True)


def test_mycomic_repair_diagnostics_distinguish_browser_and_runner() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved)
    ir = ir.model_copy(
        update={"metadata": ir.metadata.model_copy(update={"source_id": "zh.mycomic"})}
    )

    diagnostics = _repair_diagnostics(
        ir,
        ValidationResult(blocked=True),
        ["relative URL gap"],
    )

    assert "/comics?sort=-views" in diagnostics
    assert "/comics/54348" in diagnostics
    assert "/chapters/794527" in diagnostics
    assert "does not share the browser" in diagnostics


def test_copymanga_repair_diagnostics_include_public_api_headers() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved)
    ir = ir.model_copy(
        update={"metadata": ir.metadata.model_copy(update={"source_id": "zh.copymanga"})}
    )

    diagnostics = _repair_diagnostics(ir, ValidationResult(build_ok=True), [])

    assert "mapi.copy20.com/api/v3/comic2/<path>" in diagnostics
    assert "Version: 2025.11.21" in diagnostics
    assert "custom HTTP/API code 210" in diagnostics
    assert "&theme=<selected path_word>" in diagnostics


def test_compiler_diagnostics_produce_bounded_source_excerpts(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    lines = [f"line {index}" for index in range(1, 41)]
    (source / "lib.rs").write_text("\n".join(lines) + "\n", encoding="utf-8")

    excerpts = _diagnostic_file_excerpts(
        tmp_path,
        "error\n  --> src/lib.rs:20:5\nhelp\n  --> src/lib.rs:24:9",
        context_lines=3,
    )

    assert excerpts == [
        {
            "path": "src/lib.rs",
            "start_line": 17,
            "end_line": 27,
            "content": "\n".join(lines[16:27]),
        }
    ]


def test_repair_patch_requires_one_exact_match_and_preserves_manifest_metadata() -> None:
    manifest = GenerationManifest(
        source_struct="Simple",
        implemented_traits=["DynamicFilters"],
        files=[GeneratedFile(path="src/lib.rs", content="let title = title;\n")],
        dependencies=[DependencyRequest(name="serde")],
    )
    patch = RepairPatch.model_validate(
        {
            "edits": [
                {
                    "path": "src/lib.rs",
                    "old_text": "let title = title;",
                    "new_text": "let title = Some(title);",
                }
            ]
        }
    )

    repaired = _apply_repair_patch(
        manifest,
        [{"path": "src/lib.rs", "content": "let title = title;\n"}],
        patch,
        [
            {
                "path": "src/lib.rs",
                "start_line": 1,
                "end_line": 1,
                "content": "let title = title;",
            }
        ],
    )

    assert repaired.files[0].content == "let title = Some(title);\n"
    assert repaired.implemented_traits == ["DynamicFilters"]
    assert repaired.dependencies == [DependencyRequest(name="serde")]


def test_repair_patch_cannot_edit_text_outside_supplied_excerpts() -> None:
    manifest = GenerationManifest(
        source_struct="Simple",
        files=[GeneratedFile(path="src/lib.rs", content="safe();\nother();\n")],
    )
    patch = RepairPatch.model_validate(
        {"edits": [{"path": "src/lib.rs", "old_text": "other();", "new_text": "changed();"}]}
    )

    with pytest.raises(AIProviderError, match="not present in a supplied excerpt"):
        _apply_repair_patch(
            manifest,
            [{"path": "src/lib.rs", "content": "safe();\nother();\n"}],
            patch,
            [{"path": "src/lib.rs", "content": "safe();"}],
        )


def test_live_validated_setting_default_stays_inside_generated_allowlist() -> None:
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved)
    ir = ir.model_copy(
        update={
            "metadata": ir.metadata.model_copy(update={"source_id": "zh.copymanga"}),
            "capabilities": list(set(ir.capabilities) | {Capability.DYNAMIC_BASE_URLS}),
        }
    )

    def manifest(values: list[str]) -> GenerationManifest:
        settings = [
            {
                "type": "group",
                "items": [
                    {
                        "type": "select",
                        "key": "v2.pref.api_domain",
                        "titles": values,
                        "values": values,
                        "default": values[0],
                    }
                ],
            }
        ]
        return GenerationManifest(
            source_struct="Simple",
            files=[
                GeneratedFile(path="src/lib.rs", content=RUST_SOURCE),
                GeneratedFile(
                    path="res/settings.json",
                    content=json.dumps(settings),
                ),
            ],
        )

    allowed = _with_live_validated_setting_defaults(
        ir,
        manifest(["api.mangacopy.com", "mapi.copy20.com"]),
    )
    rejected = _with_live_validated_setting_defaults(
        ir,
        manifest(["api.mangacopy.com"]),
    )

    allowed_settings = json.loads(next(x.content for x in allowed.files if x.path.endswith("json")))
    rejected_settings = json.loads(
        next(x.content for x in rejected.files if x.path.endswith("json"))
    )
    assert allowed_settings[0]["items"][0]["default"] == "mapi.copy20.com"
    assert rejected_settings[0]["items"][0]["default"] == "api.mangacopy.com"
