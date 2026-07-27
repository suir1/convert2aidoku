from pathlib import Path

import pytest

from convert2aidoku.errors import SecurityError
from convert2aidoku.generated_source_metadata import GeneratedSourceMetadata
from convert2aidoku.models import (
    ChapterPageRoute,
    ChapterPageRouteVariant,
    DependencyRequest,
    GeneratedFile,
    GenerationManifest,
    ImageUrlPolicy,
)
from convert2aidoku.scaffold import (
    apply_generation_manifest,
    normalize_pinned_aidoku_rust,
    render_generated_lib_rs,
    validate_generated_content,
)
from tests.scenarios import scaffold_project


def _manifest(dependency: str | None = None) -> GenerationManifest:
    dependencies = [] if dependency is None else [DependencyRequest(name=dependency)]
    return GenerationManifest(
        source_struct="Simple",
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content=(
                    "#![no_std]\n"
                    "use aidoku::{alloc::{String, Vec}, Chapter, FilterValue, Manga, "
                    "MangaPageResult, Page, Result, Source};\n"
                    "struct Simple;\n"
                    "impl Source for Simple {\n"
                    "fn new() -> Self { Self }\n"
                    "fn get_search_manga_list(&self, _: Option<String>, _: i32, "
                    "_: Vec<FilterValue>) "
                    "-> Result<MangaPageResult> { Ok(MangaPageResult::default()) }\n"
                    "fn get_manga_update(&self, manga: Manga, _: bool, _: bool) -> Result<Manga> "
                    "{ Ok(manga) }\n"
                    "fn get_page_list(&self, _: Manga, _: Chapter) -> Result<Vec<Page>> "
                    "{ Ok(Vec::new()) }\n"
                    "}\n"
                ),
            )
        ],
        dependencies=dependencies,
    )


def test_scaffold_is_deterministic_and_preserves_license(tmp_path: Path) -> None:
    project, ir = scaffold_project(tmp_path)
    paths = apply_generation_manifest(project, ir, _manifest("serde"), query=None)

    assert (project / "res" / "icon.png").is_file()
    assert (project / "LICENSE.input").read_text().strip() == "Synthetic fixture license."
    assert "src/generated_smoke.rs" in paths
    cargo = (project / "Cargo.toml").read_text()
    assert "serde =" in cargo
    assert "rev =" in cargo


def test_scaffold_rejects_unapproved_dependency(tmp_path: Path) -> None:
    project, ir = scaffold_project(tmp_path)
    with pytest.raises(SecurityError, match="disallowed dependency"):
        apply_generation_manifest(project, ir, _manifest("reqwest"), query=None)


def test_generated_content_allows_grouped_aidoku_std_import_only() -> None:
    validate_generated_content(
        "src/parser.rs",
        "use aidoku::{imports::std::parse_date, alloc::String};",
    )

    with pytest.raises(SecurityError, match="uses std"):
        validate_generated_content(
            "src/parser.rs",
            "use aidoku::{imports::std::parse_date, alloc::String, std::fs};",
        )


def test_normalizer_recognizes_grouped_alloc_macro_import() -> None:
    content = """#![no_std]
use aidoku::{alloc::{String, format}};
fn render(value: String) -> String { format!("{value}") }
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert normalized.count("format") == content.count("format")
    assert "use aidoku::alloc::format;" not in normalized


def test_normalizer_rewrites_invalid_closure_retry_to_aidoku_error_flow() -> None:
    content = """#![no_std]
fn http_get(url: &str) -> Result<Response> {
    let make_request = || -> Result<Request> {
        Ok(Request::get(url)?)
    };

    Ok(make_request()
        .map_err(|e| RequestError::from(format!("{e:?}")))
        .and_then(|request| request.send())
        .or_else(|_| make_request()
            .map_err(|e| RequestError::from(format!("{e:?}")))
            .and_then(|request| request.send()))?)
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert "let response = match make_request()?.send()" in normalized
    assert "Err(_) => make_request()?.send()?," in normalized
    assert "RequestError::from" not in normalized


def test_normalizer_adds_idempotent_dead_code_allowance_for_generated_modules() -> None:
    content = "use aidoku::serde::Deserialize;\nstruct Dto { value: String }\n"

    normalized = normalize_pinned_aidoku_rust(content, allow_dead_code=True)

    assert normalized.startswith("#![allow(dead_code)]\n")
    assert normalize_pinned_aidoku_rust(normalized, allow_dead_code=True) == normalized


def test_scaffold_invalidates_lockfile_when_dependencies_change(tmp_path: Path) -> None:
    project, ir = scaffold_project(tmp_path)
    apply_generation_manifest(project, ir, _manifest("serde"), query=None)
    lock = project / "Cargo.lock"
    lock.write_text("stale lock", encoding="utf-8")

    apply_generation_manifest(project, ir, _manifest("serde_json"), query=None)

    assert not lock.exists()


def test_scaffold_preserves_lockfile_when_dependencies_do_not_change(tmp_path: Path) -> None:
    project, ir = scaffold_project(tmp_path)
    manifest = _manifest("serde")
    apply_generation_manifest(project, ir, manifest, query=None)
    lock = project / "Cargo.lock"
    lock.write_text("current lock", encoding="utf-8")

    apply_generation_manifest(project, ir, manifest, query=None)

    assert lock.read_text(encoding="utf-8") == "current lock"


def test_scaffold_injects_no_std_crate_attribute(tmp_path: Path) -> None:
    project, ir = scaffold_project(tmp_path)
    manifest = _manifest()
    manifest.files[0].content = manifest.files[0].content.removeprefix("#![no_std]\n")

    apply_generation_manifest(project, ir, manifest, query=None)

    lib = (project / "src" / "lib.rs").read_text(encoding="utf-8")
    assert lib.startswith("#![no_std]\n")


def test_scaffold_normalizes_pinned_no_std_compatibility(tmp_path: Path) -> None:
    project, ir = scaffold_project(tmp_path)
    manifest = _manifest()
    manifest.files[0].content += (
        "\nfn compatibility(mut chapters: Vec<Chapter>) -> Option<i64> {\n"
        'let _ = format!("{}", vec![1].len());\n'
        "let _: Option<aidoku::std::filters::SelectFilter> = None;\n"
        'let _ = format!("/comic/{comic}/group/{}//chapters", "default");\n'
        "chapters.sort_by(|left, right| right.index.cmp(&left.index));\n"
        'parse_date("2025-01-01", "yyyy-MM-dd").ok()\n'
        "}\n"
    )

    apply_generation_manifest(project, ir, manifest, query=None)

    lib = (project / "src" / "lib.rs").read_text(encoding="utf-8")
    assert "use aidoku::alloc::format;" in lib
    assert "use aidoku::alloc::vec;" in lib
    assert 'parse_date("2025-01-01", "yyyy-MM-dd").ok()' not in lib
    assert 'parse_date("2025-01-01", "yyyy-MM-dd")' in lib
    assert "aidoku::std::filters::SelectFilter" not in lib
    assert "aidoku::SelectFilter" in lib
    assert '}//chapters"' not in lib
    assert '}/chapters"' in lib
    assert "chapters.sort_by_key(|item| core::cmp::Reverse(item.index));" in lib


def test_scaffold_normalizes_pinned_request_error_and_borrow(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest.files[0].content = """#![no_std]
use aidoku::imports::net::{Request, RequestError};
fn request() -> aidoku::Result<Request> {
    let absolute = aidoku::alloc::string::String::from("https://example.com/image");
    let host = absolute.as_str();
    if host.is_empty() { return Err(RequestError::new("empty").into()); }
    Ok(Request::get(absolute)?.header("Referer", host))
}
"""
    project, ir = scaffold_project(tmp_path)
    apply_generation_manifest(project, ir, manifest, query=None)

    lib = (project / "src" / "lib.rs").read_text(encoding="utf-8")
    assert "RequestError::new" not in lib
    assert "aidoku::AidokuError::message" in lib
    assert "use aidoku::imports::net::Request;" in lib
    assert "Request::get(absolute.clone())?" in lib


def test_normalizer_repairs_unambiguous_pinned_model_shapes() -> None:
    content = """#![no_std]
use aidoku::alloc::string::String;
use aidoku::{Manga, Result};
fn update(manga: Manga) -> Result<()> {
    let path = &manga.key;
    let author = String::new();
    let _detail = Manga {
        authors: Some(author),
        ..Default::default()
    };
    let _filter: Vec<alloc::borrow::Cow<'static, str>> = Vec::new();
    let mut output = manga;
    let mut req = make_request()?;
    match req.send() {
        Ok(_) => Ok(()),
        Err(_) => {
            req = make_request()?;
            req.send()
        }
    }?;
    output.key = path.into();
    Ok(())
}
fn keep_existing_vector(author: Vec<String>) -> Manga {
    Manga {
        authors: Some(author),
        ..Default::default()
    }
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert "use aidoku::alloc::vec;" in normalized
    assert "authors: Some(vec![author])," in normalized
    assert normalized.count("authors: Some(author),") == 1
    assert "Vec<aidoku::alloc::borrow::Cow<'static, str>>" in normalized
    assert "let path = manga.key.clone();" in normalized
    assert "Ok(req.send()?)" in normalized
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_normalizer_projects_allocation_and_core_std_paths_but_not_io() -> None:
    content = """#![no_std]
extern crate std;
use std::collections::HashMap;
use std::borrow::Cow;
fn values() -> std::vec::Vec<std::string::String> {
    let _: std::cmp::Ordering = std::cmp::Ordering::Equal;
    HashMap::<String, Cow<'static, str>>::new();
    Vec::new()
}
use std::fs;
"""

    normalized = normalize_pinned_aidoku_rust(content, remove_extern_std=True)

    assert "std::collections" not in normalized
    assert "extern crate std" not in normalized
    assert "BTreeMap::<String" in normalized
    assert "aidoku::alloc::borrow::Cow" in normalized
    assert "aidoku::alloc::vec::Vec" in normalized
    assert "aidoku::alloc::string::String" in normalized
    assert "core::cmp::Ordering" in normalized
    assert "use std::fs;" in normalized


def test_generated_lib_is_derived_from_modules_source_struct_and_traits() -> None:
    content = render_generated_lib_rs(
        "CopyManga",
        ["ListingProvider", "DynamicFilters"],
        {
            "src/lib.rs",
            "src/source.rs",
            "src/request.rs",
            "src/api/mod.rs",
            "src/api/dto.rs",
        },
    )

    assert content.startswith("#![no_std]")
    assert "mod api;" in content
    assert "mod request;" in content
    assert "mod source;" in content
    assert "pub use source::CopyManga;" in content
    assert "CopyManga,\n    ListingProvider,\n    DynamicFilters" in content


def test_normalizer_repairs_pagination_moves_helpers_and_select_filter_shape() -> None:
    content = r"""
#[derive(Deserialize)]
struct ChapterListResult {
    list: Vec<Chapter>,
    total: i32,
    limit: i32,
    offset: i32,
}
fn list(result: ChapterListResult) -> MangaPageResult {
    let manga_list: Vec<Manga> = result
        .list
        .into_iter()
        .map(to_manga)
        .collect();
    MangaPageResult {
        entries: manga_list,
        has_next_page: result.has_next(),
    }
}
fn filters() -> Vec<Filter> {
    vec![aidoku::Filter::Select {
        id: "theme".into(),
        title: Some("Theme".into()),
        ..Default::default()
    }]
}
fn chapters(list_result: ChapterListResult, chapter_key: String) -> Vec<Chapter> {
    let mut chapters = Vec::new();
    for chapter in list_result.list {
        chapters.push(Chapter {
            key: chapter_key,
            url: Some(absolute_url(&chapter_key)),
            ..Default::default()
        });
    }
    if !list_result.has_next() {
        return chapters;
    }
    chapters
}
fn pages(items: Vec<Item>) {
    for (index, item) in items.into_iter().enumerate() { use_item(item); }
}
fn selected(filters: &[FilterValue], id: &str) {
    let _ = filters.iter().find(|filter| match filter {
        FilterValue::Select { id: found, value } if found == id => true,
        _ => false,
    });
}
pub fn resolve_image(url: &str, resolution: &str) -> String {
    use regex::Regex;
    Regex::new(r"\d+(?=x\.(?:jpg|webp)$)")
        .unwrap()
        .replace(url, resolution)
        .into_owned()
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert "impl ChapterListResult" in normalized
    assert "pub fn has_next(&self) -> bool" in normalized
    assert "let manga_list_has_next = result.has_next();" in normalized
    assert "has_next_page: manga_list_has_next" in normalized
    assert "aidoku::SelectFilter" in normalized
    assert "}.into()" in normalized
    assert "let list_result_has_next = list_result.has_next();" in normalized
    assert "if !list_result_has_next" in normalized
    assert "key: chapter_key.clone()" in normalized
    assert ".enumerate()" not in normalized
    assert "find(|filter| matches!(filter," in normalized
    assert "regex::Regex" not in normalized
    assert 'url.ends_with(".jpg")' in normalized
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_normalizer_repairs_common_aidoku_api_compile_shapes() -> None:
    content = r"""
use aidoku::{
    Chapter, Filter, Manga, Page, PageContent, Source,
    error::AidokuError,
    helpers::uri,
};
pub struct CopyManga;
impl Source for CopyManga {}
fn map_manga(c: Comic) -> Manga {
    let mut manga = Manga::default();
    manga.key = format!("/comic/{}", c.path_word);
    manga.author = c.author.into_iter().map(|a| a.name).collect::<Vec<_>>().join(", ");
    manga.cover = c.cover;
    manga.description = c.brief;
    manga
}
fn map_chapter(ci: ChapterInfo) -> Chapter {
    let mut chapter = Chapter::default();
    chapter.key = format!("/comic/x/chapter/{}", ci.uuid);
    chapter.title = format!("{}", ci.name);
    chapter
}
fn page(ci: ContentItem) -> Page {
    Page { index: 0, content: PageContent::url(ci.url), ..Default::default() }
}
fn pages(items: Vec<ContentItem>) -> Vec<Page> {
    items.iter().enumerate().map(|(i, ci)| {
        Page { index: i as i32, content: PageContent::url(ci.url.clone()), ..Default::default() }
    }).collect()
}
fn filter(
    options: Vec<aidoku::alloc::borrow::Cow<'static, str>>,
    ids: Vec<aidoku::alloc::borrow::Cow<'static, str>>,
) -> Filter {
    aidoku::Filter::select("theme", "Theme", options, Some(ids))
}
fn search(query: &str) -> String { uri::encode(query) }
fn create_request(url: String) -> aidoku::Request {
    let mut request = aidoku::Request::get(&url);
    request = request.header("Accept", "application/json");
    request
}
fn send_request(url: &str) -> Result<String> {
    let request = create_request(url.to_string());
    request.send()?.get_body_string()
}
fn response_status(resp: Response) -> i32 { let _ = resp.get_body_string(); resp.code() }
fn parsed(value: &str) -> i64 {
    if let Ok(timestamp) = aidoku::imports::std::parse_date(value, "yyyy-MM-dd") {
        timestamp
    } else { 0 }
}
fn grouped_chapters(groups: Vec<Group>) -> Vec<Chapter> {
    let mut all_chapters = Vec::new();
    for group in groups {
        let group_name = group.name;
        let list = group.chapters;
        all_chapters.extend(list);
        if group.total >= group.offset + group.limit { break; }
    }
    all_chapters.sort_by_key(|item| core::cmp::Reverse(item.index));
    all_chapters.into_iter().enumerate().map(|(i, ci)| {
        let prefix = if group_name.is_empty() || ci.group_path_word == "default" {
            String::new()
        } else { format!("{}：", group_name) };
        let mut chapter = Chapter::default();
        chapter.title = format!("{}{}", prefix, ci.name);
        chapter.date_uploaded = Some(i as i64);
        chapter
    }).collect()
}
"""

    normalized = normalize_pinned_aidoku_rust(
        content,
        public_base_url="https://example.com",
    )

    assert "error::AidokuError" not in normalized
    assert "use aidoku::Result;" in normalized
    assert "use aidoku::alloc::string::ToString;" in normalized
    assert "fn new() -> Self { Self }" in normalized
    assert "manga.authors = Some(" in normalized
    assert ".join(" not in normalized
    assert "manga.cover = Some(c.cover);" in normalized
    assert "manga.description = Some(c.brief);" in normalized
    assert "chapter.title = Some(format!" in normalized
    assert "index: 0" not in normalized
    assert "items.iter().map(|ci|" in normalized
    assert "aidoku::SelectFilter" in normalized
    assert "uri::encode_uri(query)" in normalized
    assert "aidoku::Request" not in normalized
    assert "fn create_request(url: String) -> Result<Request>" in normalized
    assert "let mut request = Request::get(&url)?;" in normalized
    assert "Ok(request)" in normalized
    assert "let request = create_request(url.to_string())?;" in normalized
    assert ".get_body_string()" not in normalized
    assert "resp.status_code()" in normalized
    assert "if let Some(timestamp)" in normalized
    assert "all_chapters.extend(list.into_iter().map(|chapter|" in normalized
    assert ".map(|(i, (ci, group_name))|" in normalized
    assert "Reverse(item.0.index)" in normalized
    assert "if group.total <= group.offset + group.limit" in normalized
    assert "fn absolute_url(relative: &str) -> String" in normalized
    assert "manga.url = Some(absolute_url(&manga.key));" in normalized
    assert "chapter.url = Some(absolute_url(&chapter.key));" in normalized
    cross_file_call = normalize_pinned_aidoku_rust(
        "fn send(url: String) -> Result<()> { "
        "let request = create_request(url); request.send()?; Ok(()) }",
        request_builder_helpers={"create_request"},
    )
    assert "let request = create_request(url)?;" in cross_file_call
    assert (
        normalize_pinned_aidoku_rust(
            normalized,
            public_base_url="https://example.com",
        )
        == normalized
    )


def test_normalizer_repairs_pinned_struct_import_request_and_resolution_shapes() -> None:
    content = """#![no_std]
use aidoku::{
    alloc::{string::String, vec::Vec},
    filter::SelectFilter, DeepLinkHandler, DynamicFilters, ListingProvider,
};
fn headers() -> Vec<String> { vec![String::new()] }
fn request() -> Result<Request, RequestError> {
    let mut req = Request::get("https://example.com")?
        .header("User-Agent", get_user_agent());
    if let Some((key, val)) = get_platform_header() {
        req = req.header(key, val);
    }
    Ok(req)
}
fn domain() -> String {
    let domain = defaults_get("platform").unwrap_or_else(|| "stale".to_string());
    domain
}
fn filter() -> SelectFilter {
    SelectFilter {
        id: "theme".to_string(),
        title: Some("Theme".to_string()),
        ..Default::default()
    }
}
pub fn translate_resolution(url: &str, resolution: &str) -> String {
    let re = regex::Regex::new(r"\\d+(?=x\\.(?:jpg|webp)$)").unwrap();
    re.replace(url, resolution).to_string()
}
fn manga() -> Manga {
    Manga {
        key: String::new(),
        title: String::new(),
    }
}
fn chapter() -> Chapter {
    Chapter {
        key: String::new(),
        title: None,
    }
}
fn sort(mut chapters: Vec<Chapter>) {
    chapters.sort_by(|left, right| right.date_uploaded.cmp(&left.date_uploaded));
}
register_source!(Source, ListingProvider, DynamicFilters, DeepLinkHandler);
"""

    normalized = normalize_pinned_aidoku_rust(
        content,
        setting_defaults={"platform": "1"},
    )

    assert "aidoku::filter::SelectFilter" not in normalized
    assert "filter::SelectFilter" not in normalized
    assert "SelectFilter" in normalized
    assert "use aidoku::alloc::vec;" in normalized
    assert '.header("User-Agent", &get_user_agent())' in normalized
    assert ".header(key, &val)" in normalized
    assert "let domain: String = defaults_get::<String>(" in normalized
    assert 'String::from("1")' in normalized
    assert 'id: "theme".into()' in normalized
    assert 'title: Some("Theme".into())' in normalized
    assert "regex::Regex" not in normalized
    assert normalized.count("..Default::default()") == 3
    assert "sort_by_key(|item| core::cmp::Reverse(item.date_uploaded))" in normalized
    assert "ListingProvider" not in normalized.split("register_source!", 1)[0]
    assert "DynamicFilters" not in normalized.split("register_source!", 1)[0]
    assert "DeepLinkHandler" not in normalized.split("register_source!", 1)[0]
    assert (
        normalize_pinned_aidoku_rust(
            normalized,
            setting_defaults={"platform": "1"},
        )
        == normalized
    )


def test_scaffold_applies_declared_setting_default_to_rust_fallback(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest.files[0].content += """
use aidoku::imports::defaults::defaults_get;
fn platform() -> String {
    defaults_get::<String>("platform").unwrap_or_default()
}
fn platform_with_stale_fallback() -> String {
    defaults_get::<String>("platform").unwrap_or_else(|| String::from("stale"))
}
"""
    manifest.files.append(
        GeneratedFile(
            path="res/settings.json",
            content="""[
                {"type":"group","title":"Request","items":[
                    {"type":"select","key":"platform","title":"Platform",
                     "values":["1","2"],"titles":["1","2"],"default":"1"}
                ]}
            ]""",
        )
    )
    project, ir = scaffold_project(tmp_path)

    apply_generation_manifest(project, ir, manifest, query=None)

    lib = (project / "src" / "lib.rs").read_text(encoding="utf-8")
    assert 'defaults_get::<String>("platform").unwrap_or_default()' not in lib
    assert lib.count('defaults_get::<String>("platform").unwrap_or_else(|| String::from("1"))') == 2
    assert 'String::from("stale")' not in lib


def test_scaffold_reconciles_unique_setting_suffix_aliases(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest.files[0].content += """
use aidoku::imports::defaults::defaults_get;
fn api_domain() -> String {
    defaults_get::<String>("api_domain").unwrap_or_else(|| "stale.example".into())
}
fn resolution() -> String {
    defaults_get("v2.pref.resolution").unwrap_or_else(|| "1500".into())
}
"""
    manifest.files.append(
        GeneratedFile(
            path="res/settings.json",
            content="""[
                {"type":"group","title":"Request","items":[
                    {"type":"select","key":"v2.pref.api_domain","title":"API Domain",
                     "values":["mapi.copy20.com"],"default":"mapi.copy20.com"},
                    {"type":"select","key":"v2.pref.resolution","title":"Resolution",
                     "values":["resolution.r800","resolution.r1500"],
                     "default":"resolution.r1500"}
                ]}
            ]""",
        )
    )
    project, ir = scaffold_project(tmp_path)

    apply_generation_manifest(project, ir, manifest, query=None)

    lib = (project / "src" / "lib.rs").read_text(encoding="utf-8")
    assert 'defaults_get::<String>("api_domain")' not in lib
    assert 'defaults_get::<String>("v2.pref.api_domain")' in lib
    assert 'String::from("mapi.copy20.com")' in lib
    assert 'Some("resolution.r800") => String::from("800")' in lib
    assert 'Some("resolution.r1500") => String::from("1500")' in lib
    assert '_ => String::from("1500")' in lib


def test_scaffold_maps_prefixed_platform_setting_to_protocol_header(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest.files[0].content += """
use aidoku::imports::defaults::defaults_get;
fn get_platform_header() -> Option<(&'static str, String)> {
    let platform: Option<String> = defaults_get("v2.pref.platform");
    platform.map(|value| ("platform", value))
}
"""
    manifest.files.append(
        GeneratedFile(
            path="res/settings.json",
            content="""[
                {"type":"group","title":"Request","items":[
                    {"type":"select","key":"v2.pref.platform","title":"Platform",
                     "values":["platform.none","platform.blank","platform.one",
                               "platform.two","platform.three","platform.four",
                               "platform.five"],
                     "titles":["None","Blank","1","2","3","4","5"],
                     "default":"platform.one"}
                ]}
            ]""",
        )
    )
    project, ir = scaffold_project(tmp_path)

    apply_generation_manifest(project, ir, manifest, query=None)

    lib = (project / "src" / "lib.rs").read_text(encoding="utf-8")
    assert '"platform.none" => None' in lib
    assert '"platform.blank" => Some(("platform", String::from(" ")))' in lib
    for word, number in (
        ("one", "1"),
        ("two", "2"),
        ("three", "3"),
        ("four", "4"),
        ("five", "5"),
    ):
        assert f'"platform.{word}" => Some(("platform", String::from("{number}")))' in lib
    assert 'String::from("platform.one")' in lib
    assert "platform.map(" not in lib


def test_scaffold_maps_prefixed_platform_binding_before_header_push(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest.files[0].content += """
use aidoku::alloc::{String, Vec};
use aidoku::imports::defaults::defaults_get;
fn build_headers() -> Vec<(String, String)> {
    let mut headers = Vec::new();
    let platform: String = aidoku::imports::defaults::defaults_get("v2.pref.platform")
        .unwrap_or_else(|| "platform.one".into());
    if !platform.is_empty() {
        headers.push(("platform".into(), platform));
    }
    headers
}
"""
    manifest.files.append(
        GeneratedFile(
            path="res/settings.json",
            content="""[
                {"type":"group","title":"Request","items":[
                    {"type":"select","key":"v2.pref.platform","title":"Platform",
                     "values":["platform.none","platform.blank","platform.one","platform.two"],
                     "titles":["None","Blank","1","2"],"default":"platform.one"}
                ]}
            ]""",
        )
    )
    project, ir = scaffold_project(tmp_path)

    apply_generation_manifest(project, ir, manifest, query=None)

    lib = (project / "src" / "lib.rs").read_text(encoding="utf-8")
    assert 'Some("platform.none") => String::new()' in lib
    assert 'Some("platform.blank") => String::from(" ")' in lib
    assert 'Some("platform.one") => String::from("1")' in lib
    assert 'Some("platform.two") => String::from("2")' in lib
    assert '_ => String::from("1")' in lib
    assert 'headers.push(("platform".into(), platform))' in lib


def test_scaffold_maps_prefixed_resolution_setting_to_numeric_value(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest.files[0].content += """
use aidoku::imports::defaults::defaults_get;
fn resolution() -> String {
    defaults_get::<String>("v2.pref.resolution")
        .unwrap_or_else(|| String::from("resolution.r1500"))
}
"""
    manifest.files.append(
        GeneratedFile(
            path="res/settings.json",
            content="""[
                {"type":"group","title":"Images","items":[
                    {"type":"select","key":"v2.pref.resolution","title":"Resolution",
                     "values":["resolution.r800","resolution.r1200","resolution.r1500"],
                     "titles":["800","1200","1500"],"default":"resolution.r1500"}
                ]}
            ]""",
        )
    )
    project, ir = scaffold_project(tmp_path)

    apply_generation_manifest(project, ir, manifest, query=None)

    lib = (project / "src" / "lib.rs").read_text(encoding="utf-8")
    assert 'Some("resolution.r800") => String::from("800")' in lib
    assert 'Some("resolution.r1200") => String::from("1200")' in lib
    assert 'Some("resolution.r1500") => String::from("1500")' in lib
    assert '_ => String::from("1500")' in lib


def test_scaffold_repairs_prequeried_helper_and_preserves_cover_url(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest.files[0].content += """
fn filtered_url(page: i32) -> String {
    format!("{}?date_type=day", urls::rank_url(page))
}
fn to_manga(&self, resolution: &str) -> Manga {
    let cover = self
        .cover
        .as_deref()
        .map(|value| translate_resolution(value, resolution))
        .unwrap_or_default();
    Manga { cover: Some(cover), ..Default::default() }
}
"""
    manifest.files.append(
        GeneratedFile(
            path="src/urls.rs",
            content='fn rank_url(page: i32) -> String { format!("/ranks?offset={}", page) }',
        )
    )
    project, ir = scaffold_project(tmp_path)
    ir = ir.model_copy(
        update={
            "image_url_policy": ImageUrlPolicy(
                preserve_cover_urls=True,
                chapter_resolution_regex=r"\d+(?=x\.(?:jpg|webp)$)",
            )
        }
    )

    apply_generation_manifest(project, ir, manifest, query=None)

    lib = (project / "src" / "lib.rs").read_text(encoding="utf-8")
    assert 'format!("{}&date_type=day", urls::rank_url(page))' in lib
    assert ".map(|value| translate_resolution(value, resolution))" not in lib
    assert "let cover = self.cover.clone().unwrap_or_default();" in lib


def test_scaffold_uses_public_source_base_for_generated_absolute_urls(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest.files[0].content += """
fn get_api_base() -> String { String::from("https://api.example/api/v3") }
fn absolute_url(relative: &str) -> String {
    format!("{}/{}", get_api_base(), relative.trim_start_matches('/'))
}
"""
    project, ir = scaffold_project(tmp_path)
    ir = ir.model_copy(update={"relative_url_keys": True})

    apply_generation_manifest(project, ir, manifest, query=None)

    lib = (project / "src" / "lib.rs").read_text(encoding="utf-8")
    assert 'String::from("https://api.example/api/v3")' in lib
    assert 'format!("{}/{}", "https://example.com", relative.trim_start_matches(\'/\'))' in lib


def test_scaffold_projects_recovered_chapter_key_template(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest.files[0].content += """
fn chapter_key(comic_path: &str, chapter_id: &str) -> String {
    format!("{}/chapter/{}", comic_path, chapter_id)
}
"""
    project, ir = scaffold_project(tmp_path)
    ir = ir.model_copy(
        update={
            "chapter_page_routes": [
                ChapterPageRoute(
                    source_method="content(chapter.url)",
                    chapter_key_template="/comic/{comic_path}/chapter/{chapter_id}",
                    endpoint_template="/api/v3/comic/{normalized_chapter_key}",
                    variants=[
                        ChapterPageRouteVariant(
                            name="default",
                            condition="default API domain",
                            is_default=True,
                        )
                    ],
                )
            ]
        }
    )

    apply_generation_manifest(project, ir, manifest, query=None)

    lib = (project / "src" / "lib.rs").read_text(encoding="utf-8")
    assert 'format!("/comic/{}/chapter/{}", comic_path, chapter_id)' in lib


def test_scaffold_skips_unused_decompiled_dto_fields(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest.files.append(
        GeneratedFile(
            path="src/dto.rs",
            content="""
use aidoku::{alloc::string::String, serde::Deserialize};
#[derive(Deserialize)]
struct ChapterDetail {
    group_id: String,
    name: String,
    region: Region,
    next: String,
}
#[derive(Deserialize)]
struct Region { value: String }
fn chapter_name(chapter: &ChapterDetail) -> &str { &chapter.name }
fn iterator_next(values: &mut impl Iterator<Item = String>) { let _ = values.next(); }
""",
        )
    )
    project, ir = scaffold_project(tmp_path)
    ir = ir.model_copy(update={"source_format": "decompiled_apk"})

    apply_generation_manifest(project, ir, manifest, query=None)

    dto = (project / "src" / "dto.rs").read_text(encoding="utf-8")
    assert dto.count("#[serde(skip_deserializing)]") == 4
    assert "group_id: Option<String>" in dto
    assert "region: Option<Region>" in dto
    assert "next: Option<String>" in dto
    assert "name: String" in dto


@pytest.mark.parametrize(
    ("dependency", "version"),
    [("aes", "0.8.4"), ("des", "0.8.1"), ("cbc", "0.1.2"), ("hex", "0.4.3")],
)
def test_scaffold_pins_allowed_crypto_dependencies(
    tmp_path: Path, dependency: str, version: str
) -> None:
    project, ir = scaffold_project(tmp_path)
    apply_generation_manifest(project, ir, _manifest(dependency), query=None)

    cargo = (project / "Cargo.toml").read_text(encoding="utf-8")
    assert f'{dependency} = {{ version = "={version}"' in cargo


def test_smoke_exercises_declared_popular_and_latest_listings(tmp_path: Path) -> None:
    project, ir = scaffold_project(tmp_path)
    manifest = _manifest()
    manifest.implemented_traits = ["ListingProvider"]
    apply_generation_manifest(project, ir, manifest, query=None)

    smoke = (project / "src" / "generated_smoke.rs").read_text(encoding="utf-8")
    assert 'id: "popular".into()' in smoke
    assert 'id: "latest".into()' in smoke
    assert "popular listing returned no manga" in smoke
    assert "latest listing returned no manga" in smoke
    assert "result.entries.into_iter().take(1)" in smoke
    assert "candidates.extend(popular_result.entries)" in smoke
    assert "candidates.extend(latest_result.entries)" in smoke


def test_smoke_exercises_declared_dynamic_filters_and_deep_links(tmp_path: Path) -> None:
    project, ir = scaffold_project(tmp_path)
    manifest = _manifest()
    manifest.implemented_traits = ["DynamicFilters", "DeepLinkHandler"]
    apply_generation_manifest(project, ir, manifest, query=None)

    smoke = (project / "src" / "generated_smoke.rs").read_text(encoding="utf-8")
    assert "get_dynamic_filters" in smoke
    assert "dynamic filters returned no filters" in smoke
    assert "dynamic filter returned no manga" in smoke
    assert "dynamic filter did not change manga results" in smoke
    assert "handle_deep_link" in smoke
    assert "chapter deep link returned no result" in smoke


def test_smoke_requests_manga_cover_and_exercises_static_filters(tmp_path: Path) -> None:
    project, ir = scaffold_project(tmp_path)
    manifest = _manifest()
    manifest.files.append(
        GeneratedFile(
            path="res/filters.json",
            content=(
                '[{"type":"select","id":"region","title":"Region",'
                '"options":["All","Japan"],"ids":["","japan"]}]'
            ),
        )
    )
    apply_generation_manifest(project, ir, manifest, query=None)

    smoke = (project / "src" / "generated_smoke.rs").read_text(encoding="utf-8")
    assert "manga.cover" in smoke
    assert "cover image returned HTTP" in smoke
    assert "cover image request failed after retry" in smoke
    assert 'id: "region".into()' in smoke
    assert 'value: "japan".into()' in smoke
    assert "static filter {filter_id} returned no manga" in smoke
    assert "static filter {filter_id} returned a manga with an empty title" in smoke
    assert "first image request failed after retry" in smoke


def test_smoke_skips_listing_entries_without_readable_chapters(tmp_path: Path) -> None:
    project, ir = scaffold_project(tmp_path)
    apply_generation_manifest(project, ir, _manifest(), query=None)

    smoke = (project / "src" / "generated_smoke.rs").read_text(encoding="utf-8")
    assert "for candidate in candidates.into_iter().take(12)" in smoke
    assert 'attempted.push_str(" (no chapters)")' in smoke
    assert 'attempted.push_str(" (update error)")' in smoke
    assert "no readable manga candidate among" in smoke


def test_scaffold_declares_minimum_app_version_for_date_host_import(tmp_path: Path) -> None:
    project, ir = scaffold_project(tmp_path)
    manifest = _manifest()
    manifest.files[0].content += (
        "\nuse aidoku::imports::std::parse_date;\n"
        'fn chapter_date() -> Option<i64> { parse_date("2025-01-01", "yyyy-MM-dd") }\n'
    )
    apply_generation_manifest(project, ir, manifest, query=None)

    assert GeneratedSourceMetadata.load(project).minimum_app_version == "0.7.1"


def test_scaffold_declares_minimum_app_version_for_request_timeout(tmp_path: Path) -> None:
    project, ir = scaffold_project(tmp_path)
    manifest = _manifest()
    manifest.files[0].content += (
        "\nuse aidoku::imports::net::Request;\n"
        'fn image_request() { let _ = Request::get("https://example.com")'
        ".unwrap().timeout(60.0); }\n"
    )
    apply_generation_manifest(project, ir, manifest, query=None)

    assert GeneratedSourceMetadata.load(project).minimum_app_version == "0.8.3"


@pytest.mark.parametrize(
    "token",
    [
        'env!("C2A_API_KEY")',
        'include_str !("/tmp/x")',
        'include_bytes/*x*/!("/tmp/x")',
        "#[cfg/*x*/(test)] mod hidden_test {}",
        '#[path="/tmp/x.rs"] mod external_module;',
        "std/**/::env/**/::vars();",
        "extern crate std;",
        "unsafe {}",
        "unsafe fn hidden() {}",
        "unsafe trait Hidden {}",
        "unsafe impl Hidden for Simple {}",
        "mod generated_smoke {}",
    ],
)
def test_scaffold_rejects_dangerous_generated_rust(tmp_path: Path, token: str) -> None:
    project, ir = scaffold_project(tmp_path)
    manifest = _manifest()
    manifest.files[0].content += f"\n{token}\n"
    with pytest.raises(SecurityError, match="forbidden"):
        apply_generation_manifest(project, ir, manifest, query=None)
