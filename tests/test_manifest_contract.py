import json
from pathlib import Path

from convert2aidoku.analyzer import analyze_source
from convert2aidoku.ingest import resolve_source
from convert2aidoku.manifest_contract import (
    ContractDiagnostic,
    ContractEvaluation,
    evaluate_manifest_contract,
    normalize_decompiled_dto_manifest,
    normalize_decompiled_setting_manifest,
)
from convert2aidoku.models import (
    Capability,
    DependencyRequest,
    GeneratedFile,
    GenerationManifest,
    ImageUrlPolicy,
    SourceFile,
)
from tests.scenarios import minimal_source_ir

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


def test_detail_request_dedup_ignores_relative_manga_key_prefixes() -> None:
    ir = minimal_source_ir(capabilities=[Capability.DETAILS, Capability.CHAPTERS])
    harmless = evaluate_manifest_contract(
        ir,
        _manifest(
            """
            fn get_manga_update(&self, needs_details: bool, needs_chapters: bool) {
                let key = manga.key.strip_prefix("/comic/");
                self.chapter_url(key);
            }
            fn chapter_url(&self, key: &str) { format!("/comic/{key}"); }
            """
        ),
    )
    repeated = evaluate_manifest_contract(
        ir,
        _manifest(
            """
            fn get_manga_update(&self, needs_details: bool, needs_chapters: bool) {
                self.request("/api/v3/comic2/");
                self.chapter_detail();
            }
            fn chapter_detail(&self) { self.request("/api/v3/comic2/"); }
            """
        ),
    )

    assert not any("same REST detail route" in message for message in harmless.messages)
    assert any("same REST detail route" in message for message in repeated.messages)


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


def test_image_resolution_contract_gap_selects_only_resolution_function(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "lib.rs").write_text(
        (
            "fn replace_resolution(url: &str) -> String { url.to_string() }\n"
            "fn unrelated() { preserve(); }\n"
        ),
        encoding="utf-8",
    )
    evaluation = ContractEvaluation(
        (ContractDiagnostic("chapter image resolution scope is incomplete", "image_resolution"),)
    )

    repair = evaluation.repair(tmp_path)

    assert repair is not None
    assert [item["content"] for item in repair.excerpts] == [
        "fn replace_resolution(url: &str) -> String { url.to_string() }"
    ]


def test_decompiled_dto_map_value_mismatch_is_a_targeted_contract_gap(
    tmp_path: Path,
) -> None:
    ir = minimal_source_ir(
        source_format="decompiled_apk",
        files=[
            SourceFile(
                path="sources/example/api/dto/ComicDetailResult.java",
                sha256="0",
                content=(
                    "public final class ComicDetailResult {\n"
                    "private final Map<String, GroupInfo> groups;\n"
                    "}\n"
                ),
            ),
            SourceFile(
                path="sources/example/api/dto/GroupInfo.java",
                sha256="0",
                content=("public final class GroupInfo {\nprivate final String name;\n}\n"),
            ),
        ],
    )
    manifest = _manifest(
        "use alloc::collections::BTreeMap;\n"
        "struct ComicDetailResult { groups: BTreeMap<String, String> }\n"
    )
    source = tmp_path / "src"
    source.mkdir()
    (source / "lib.rs").write_text(manifest.files[0].content, encoding="utf-8")

    evaluation = evaluate_manifest_contract(ir, manifest)
    repair = evaluation.repair(tmp_path)

    assert evaluation.diagnostics == (
        ContractDiagnostic(
            "decompiled DTO ComicDetailResult.groups is Map<String, GroupInfo>, but the "
            "generated Rust field is BTreeMap<String, String>; preserve the recovered map "
            "key/value DTO types so detail JSON can deserialize",
            "dto_shape",
        ),
    )
    assert repair is not None
    assert repair.excerpts[0]["content"] == (
        "struct ComicDetailResult { groups: BTreeMap<String, String> }"
    )


def test_decompiled_dto_map_value_contract_accepts_matching_rust_shape() -> None:
    ir = minimal_source_ir(
        source_format="decompiled_apk",
        files=[
            SourceFile(
                path="sources/example/api/dto/ComicDetailResult.java",
                sha256="0",
                content=(
                    "public final class ComicDetailResult {\n"
                    "private final Map<String, GroupInfo> groups;\n"
                    "}\n"
                ),
            )
        ],
    )
    manifest = _manifest(
        "use alloc::collections::BTreeMap;\n"
        "struct ComicDetailResult { groups: Option<BTreeMap<String, GroupInfo>> }\n"
        "struct GroupInfo { name: String }\n"
    )

    assert evaluate_manifest_contract(ir, manifest).messages == []


def test_terminal_rfind_image_resolution_scope_satisfies_contract() -> None:
    ir = minimal_source_ir(
        image_url_policy=ImageUrlPolicy(
            preserve_cover_urls=False,
            chapter_resolution_regex=r"\d+(?=x\.(?:jpg|webp)$)",
        )
    )
    manifest = _manifest(
        """
        fn replace_resolution(url: &str) -> String {
            let pos = url.rfind("x.jpg").filter(|&p| p + 5 == url.len())
                .or_else(|| url.rfind("x.webp").filter(|&p| p + 6 == url.len()));
            url.to_string()
        }
        """
    )

    assert evaluate_manifest_contract(ir, manifest).messages == []


def test_dynamic_filter_contract_allows_deserializing_option_dto() -> None:
    ir = minimal_source_ir(capabilities=[Capability.DYNAMIC_FILTERS])
    manifest = _manifest(
        """
        impl DynamicFilters for SourceImpl {
            fn get_dynamic_filters(&self) -> Result<Vec<Filter>> {
                let result: ApiResponse<ThemeResult> = serde_json::from_str(&json)?;
                Ok(result.results.into_filters())
            }
        }
        """
    ).model_copy(update={"implemented_traits": ["DynamicFilters"]})

    assert evaluate_manifest_contract(ir, manifest).messages == []


def test_dynamic_filter_contract_rejects_deserializing_filter_itself() -> None:
    ir = minimal_source_ir(capabilities=[Capability.DYNAMIC_FILTERS])
    manifest = _manifest(
        """
        impl DynamicFilters for SourceImpl {
            fn get_dynamic_filters(&self) -> Result<Vec<Filter>> {
                let filters: Vec<Filter> = serde_json::from_str(&json)?;
                Ok(filters)
            }
        }
        """
    ).model_copy(update={"implemented_traits": ["DynamicFilters"]})

    assert any(
        "attempts to deserialize aidoku::Filter" in message
        for message in evaluate_manifest_contract(ir, manifest).messages
    )


def test_manual_terminal_image_resolution_scope_satisfies_contract() -> None:
    ir = minimal_source_ir(
        image_url_policy=ImageUrlPolicy(
            preserve_cover_urls=False,
            chapter_resolution_regex=r"\d+(?=x\.(?:jpg|webp)$)",
        )
    )
    manifest = _manifest(
        """
        fn replace_resolution(url: &str, resolution: &str) -> String {
            let suffix_start = if url.ends_with(".jpg") {
                Some(url.len() - 4)
            } else if url.ends_with(".webp") {
                Some(url.len() - 5)
            } else {
                None
            };
            if let Some(suffix_start) = suffix_start {
                let before_suffix = &url[..suffix_start];
                if let Some(x_pos) = before_suffix.rfind('x') {
                    let before_x = &before_suffix[..x_pos];
                    let digits_start = before_x
                        .rfind(|character: char| !character.is_ascii_digit())
                        .map_or(0, |position| position + 1);
                    if digits_start < x_pos {
                        return format!("{}{}{}", &url[..digits_start], resolution, &url[x_pos..]);
                    }
                }
            }
            url.to_string()
        }
        """
    )

    assert evaluate_manifest_contract(ir, manifest).messages == []


def test_exact_recovered_image_resolution_regex_satisfies_contract() -> None:
    ir = minimal_source_ir(
        image_url_policy=ImageUrlPolicy(
            preserve_cover_urls=False,
            chapter_resolution_regex=r"\d+(?=x\.(?:jpg|webp)$)",
        )
    )
    manifest = _manifest(
        r"""
        fn replace_resolution(url: &str, resolution: &str) -> String {
            Regex::new(r"\d+(?=x\.(?:jpg|webp)$)")
                .unwrap()
                .replace(url, resolution)
                .to_string()
        }
        """
    )

    assert evaluate_manifest_contract(ir, manifest).messages == []


def test_decompiled_dto_serialized_name_is_normalized_deterministically() -> None:
    ir = minimal_source_ir(
        source_format="decompiled_apk",
        files=[
            SourceFile(
                path="sources/example/api/dto/ThemeResult.java",
                sha256="0",
                content="""
                public final class ThemeResult {
                    // themeList -> "list"
                    private final List<ThemeDetail> themeList;
                }
                """,
            )
        ],
    )
    manifest = _manifest(
        """
        struct ThemeResult {
            #[serde(rename = "themeList")]
            theme_list: Vec<ThemeDetail>,
        }
        """
    )

    assert any(
        "serialized name 'list'" in gap for gap in evaluate_manifest_contract(ir, manifest).messages
    )

    normalized = normalize_decompiled_dto_manifest(ir, manifest)

    assert '#[serde(rename = "list")]' in normalized.files[0].content
    assert evaluate_manifest_contract(ir, normalized).messages == []


def test_decompiled_dto_rust_keyword_field_gets_serde_rename() -> None:
    ir = minimal_source_ir(
        source_format="decompiled_apk",
        files=[
            SourceFile(
                path="sources/example/api/dto/Recommendation.java",
                sha256="0",
                content="""
                public final class Recommendation {
                    private final int type;
                }
                """,
            )
        ],
    )
    manifest = _manifest("struct Recommendation { type_: i32 }")

    normalized = normalize_decompiled_dto_manifest(ir, manifest)

    assert '#[serde(rename = "type")]' in normalized.files[0].content
    assert evaluate_manifest_contract(ir, normalized).messages == []


def test_decompiled_dto_field_overrides_incompatible_struct_rename_all() -> None:
    ir = minimal_source_ir(
        source_format="decompiled_apk",
        files=[
            SourceFile(
                path="sources/example/api/dto/ThemeDetail.java",
                sha256="0",
                content="""
                public final class ThemeDetail {
                    // pathWord -> "path_word"
                    private final String pathWord;
                }
                """,
            )
        ],
    )
    manifest = _manifest(
        """
        #[serde(rename_all = "camelCase")]
        struct ThemeDetail { path_word: String }
        """
    )

    normalized = normalize_decompiled_dto_manifest(ir, manifest)

    assert '#[serde(rename = "path_word")]' in normalized.files[0].content


def test_decompiled_enum_setting_names_are_projected_to_storage_values() -> None:
    ir = minimal_source_ir(
        source_format="decompiled_apk",
        files=[
            SourceFile(
                path="sources/example/ApiDomainOption.java",
                sha256="0",
                content="""
                public enum ApiDomainOption {
                    COPY1("api.example", "api.example", "Primary"),
                    COPY2("api2.example", "api2.example", "Secondary"),
                    CUSTOM("custom", "custom", "Custom");
                    public static final String KEY = "v2.pref.api_domain";
                }
                """,
            )
        ],
    )
    manifest = _manifest("struct Source;")
    manifest.files.append(
        GeneratedFile(
            path="res/settings.json",
            content="""[
                {"type":"group","items":[
                    {"type":"select","key":"v2.pref.api_domain","title":"Domain",
                     "values":["COPY1","COPY2","CUSTOM"],
                     "titles":["Primary","Secondary","Custom"],"default":"COPY1"}
                ]}
            ]""",
        )
    )

    normalized = normalize_decompiled_setting_manifest(ir, manifest)

    settings_file = next(file for file in normalized.files if file.path == "res/settings.json")
    setting = json.loads(settings_file.content)[0]["items"][0]
    assert setting["values"] == ["api.example", "api2.example", "custom"]
    assert setting["default"] == "api.example"
