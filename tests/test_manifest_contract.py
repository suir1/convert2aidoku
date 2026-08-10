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
    ChapterPageRoute,
    ChapterPageRouteVariant,
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
TRANSPORT_HEADER_MESSAGE = (
    "generated requests set Accept-Encoding manually; omit it because the Aidoku runtime "
    "owns response decompression and may otherwise expose compressed bytes to HTML/JSON parsers"
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

    assert decompiled.diagnostics == (
        ContractDiagnostic(
            DECOMPILED_RETRY_MESSAGE,
            "retry",
            "missing_decompiled_get_retry",
        ),
    )
    assert (
        ContractDiagnostic(KOTLIN_RETRY_MESSAGE, "retry", "missing_kotlin_get_retry")
        in kotlin.diagnostics
    )
    assert (
        ContractDiagnostic(CHAPTER_REGEX_MESSAGE, "chapter_regex", "chapter_regex_hot_path")
        in chapter.diagnostics
    )
    assert decompiled.messages == [DECOMPILED_RETRY_MESSAGE]
    assert decompiled.rule_ids == ["missing_decompiled_get_retry"]


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
    assert evaluation.rule_ids == []
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


def test_manual_accept_encoding_is_a_targeted_contract_gap(tmp_path: Path) -> None:
    content = (
        'fn fetch(&self) { Request::get("https://example.test")'
        '.header("Accept-Encoding", "gzip"); }\n'
        "fn unrelated(&self) { preserve(); }\n"
    )
    evaluation = evaluate_manifest_contract(minimal_source_ir(), _manifest(content))
    source = tmp_path / "src"
    source.mkdir()
    (source / "lib.rs").write_text(content, encoding="utf-8")

    repair = evaluation.repair(tmp_path)

    assert evaluation.diagnostics == (
        ContractDiagnostic(
            TRANSPORT_HEADER_MESSAGE,
            "transport_header",
            "runtime_managed_headers",
        ),
    )
    assert repair is not None
    assert [item["content"] for item in repair.excerpts] == [
        'fn fetch(&self) { Request::get("https://example.test")'
        '.header("Accept-Encoding", "gzip"); }'
    ]


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
            "decompiled_dto_shape",
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


def test_settings_contract_lists_keys_not_consumed_by_generated_rust() -> None:
    ir = minimal_source_ir(capabilities=[Capability.SETTINGS])
    manifest = _manifest(
        """
        const POPULAR_KEY: &str = "POPULAR_MANGA_DISPLAY";
        fn popular() { defaults_get::<String>(POPULAR_KEY); }
        """
    )
    manifest.files.append(
        GeneratedFile(
            path="res/settings.json",
            content="""[
                {"type":"group","title":"Source","items":[
                    {"type":"select","key":"POPULAR_MANGA_DISPLAY","title":"Popular",
                     "titles":["Weekly"],"values":["week"],"default":"week"},
                    {"type":"text","key":"RATE_LIMIT","title":"Rate","default":"10/10"}
                ]}
            ]""",
        )
    )

    messages = evaluate_manifest_contract(ir, manifest).messages

    gap = next(message for message in messages if "does not consume source settings" in message)
    assert "RATE_LIMIT" in gap
    assert "POPULAR_MANGA_DISPLAY" not in gap


def test_settings_contract_tracks_constant_through_defaults_wrapper() -> None:
    ir = minimal_source_ir(capabilities=[Capability.SETTINGS])
    manifest = _manifest(
        """
        const RESOLUTION_KEY: &str = "v2.pref.resolution";
        fn setting(key: &str, default: &str) -> String {
            defaults_get::<String>(key).unwrap_or_else(|| String::from(default))
        }
        fn resolution() -> String { setting(RESOLUTION_KEY, "resolution.r1500") }
        """
    )
    manifest.files.append(
        GeneratedFile(
            path="res/settings.json",
            content="""[
                {"type":"group","title":"Source","items":[
                    {"type":"select","key":"v2.pref.resolution","title":"Resolution",
                     "titles":["1500"],"values":["resolution.r1500"],
                     "default":"resolution.r1500"}
                ]}
            ]""",
        )
    )

    evaluation = evaluate_manifest_contract(ir, manifest)

    assert "unread_settings" not in evaluation.rule_ids


def test_settings_contract_ignores_non_json_rust_string_constants() -> None:
    ir = minimal_source_ir(capabilities=[Capability.SETTINGS])
    manifest = _manifest(
        """
        const COMIC_BODY: &str = "
        {
          id
          title
        }
        ";
        const CHAPTER_FILTER_PREF: &str = "CHAPTER_FILTER";
        fn chapter_filter() -> String {
            defaults_get::<String>(CHAPTER_FILTER_PREF).unwrap_or_default()
        }
        """
    )
    manifest.files.append(
        GeneratedFile(
            path="res/settings.json",
            content='[{"type":"text","key":"CHAPTER_FILTER","title":"Chapters"}]',
        )
    )

    evaluation = evaluate_manifest_contract(ir, manifest)

    assert "unread_settings" not in evaluation.rule_ids


def test_settings_contract_does_not_conflate_same_named_functions() -> None:
    ir = minimal_source_ir(capabilities=[Capability.SETTINGS])
    manifest = _manifest(
        """
        const RESOLUTION_KEY: &str = "v2.pref.resolution";
        fn setting(key: &str, default: &str) -> String {
            defaults_get::<String>(key).unwrap_or_else(|| String::from(default))
        }
        mod unrelated {
            pub fn setting(_key: &str, default: &str) -> String { String::from(default) }
        }
        fn resolution() -> String {
            unrelated::setting(RESOLUTION_KEY, "resolution.r1500")
        }
        """
    )
    manifest.files.append(
        GeneratedFile(
            path="res/settings.json",
            content="""[
                {"type":"group","title":"Source","items":[
                    {"type":"select","key":"v2.pref.resolution","title":"Resolution",
                     "titles":["1500"],"values":["resolution.r1500"],
                     "default":"resolution.r1500"}
                ]}
            ]""",
        )
    )

    evaluation = evaluate_manifest_contract(ir, manifest)

    assert "unread_settings" in evaluation.rule_ids


def test_static_filter_and_setting_capabilities_require_resource_files() -> None:
    filters = evaluate_manifest_contract(
        minimal_source_ir(capabilities=[Capability.FILTERS]),
        _manifest("fn source() {}"),
    )
    settings = evaluate_manifest_contract(
        minimal_source_ir(capabilities=[Capability.SETTINGS]),
        _manifest("fn source() {}"),
    )

    assert "missing_filters_resource" in filters.rule_ids
    assert "missing_settings_resource" in settings.rule_ids


def test_contextual_chapter_url_contract_requires_complete_resolution() -> None:
    ir = minimal_source_ir(capabilities=[Capability.CONTEXTUAL_CHAPTER_URLS])
    incomplete = _manifest(
        """
        fn chapters(href: &str) {
            if href == "javascript:cid(1)" { return; }
        }
        """
    )
    complete = _manifest(
        """
        fn contextual_chapter(direction: &str, body: &str) {
            let placeholder = "javascript:cid(1)";
            let prev = "#prev";
            let next = "#next";
            let previous_marker = "url_previous:'";
            let next_marker = "url_next:'";
            let fallback = "/read/1/2.html";
            let resolved = fallback.replace(".", "_2.");
        }
        """
    )

    assert any(
        "placeholder chapter URLs" in message
        for message in evaluate_manifest_contract(ir, incomplete).messages
    )
    assert not any(
        "placeholder chapter URLs" in message
        for message in evaluate_manifest_contract(ir, complete).messages
    )


def test_chapter_page_route_contract_requires_recovered_prefix_removal() -> None:
    ir = minimal_source_ir(
        chapter_page_routes=[
            ChapterPageRoute(
                source_method="chapterContentDetailUrl",
                chapter_key_template="/comic/{comic_path}/chapter/{chapter_id}",
                endpoint_template="/api/v3/comic/{normalized_chapter_key}",
                variants=[
                    ChapterPageRouteVariant(
                        name="default",
                        condition="default API domain",
                        is_default=True,
                        strip_prefix="/comic/",
                    )
                ],
            )
        ]
    )

    evaluation = evaluate_manifest_contract(ir, _manifest("fn page_list() {}"))

    assert "chapter_route_strip_prefix" in evaluation.rule_ids


def test_shared_request_headers_are_required_by_contract() -> None:
    ir = minimal_source_ir(shared_request_headers={"User-Agent": "Mozilla/5.0 Test Browser"})
    incomplete = _manifest("fn request(url: String) { Request::get(url); }")
    complete = _manifest(
        'fn request(url: String) { Request::get(url).header("User-Agent", '
        '"Mozilla/5.0 Test Browser"); }'
    )

    assert any(
        "source-wide headers: User-Agent" in message
        for message in evaluate_manifest_contract(ir, incomplete).messages
    )
    assert not any(
        "source-wide headers" in message
        for message in evaluate_manifest_contract(ir, complete).messages
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
                    private static final String DEFAULT;
                    static { DEFAULT = COPY2.entryKey; }
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
    assert setting["default"] == "api2.example"


def test_decompiled_enum_default_overrides_ai_storage_value_guess() -> None:
    ir = minimal_source_ir(
        source_format="decompiled_apk",
        files=[
            SourceFile(
                path="sources/example/ApiDomainOption.java",
                sha256="0",
                content="""
                public enum ApiDomainOption {
                    FIRST("api.example", "api.example", "Primary"),
                    SECOND("api2.example", "api2.example", "Secondary");
                    public static final String KEY = "v2.pref.api_domain";
                    private static final String DEFAULT;
                    static { DEFAULT = SECOND.entryKey; }
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
                     "values":["api.example","api2.example"],
                     "titles":["Primary","Secondary"],"default":"api.example"}
                ]}
            ]""",
        )
    )

    normalized = normalize_decompiled_setting_manifest(ir, manifest)

    settings_file = next(file for file in normalized.files if file.path == "res/settings.json")
    setting = json.loads(settings_file.content)[0]["items"][0]
    assert setting["default"] == "api2.example"


def test_public_only_decompiled_settings_exclude_non_reading_preferences() -> None:
    ir = minimal_source_ir(source_format="decompiled_apk", feature_scope="public_only")
    manifest = _manifest("struct Source;")
    manifest.files.append(
        GeneratedFile(
            path="res/settings.json",
            content="""[
                {"type":"group","items":[
                    {"type":"select","key":"v2.pref.api_domain","title":"API Domain",
                     "values":["api.example"],"default":"api.example"},
                    {"type":"text","key":"v2.pref.web_view_link","title":"WebView Link"},
                    {"type":"text","key":"v2.pref.web_view_link_custom","title":"WebView Custom"},
                    {"type":"select","key":"v2.pref.web_view_client","title":"WebView Client",
                     "values":["desktop"],"default":"desktop"},
                    {"type":"select","key":"v2.pref.lan_option","title":"Chinese Script",
                     "values":["default"],"default":"default"},
                    {"type":"text","key":"v2.key.hide_default_continuous_chapter",
                     "title":"Shelf Update Workaround"},
                    {"type":"select","key":"v2.pref.chapter_comment_api",
                     "title":"Comment API","values":["api.example"],
                     "default":"api.example"},
                    {"type":"text","key":"v2.pref.chapter_comment_api_custom",
                     "title":"Custom Comment API"},
                    {"type":"select","key":"v2.pref.resolution","title":"Resolution",
                     "values":["1500"],"default":"1500"}
                ]}
            ]""",
        )
    )

    normalized = normalize_decompiled_setting_manifest(ir, manifest)

    settings_file = next(file for file in normalized.files if file.path == "res/settings.json")
    keys = [item["key"] for item in json.loads(settings_file.content)[0]["items"]]
    assert keys == ["v2.pref.api_domain", "v2.pref.resolution"]
