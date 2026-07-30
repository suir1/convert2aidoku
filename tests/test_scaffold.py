import re
from pathlib import Path

import pytest

from convert2aidoku.errors import SecurityError
from convert2aidoku.generated_source_metadata import GeneratedSourceMetadata
from convert2aidoku.models import (
    Capability,
    ChapterPageRoute,
    ChapterPageRouteVariant,
    DependencyRequest,
    GeneratedFile,
    GenerationManifest,
    ImageUrlPolicy,
    RequestHeaderProfile,
    RouteReplacement,
    SourceFile,
    SourceFilterOption,
    SourceFilterSpec,
)
from convert2aidoku.scaffold import (
    apply_generation_manifest,
    normalize_generation_manifest,
    normalize_pinned_aidoku_rust,
    render_generated_lib_rs,
    validate_generated_content,
)
from tests.scenarios import minimal_source_ir, scaffold_project


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


def test_normalizes_owned_string_values_for_pinned_defaults_set() -> None:
    content = """
use aidoku::imports::defaults::{defaults_get, defaults_set};
fn save(received_token: String, response: Response) {
    defaults_set("token", received_token.clone());
    defaults_set("userId", response.user_id.to_string());
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert (
        'defaults_set("token", '
        "aidoku::imports::defaults::DefaultValue::String(received_token.clone()));" in normalized
    )
    assert (
        'defaults_set("userId", '
        "aidoku::imports::defaults::DefaultValue::String(response.user_id.to_string()));"
        in normalized
    )
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_rsa_bootstrap_preserves_server_error_body_and_millisecond_time() -> None:
    content = """
use rsa::Pkcs1v15Encrypt;
fn last_used_time() -> i64 { (current_date() / 1_000) * 1_000 }
fn fetch_token() -> Result<String> {
    let response: AnonymousBody = request.send()?.get_json_owned()?;
    Ok(response.token)
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert "let response_text = request.send()?.get_string()?;" in normalized
    assert "serde_json::from_str(&response_text)" in normalized
    assert "aidoku::AidokuError::message(response_text)" in normalized
    assert "current_date() * 1_000" in normalized
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_smoke_query_uses_valid_rust_unicode_literal(tmp_path: Path) -> None:
    project, ir = scaffold_project(tmp_path)

    apply_generation_manifest(project, ir, _manifest("serde"), query="漫画")

    smoke = (project / "src" / "generated_smoke.rs").read_text(encoding="utf-8")
    assert 'Some("漫画".into())' in smoke
    assert "\\u6f2b" not in smoke


def test_scaffold_rejects_unapproved_dependency(tmp_path: Path) -> None:
    project, ir = scaffold_project(tmp_path)
    with pytest.raises(SecurityError, match="disallowed dependency"):
        apply_generation_manifest(project, ir, _manifest("reqwest"), query=None)


def test_generated_content_allows_grouped_aidoku_std_import_only() -> None:
    validate_generated_content(
        "src/parser.rs",
        "use aidoku::{imports::std::parse_date, alloc::String};",
    )
    validate_generated_content(
        "src/parser.rs",
        "use aidoku::imports::{html::Element, net::Request, std::parse_date};",
    )
    validate_generated_content(
        "src/parser.rs",
        "use aidoku::{imports::{html::Element, std::parse_date}, alloc::String};",
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
use std::error::Error;
use std::time::Duration;
use std::ops::Deref;
use std::marker::PhantomData;
use std::hash::Hasher;
use std::cell::RefCell;
use std::any::Any;
use std::num::NonZeroUsize;
use std::slice::Iter;
use std::rc::Rc;
use std::sync::Arc;
use std::collections::VecDeque;
use std::sync::Mutex;
fn values() -> std::vec::Vec<std::string::String> {
    let _: std::cmp::Ordering = std::cmp::Ordering::Equal;
    HashMap::<String, Cow<'static, str>>::new();
    Vec::new()
}
use std::fs;
use std::net::TcpStream;
use std::process::Command;
"""

    normalized = normalize_pinned_aidoku_rust(content, remove_extern_std=True)

    assert "std::collections" not in normalized
    assert "extern crate std" not in normalized
    assert "BTreeMap::<String" in normalized
    assert "aidoku::alloc::borrow::Cow" in normalized
    assert "aidoku::alloc::vec::Vec" in normalized
    assert "aidoku::alloc::string::String" in normalized
    assert "core::cmp::Ordering" in normalized
    assert "core::error::Error" in normalized
    assert "core::time::Duration" in normalized
    assert "core::ops::Deref" in normalized
    assert "core::marker::PhantomData" in normalized
    assert "core::hash::Hasher" in normalized
    assert "core::cell::RefCell" in normalized
    assert "core::any::Any" in normalized
    assert "core::num::NonZeroUsize" in normalized
    assert "core::slice::Iter" in normalized
    assert "aidoku::alloc::rc::Rc" in normalized
    assert "aidoku::alloc::sync::Arc" in normalized
    assert "aidoku::alloc::collections::VecDeque" in normalized
    assert "use std::sync::Mutex;" in normalized
    assert "use std::fs;" in normalized
    assert "use std::net::TcpStream;" in normalized
    assert "use std::process::Command;" in normalized


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


def test_normalizer_repairs_legacy_dynamic_filter_constructors_and_imports() -> None:
    content = r"""
use aidoku::MangaStatus;
use aidoku::Result;
use aidoku::imports::net::Request;
use aidoku::imports::net::{Request as NetRequest, Request, Response};
use aidoku::{Filter, SelectFilter, SortFilter, SortFilterDefault};

fn status() -> aidoku::MangaStatus { aidoku::MangaStatus::Unknown }
fn request() -> aidoku::Result<Request> { Request::get("https://example.com") }
fn net_request() -> aidoku::Result<Response> { NetRequest::get("https://example.com")?.send() }
fn filters() -> Vec<Filter> {
    let mut filters = Vec::new();
    filters.push(Filter::note(
        "Filters".into(),
    ));
    filters.push(Filter::from(SortFilter {
        id: "sort".into(),
        title: Some("Sort".into()),
        options: vec!["Latest".into()],
        default: Some(SortFilterDefault::DefaultIndex(0)),
        hide_from_header: Some(false),
        can_ascend: true,
    }));
    filters.push(Filter::Select(SelectFilter {
        id: "status".into(),
        title: Some("Status".into()),
        options: vec!["All".into()],
        ids: Some(vec!["".into()]),
        default: Some("".into()),
    }));
    filters
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert "use aidoku::MangaStatus;" not in normalized
    assert "use aidoku::Result;" not in normalized
    assert "use aidoku::imports::net::Request;" not in normalized
    assert "Request as NetRequest, Request, Response" in normalized
    assert "SortFilterDefault::DefaultIndex" not in normalized
    assert "SortFilterDefault { index: 0, ascending: false }" in normalized
    assert "Filter::Select" not in normalized
    assert "Filter::from(SelectFilter {" in normalized
    assert '"Filters".into()' not in normalized
    assert "..Default::default()" in normalized
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_normalizer_adds_missing_sort_filter_default_direction() -> None:
    content = """
fn filter() -> aidoku::SortFilterDefault {
    aidoku::SortFilterDefault { index: 1 }
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert "aidoku::SortFilterDefault { index: 1, ascending: false }" in normalized
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_normalizer_folds_identical_if_branches_without_skipping_condition() -> None:
    content = """
fn chapter_key(info: Info) -> String {
    if info.path.starts_with("/comic/") {
        format!("/comic/{}/chapter/{}", info.path, info.uuid)
    } else {
        format!("/comic/{}/chapter/{}", info.path, info.uuid)
    }
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert 'let _ = info.path.starts_with("/comic/");' in normalized
    assert normalized.count('format!("/comic/{}/chapter/{}", info.path, info.uuid)') == 1
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_normalizer_repairs_dynamic_header_and_option_date_fallback() -> None:
    content = """
fn request(request: Request, cookie: String) -> Request {
    request.header("Cookie", cookie)
}
fn date(value: &str) -> Option<i64> {
    parse_date(value, "yyyy-MM-dd HH:mm:ss")
        .or_else(|_| parse_date(value, "yyyy-MM-dd"))
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert 'request.header("Cookie", &cookie)' in normalized
    assert ".or_else(|| parse_date" in normalized
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_normalizer_folds_default_model_field_assignments() -> None:
    content = """
fn manga(dto: Dto) -> Manga {
    let mut manga = Manga::default();
    manga.key = dto.key;
    manga.title = dto.title;
    manga.status = match dto.status.as_str() {
        "ongoing" => MangaStatus::Ongoing,
        _ => MangaStatus::Unknown,
    };
    manga
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert "let mut manga = Manga::default();" not in normalized
    assert "let manga = Manga {" in normalized
    assert "key: dto.key," in normalized
    assert "title: dto.title," in normalized
    assert "status: match dto.status.as_str()" in normalized
    assert "..Default::default()" in normalized
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_normalizer_restores_graphql_fragment_selection_braces() -> None:
    content = r"""const COMIC_BODY: &str = r#"
    id
    title
"#;
fn build_query(query: &str) -> String {
    query.replace("#{body}", COMIC_BODY)
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert 'const COMIC_BODY: &str = r#"\n{' in normalized
    assert "    id\n    title\n}" in normalized
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_normalizer_keeps_chapters_when_optional_dates_do_not_parse() -> None:
    content = """
fn chapter(value: &str) -> Result<Chapter> {
    let date_seconds = parse_date(value, "yyyy-MM-dd'T'HH:mm:ss'Z'")
        .ok_or_else(|| AidokuError::message("Invalid date".to_string()))?;
    Ok(Chapter {
        key: "chapter".into(),
        date_uploaded: Some(date_seconds),
        ..Default::default()
    })
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert ".ok_or_else" not in normalized
    assert "let date_seconds = parse_date(" in normalized
    assert "date_uploaded: date_seconds" in normalized
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
    assert "authors: Some(" in normalized
    assert ".join(" not in normalized
    assert "cover: Some(c.cover)," in normalized
    assert "description: Some(c.brief)," in normalized
    assert "title: Some(format!" in normalized
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
    assert 'url: Some(absolute_url(&(format!("/comic/{}", c.path_word))))' in normalized
    assert 'url: Some(absolute_url(&(format!("/comic/x/chapter/{}", ci.uuid))))' in normalized
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
    assert "use aidoku::alloc::vec::Vec;" not in normalized
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


def test_normalizer_repairs_crate_root_imports_strings_and_legacy_request_errors() -> None:
    content = """#![allow(dead_code)]
use aidoku::{
    alloc::{Vec, string::ToString},
    imports::net::{Request, RequestError},
    source::{Manga, MangaPageResult, MangaStatus, PageContent, Viewer},
};
fn parse() -> Result<MangaPageResult, RequestError> {
    let _: String = String::new();
    let _: Option<Manga> = None;
    let _ = MangaStatus::Unknown;
    let _ = Viewer::Unknown;
    let _ = PageContent::url("image");
    Err(RequestError::Parse)
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert "source::{" not in normalized
    assert not re.search(r",\s*,", normalized)
    assert "RequestError" not in normalized
    assert "Result<MangaPageResult>" in normalized
    assert "AidokuError::message" in normalized
    assert "use aidoku::alloc::string::String;" in normalized
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_normalizer_repairs_no_std_network_result_defaults_and_string_errors() -> None:
    content = """
use aidoku::{Request, Response};
use aidoku::Result;

fn parse(response: Response) -> Result<Vec<String>, String> {
    let _boxed: Option<Box<String>> = None;
    let domain: String = defaults_get("domain");
    if domain.is_empty() {
        return Err("missing domain".into());
    }
    let value = response.get_json_owned().map_err(|e| format!("json: {}", e))?;
    if value.is_null() {
        return Err(format!("invalid: {}", value));
    }
    Ok(Vec::new())
}

fn tuple_result() -> Result<(String, bool), aidoku::NetworkError> {
    Ok((String::new(), false))
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert "use aidoku::{Request, Response};" not in normalized
    assert "use aidoku::imports::net::Response;" in normalized
    assert "use aidoku::imports::net::Request;" not in normalized
    assert "use aidoku::alloc::vec::Vec;" in normalized
    assert "use aidoku::alloc::boxed::Box;" in normalized
    assert "Result<Vec<String>, String>" not in normalized
    assert "Result<Vec<String>>" in normalized
    assert "Result<(String, bool)>" in normalized
    assert 'defaults_get::<String>("domain").unwrap_or_default()' in normalized
    assert 'Err(aidoku::AidokuError::message("missing domain"))' in normalized
    assert "map_err(|e| format!" not in normalized
    assert 'Err(aidoku::AidokuError::message(format!("invalid: {}", value)))' in normalized
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_normalizer_repairs_legacy_models_filters_pages_and_context() -> None:
    content = """
fn chapter(value: &str, group: &str) -> Result<Chapter> {
    let date = parse_date(
        value,
        "yyyy-MM-dd",
        None,
    )?;
    Ok(Chapter {
        scanlator: Some(group.to_string()),
        date_uploaded: Some(date),
        ..Default::default()
    })
}
fn manga() -> Manga {
    let mut manga = Manga::new();
    manga.initialized = true;
    manga
}
fn pages(image_url: String) -> Vec<Page> {
    let context = serde_json::json!({"referer": "https://example.com".to_string()}).to_string();
    let mut pages = Vec::new();
    pages.push(Page {
        index,
        url: image_url.clone(),
        content: PageContent::url_context(image_url, context),
        ..Default::default()
    });
    pages
}
fn image(context: Option<PageContext>) -> Result<Request> {
    let mut request = Request::get("https://example.com")?;
    if let Some(ctx) = context {
        if let Ok(json) = serde_json::from_str::<serde_json::Value>(&ctx.0) {
            if let Some(referer) = json.get("referer").and_then(|v| v.as_str()) {
                request = request.header("referer", referer);
            }
        }
    }
    Ok(request)
}
fn filters(categories: Vec<Category>) -> Vec<Filter> {
    let mut filters = vec![Filter::Header { title: "Header".into() }];
    for category in categories {
        filters.push(Filter::Checkbox(aidoku::CheckboxFilter {
            id: category.id,
            title: Some(category.name),
            default: false,
        }));
    }
    filters.push(Filter::from(aidoku::SortFilter {
        id: "sort".into(),
        default_index: 0,
        values: vec!["Latest".into()],
        ascending: Some(false),
        ..Default::default()
    }));
    filters.push(Filter::from(aidoku::SelectFilter {
        id: "status".into(),
        default_id: Some("".into()),
        ..Default::default()
    }));
    filters
}
fn selected(filters: &[FilterValue]) -> bool {
    for filter in filters {
        if let FilterValue::Checkbox { id, checked } = filter {
            if id == "category" && *checked { return true; }
        }
    }
    false
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert "Manga::new()" not in normalized
    assert ".initialized" not in normalized
    assert "scanlators: Some(vec![group.to_string()])" in normalized
    assert "let date = parse_date(" in normalized
    assert "None" not in normalized.split("Ok(Chapter", 1)[0]
    assert "date_uploaded: date" in normalized
    assert "Filter::Header" not in normalized
    assert 'Filter::note("Header")' in normalized
    assert "Filter::from(aidoku::CheckFilter" in normalized
    assert "default: Some(false)" in normalized
    assert "id: category.id.into()" in normalized
    assert "title: Some(category.name.into())" in normalized
    assert "options: vec!" in normalized
    assert "aidoku::SortFilterDefault { index: 0, ascending: false }" in normalized
    assert 'default: Some("".into())' in normalized
    assert "FilterValue::Check { id, value }" in normalized
    assert "*value > 0" in normalized
    assert "index," not in normalized
    assert "url: image_url.clone()" not in normalized
    assert "let mut context = PageContext::new();" in normalized
    assert 'context.insert("referer".into()' in normalized
    assert 'ctx.get("referer")' in normalized
    assert "ctx.0" not in normalized
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_normalizer_repairs_local_model_optional_fields_image_request_and_deep_links() -> None:
    content = """
use aidoku::filters::SelectFilter;
use aidoku::imports::net::RequestError;
fn manga(dto: Dto) -> Manga {
    Manga {
        cover: absolute_url(&dto.cover),
        description: dto.description,
        ..Default::default()
    }
}
fn chapter(value: String) -> Chapter {
    Chapter {
        title: format!("{}", value),
        chapter_number: Some((value.parse::<f32>().ok()) as f32),
        ..Default::default()
    }
}
fn parse_chapter_number(value: &str) -> Option<f32> {
    value.parse::<f32>().ok()
}
fn parsed_chapter(title: String) -> Chapter {
    Chapter {
        chapter_number: Some((Self::parse_chapter_number(&title)) as f32),
        ..Default::default()
    }
}
fn page(image_url: String, chapter_url: String) -> Page {
    Page {
        content: PageContent::url_context(image_url, chapter_url.clone()),
        ..Default::default()
    }
}
fn get_image_request(url: String, context: Option<PageContext>) -> Result<Request> {
    let referer = context.unwrap_or_else(|| BASE_URL.into());
    Request::get(&url)?.header("referer", referer).send_error_type::<RequestError>();
    Request::get(&url)?.header("referer", referer).into()
}
fn deep_link() -> DeepLinkResult {
    DeepLinkResult::Manga {
        key: "manga".into(),
        ..Default::default()
    }
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert "aidoku::filters::SelectFilter" not in normalized
    assert "SelectFilter" not in normalized
    assert "cover: Some(absolute_url(&dto.cover))" in normalized
    assert "description: Some(dto.description)" in normalized
    assert 'title: Some(format!("{}", value))' in normalized
    assert "chapter_number: value.parse::<f32>().ok()" in normalized
    assert "chapter_number: Self::parse_chapter_number(&title)" in normalized
    assert 'PageContext::from([("referer".into(), chapter_url.clone())])' in normalized
    assert 'value.get("referer")' in normalized
    assert "send_error_type" not in normalized
    assert 'Ok(Request::get(&url)?.header("referer", &referer))' in normalized
    assert "RequestError" not in normalized
    assert "use aidoku::imports::net::;" not in normalized
    assert "..Default::default()" not in normalized.split("DeepLinkResult::Manga", 1)[1]
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_normalizer_does_not_double_wrap_optional_dto_model_fields() -> None:
    content = """
#[derive(Deserialize)]
struct MangaDto {
    cover_img_url: Option<String>,
    description: Option<String>,
}
fn manga(dto: MangaDto) -> Manga {
    Manga {
        cover: Some(dto.cover_img_url),
        description: Some(dto.description),
        ..Default::default()
    }
}
fn update(manga: &mut Manga, dto: MangaDto) {
    manga.cover = Some(dto.cover_img_url);
    manga.description = Some(dto.description);
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert "cover: dto.cover_img_url" in normalized
    assert "description: dto.description" in normalized
    assert "manga.cover = dto.cover_img_url;" in normalized
    assert "manga.description = dto.description;" in normalized
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_normalizer_repairs_bound_optional_numbers_context_and_request_headers() -> None:
    content = """
fn chapter(serial: &str) -> Chapter {
    let number = serial.parse::<f32>().ok();
    Chapter {
        chapter_number: Some((number) as f32),
        ..Default::default()
    }
}
fn pages(&self, chapter: Chapter, image_url: String) -> Vec<Page> {
    let context = chapter.url.clone();
    vec![Page {
        content: PageContent::url_context(
            image_url,
            PageContext::from([("referer".into(), context.clone())]),
        ),
        ..Default::default()
    }]
}
fn get_image_request(&self, url: String, context: Option<PageContext>) -> Result<Request> {
    let referer = context
        .as_ref()
        .and_then(|value| value.get("referer"))
        .map(String::as_str)
        .unwrap_or(BASE_URL);
    Ok(Request::get(url)?
        .header("accept", "image/*")?
        .header("referer", &referer))
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert "chapter_number: number" in normalized
    assert "Some((number) as f32)" not in normalized
    assert (
        "let context = chapter.url.clone().unwrap_or_else(|| chapter.key.clone());"
    ) in normalized
    assert '.header("accept", "image/*")?' not in normalized
    assert '.header("accept", "image/*")' in normalized
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_normalizer_splits_combined_graphql_manga_update_projection() -> None:
    content = """
fn manga_query(&self, manga_id: &str) -> String {
    let fields = Self::comic_fields();
    format!(
        "query chapterByComicId($comicId: ID!) {{ \
        comicById(comicId: $comicId) {{ {fields} }} \
        chaptersByComicId(comicId: $comicId) {{ id serial }} }}"
    )
}
fn get_manga_update(
    &self,
    manga_id: &str,
    needs_details: bool,
    needs_chapters: bool,
) {
    self.graphql(&self.manga_query(manga_id));
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert (
        "fn manga_query(&self, manga_id: &str, needs_details: bool, needs_chapters: bool)"
        in normalized
    )
    assert "match (needs_details, needs_chapters)" in normalized
    assert '(true, false) => format!("query chapterByComicId' in normalized
    details_arm = normalized.split("(true, false) =>", 1)[1].split("(false, true) =>", 1)[0]
    chapters_arm = normalized.split("(false, true) =>", 1)[1].split("_ =>", 1)[0]
    assert "comicById" in details_arm
    assert "chaptersByComicId" not in details_arm
    assert "comicById" not in chapters_arm
    assert "chaptersByComicId" in chapters_arm
    assert "self.manga_query(manga_id, needs_details, needs_chapters)" in normalized
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_normalizer_repairs_optional_html_element_text_shapes() -> None:
    content = """
fn parse(manga: &mut Manga, detail: Element, link: Element, heading: Element, badge: Element) {
    let ranked = Manga {
        title: link.text(),
        ..Default::default()
    };
    manga.title = heading.text();
    manga.status = match badge.text().as_str() {
        "ongoing" => MangaStatus::Ongoing,
        _ => MangaStatus::Unknown,
    };
    let mut values = Vec::new();
    values.push(link.text());
    manga.authors = Some(alloc::vec!["author".into()]);
    manga.description = detail
        .select("meta")
        .and_then(|items| items.first())
        .map(|element| element.text())
        .unwrap_or_default();
    let date = detail
        .select("time")
        .and_then(|items| items.first())
        .map(|element| element.text())
        .and_then(|value| parse_date(&value, "yyyy-MM-dd"));
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert "title: link.text().unwrap_or_default()" in normalized
    assert "manga.title = heading.text().unwrap_or_default();" in normalized
    assert "badge.text().as_deref().unwrap_or_default()" in normalized
    assert "values.extend(link.text());" in normalized
    assert "alloc::vec!" not in normalized
    assert 'Some(vec!["author".into()])' in normalized
    assert ".and_then(|element| element.text())" in normalized
    assert "manga.description = Some(detail" in normalized
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_normalizer_repairs_consumed_html_text_and_local_option_model_fields() -> None:
    content = """
fn normalized_text(&self, value: String, _separator: &str) -> String { value }
fn parse(&self, item: Element, document: Element) -> Result<Manga> {
    let text = item.text();
    let _letters = text.chars();
    let value = item.text();
    if value == "ongoing" { preserve(); }
    let direct = item
        .text()
        .chars();
    let selected = item.text() == "selected";
    let normalized = self.normalized_text(item.text(), "\\n");
    let cover = document
        .select("img")
        .and_then(|items| items.first())
        .and_then(|image| image.attr("src"))
        .map(|url| absolute_url(&url));
    if selected { return Err(AidokuError::message(item.text())); }
    Ok(Manga {
        cover: Some(cover),
        ..Default::default()
    })
}
fn chapter_number(&self, title: &str) -> Option<f32> { title.parse().ok() }
fn chapter(&self, title: String) -> Chapter {
    Chapter {
        chapter_number: Some((self.chapter_number(&title)) as f32),
        ..Default::default()
    }
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert "let text = item.text().unwrap_or_default();" in normalized
    assert "let value = item.text().unwrap_or_default();" in normalized
    assert re.search(r"item\.text\(\)\.unwrap_or_default\(\)\s*\.chars\(\)", normalized)
    assert 'item.text().as_deref() == Some("selected")' in normalized
    assert 'self.normalized_text(item.text().unwrap_or_default(), "\\n")' in normalized
    assert "AidokuError::message(item.text().unwrap_or_default())" in normalized
    assert "cover: cover" in normalized
    assert "chapter_number: self.chapter_number(&title)" in normalized
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_normalizer_repairs_text_parse_borrowed_text_and_owned_setting_route() -> None:
    content = """
fn page(element: Element, notice: Element, link: Element) -> Manga {
    let number = element.text().parse::<i32>().ok();
    let mut description = String::new();
    description.push_str(&notice.text());
    let title = half_width_digits(&link.text());
    let cover = link
        .attr("src")
        .map(|value| absolute_url(&value))
        .unwrap_or_default();
    Manga { cover: cover, ..Default::default() }
}
fn listing(listing: Listing) -> String {
    let route = if listing.id == "latest" {
        "/top/lastupdate/%d.html"
    } else {
        let saved = defaults_get::<String>("POPULAR_MANGA_DISPLAY")
            .unwrap_or_else(|| String::from("/top/weekvisit/%d.html"));
        match saved.as_str() {
            "/top/monthvisit/%d.html" | "/top/weekvisit/%d.html" => saved.as_str(),
            _ => "/top/weekvisit/%d.html",
        }
    };
    route
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert "element.text().unwrap_or_default().parse::<i32>()" in normalized
    assert "push_str(&notice.text().unwrap_or_default())" in normalized
    assert "half_width_digits(&link.text().unwrap_or_default())" in normalized
    assert "cover: Some(cover)" in normalized
    assert 'String::from("/top/lastupdate/%d.html")' in normalized
    assert "=> saved.clone()," in normalized
    assert '_ => String::from("/top/weekvisit/%d.html"),' in normalized
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_normalizer_slices_strings_only_at_utf8_boundaries() -> None:
    content = """
fn date_from_text(text: &str) -> Option<&str> {
    let bytes = text.as_bytes();
    for start in 0..bytes.len() {
        let candidate = &text[start..];
        if candidate.starts_with("2026-") { return Some(candidate); }
    }
    None
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert "let bytes = text.as_bytes();" not in normalized
    assert "for (start, _) in text.char_indices()" in normalized
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_normalizer_repairs_request_tails_partial_detail_move_and_dynamic_api_base() -> None:
    content = """
const API: &str = "https://api.example.com";
fn api_url(path: &str) -> String {
    let mut url = String::from(API);
    url.push_str(path);
    url
}
fn request(url: String) -> Result<Request> {
    Request::get(url)?.header("Accept", "application/json")
}
fn conditional_request(url: String, hot: bool) -> Result<Request> {
    let request = Request::get(url)?;
    Ok(if hot { request } else { request.header("Region", "0") })
}
fn previously_corrupted_request(url: String, hot: bool) -> Result<Request> {
    let request = Request::get(url)?;
    Ok(if hot { request } else { request = request.header("Region", "0") })
}
fn mutable_request(url: String) -> Result<Request> {
    let mut request = Request::get(url)?;
    request.header("Region", "0");
    Ok(request)
}
fn get_image_request(url: String, _context: Option<PageContext>) -> Result<Request> {
    Request::get(url)?.header("Referer", "https://example.com")
}
fn get_manga_update(
    &self,
    mut manga: Manga,
    needs_details: bool,
    needs_chapters: bool,
) -> Result<Manga> {
    let detail = self.detail(&manga.key)?;
    if needs_details {
        let mut updated = self.comic_to_manga(detail.comic, true);
        updated.chapters = manga.chapters;
        manga = updated;
    }
    if needs_chapters {
        manga.chapters = Some(self.chapters_from_detail(&detail)?);
    }
    Ok(manga)
}
"""

    normalized = normalize_pinned_aidoku_rust(
        content,
        setting_defaults={"v2.pref.api_domain": "mapi.copy20.com"},
    )

    assert 'Ok(Request::get(url)?.header("Accept", "application/json"))' in normalized
    assert 'Ok(Request::get(url)?.header("Referer", "https://example.com"))' in normalized
    assert 'else { request.header("Region", "0") }' in normalized
    assert 'request = request.header("Region", "0") })' not in normalized
    assert 'request = request.header("Region", "0");' in normalized
    assert normalized.index("if needs_chapters") < normalized.index("if needs_details")
    assert 'format!("https://{}", api_domain())' in normalized
    assert 'defaults_get::<String>("v2.pref.api_domain")' in normalized
    assert 'String::from("mapi.copy20.com")' in normalized
    assert (
        normalize_pinned_aidoku_rust(
            normalized,
            setting_defaults={"v2.pref.api_domain": "mapi.copy20.com"},
        )
        == normalized
    )


def test_manifest_projects_recovered_detail_api_envelope() -> None:
    ir = minimal_source_ir(
        source_format="decompiled_apk",
        files=[
            SourceFile(
                path="CopyManga.java",
                sha256="0",
                content=(
                    "Reflection.typeOf(ApiResponse.class, "
                    "KTypeProjection.Companion.invariant("
                    "Reflection.typeOf(ComicDetailResult.class)))"
                ),
            )
        ],
    )
    manifest = GenerationManifest(
        source_struct="CopyManga",
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content="""
struct ApiResponse<T> { results: T }
struct DetailResult;
struct CopyManga;
impl CopyManga {
    fn get_json<T>(&self, url: String) -> Result<T> {
        self.request(url)?.send()?.get_json_owned()
    }
    fn detail(&self, path: &str) -> Result<DetailResult> {
        self.get_json(self.api_url(&format!("/api/v3/comic2/{path}")))
    }
}
""",
            )
        ],
    )

    normalized = normalize_generation_manifest(ir, manifest)
    source = normalized.files[0].content

    assert "let response: ApiResponse<DetailResult>" in source
    assert "Ok(response.results)" in source
    assert normalize_generation_manifest(ir, normalized) == normalized


def test_manifest_aliases_merged_nested_dto_to_recovered_json_field() -> None:
    ir = minimal_source_ir(
        source_format="decompiled_apk",
        files=[
            SourceFile(
                path="api/dto/ComicDetail.java",
                sha256="0",
                content=(
                    "public final class ComicDetail { "
                    "private final Region region; "
                    "private final List<ThemeInfo> theme; }"
                ),
            ),
            SourceFile(
                path="api/dto/Region.java",
                sha256="0",
                content=(
                    "public final class Region { "
                    "private final String display; private final int value; }"
                ),
            ),
            SourceFile(
                path="api/dto/ThemeInfo.java",
                sha256="0",
                content=(
                    "public final class ThemeInfo { "
                    "private final String name; private final String pathWord; }"
                ),
            ),
        ],
    )
    manifest = GenerationManifest(
        source_struct="CopyManga",
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content="""
#[derive(Deserialize)]
struct ComicDetail { region: Option<NamedValue>, theme: Vec<NamedValue> }
#[derive(Deserialize)]
struct NamedValue { name: String }
fn fields(detail: ComicDetail) -> Vec<String> {
    let mut values = detail.theme.into_iter().map(|item| item.name).collect::<Vec<_>>();
    if let Some(region) = detail.region { values.push(region.name); }
    values
}
""",
            )
        ],
    )

    normalized = normalize_generation_manifest(ir, manifest)
    source = normalized.files[0].content

    assert '#[serde(alias = "display")]' in source
    assert 'alias = "pathWord"' not in source
    assert normalize_generation_manifest(ir, normalized) == normalized


def test_manifest_defaults_recovered_nullable_dto_field() -> None:
    ir = minimal_source_ir(
        source_format="decompiled_apk",
        files=[
            SourceFile(
                path="api/dto/ChapterDetail.java",
                sha256="0",
                content="""
                public final class ChapterDetail {
                    private final List<Integer> words;
                    private final List<ContentItem> contents;
                    public List<Page> pages() {
                        List<Integer> order = this.words;
                        if (order == null || order.isEmpty()) { return plain(contents); }
                        return sorted(contents, order);
                    }
                }
                """,
            )
        ],
    )
    manifest = GenerationManifest(
        source_struct="CopyManga",
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content="""
#[derive(Default, Deserialize)]
struct ChapterDetail { contents: Vec<ContentItem>, words: Vec<i32> }
#[derive(Default, Deserialize)]
struct ContentItem { url: String }
fn pages(detail: ChapterDetail) -> usize { detail.contents.len() + detail.words.len() }
""",
            )
        ],
    )

    normalized = normalize_generation_manifest(ir, manifest)
    source = normalized.files[0].content

    words = source.split("words: Vec<i32>", 1)[0]
    assert words.rstrip().endswith("#[serde(default)]")
    assert normalize_generation_manifest(ir, normalized) == normalized


def test_normalizer_repairs_generic_json_optional_fields_and_page_context() -> None:
    content = """
#[derive(Deserialize)]
struct PageResult<T> {
    #[serde(default)]
    list: Vec<T>,
    total: i32,
    limit: i32,
    offset: i32,
}
fn json<T: Deserialize<'static>>(response: Response) -> Result<T> {
    response.get_json_owned()
}
fn chapter(title: String) -> Chapter {
    Chapter { title, chapter_number: Some(1.0), ..Default::default() }
}
fn update(mut manga: Manga, url: String) {
    manga.url = url;
}
fn get_manga_update(
    &self,
    mut manga: Manga,
    needs_details: bool,
    needs_chapters: bool,
) -> Result<Manga> {
    let response: ApiResponse<DetailResult> = self.json(url)?;
    if needs_details {
        let comic = response.results.comic;
        manga = Self::manga(comic);
    }
    if needs_chapters {
        manga.chapters = Some(self.chapters(&response.results)?);
    }
    Ok(manga)
}
fn sort(chapters: &mut Vec<Chapter>) {
    chapters.sort_by(|left, right| right.chapter_number.total_cmp(&left.chapter_number));
}
fn pages(context: String, url: String) -> PageContent {
    PageContent::url_context(url, context.clone())
}
fn get_image_request(
    &self,
    url: String,
    context: Option<PageContext>,
) -> Result<Request> {
    let referer = context
        .map(|value| value.referer)
        .unwrap_or_else(|| WEB_BASE.to_string());
    Ok(Request::get(url)?.header("Referer", &referer))
}
impl PageResult {
    fn has_next(&self) -> bool { self.total >= self.offset + self.limit }
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert '#[serde(bound(deserialize = "T: aidoku::serde::Deserialize<\'de>"))]' in normalized
    assert "fn json<T: for<'de> Deserialize<'de>>" in normalized
    assert "Chapter { title: Some(title), chapter_number: Some(1.0)" in normalized
    assert "manga.url = Some(url);" in normalized
    assert "right.chapter_number.partial_cmp(&left.chapter_number)" in normalized
    assert ".unwrap_or(core::cmp::Ordering::Equal)" in normalized
    assert 'PageContext::from([("referer".into(), context.clone())])' in normalized
    assert 'context.as_ref().and_then(|value| value.get("referer")).cloned()' in normalized
    assert "impl<T> PageResult<T>" in normalized
    assert normalized.count("fn has_next(&self)") == 1
    assert normalized.index("if needs_chapters") < normalized.index("if needs_details")
    assert "let c2a_chapters = manga.chapters;" in normalized
    assert "manga.chapters = c2a_chapters;" in normalized
    assert normalize_pinned_aidoku_rust(normalized) == normalized


def test_manifest_projects_domain_dependent_chapter_page_variant() -> None:
    ir = minimal_source_ir(
        chapter_page_routes=[
            ChapterPageRoute(
                source_method="chapterContentDetailUrl(fixChapterId(chapter.url))",
                chapter_key_template="/comic/{comic_path}/chapter/{chapter_id}",
                endpoint_template="/api/v3/comic/{normalized_chapter_key}",
                variants=[
                    ChapterPageRouteVariant(
                        name="default",
                        condition="selected API domain is not HotManga",
                        is_default=True,
                        strip_prefix="/comic/",
                        replacements=[RouteReplacement(old="/chapter/", new="/chapter2/")],
                    ),
                    ChapterPageRouteVariant(
                        name="hot_manga",
                        condition="selected API domain is HotManga",
                        strip_prefix="/comic/",
                    ),
                ],
            )
        ],
        request_header_profiles=[
            RequestHeaderProfile(
                name="HOT_MANGA_HEADER",
                domains=["api.manga2025.com", "mapi.hotmangasf.com"],
                headers={"Webp": "1"},
            )
        ],
    )
    manifest = GenerationManifest(
        source_struct="CopyManga",
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content="""
fn api_domain() -> String { String::from("api.manga2025.com") }
fn page_url(&self, key: &str) -> String {
    let chapter_key = key
        .strip_prefix("/comic/")
        .unwrap_or(key);
    let normalized = chapter_key.replace("/chapter/", "/chapter2/");
    format!("/api/v3/comic/{normalized}")
}
""",
            )
        ],
    )

    normalized = normalize_generation_manifest(ir, manifest)
    source = normalized.files[0].content

    assert "c2a_is_hot_manga_domain(&api_domain())" in source
    assert '"api.manga2025.com" | "mapi.hotmangasf.com"' in source
    assert "aidoku::alloc::String::from(c2a_chapter_key)" in source
    assert 'c2a_chapter_key.replace("/chapter/", "/chapter2/")' in source
    assert source.index("let chapter_key") < source.index("let c2a_chapter_key")
    assert source.index("let c2a_chapter_key") < source.index("let normalized")
    assert normalize_generation_manifest(ir, normalized) == normalized


def test_manifest_projects_missing_chapter_image_resolution_transform() -> None:
    ir = minimal_source_ir(
        image_url_policy=ImageUrlPolicy(
            preserve_cover_urls=True,
            chapter_resolution_regex=r"\d+(?=x\.(?:jpg|webp)$)",
        )
    )
    manifest = GenerationManifest(
        source_struct="CopyManga",
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content="""
fn pages(url: String, context: PageContext) -> PageContent {
    PageContent::url_context(url, context)
}
""",
            ),
            GeneratedFile(
                path="res/settings.json",
                content="""[
                    {"type":"group","items":[
                        {"type":"select","key":"v2.pref.resolution",
                         "values":["resolution.r800","resolution.r1500"],
                         "titles":["800","1500"],"default":"resolution.r1500"}
                    ]}
                ]""",
            ),
        ],
    )

    normalized = normalize_generation_manifest(ir, manifest)
    source = next(file.content for file in normalized.files if file.path == "src/lib.rs")

    assert "c2a_translate_chapter_resolution(url)" in source
    assert "defaults_get::<aidoku::alloc::String>" in source
    assert '"v2.pref.resolution"' in source
    assert 'Some("resolution.r800") => "800"' in source
    assert 'Some("resolution.r1500") => "1500"' in source
    assert 'url.ends_with(".jpg")' in source
    assert 'url.ends_with(".webp")' in source
    assert normalize_generation_manifest(ir, normalized) == normalized


def test_manifest_projects_recovered_rank_item_wrapper() -> None:
    ir = minimal_source_ir(
        source_format="decompiled_apk",
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
        ],
    )
    manifest = GenerationManifest(
        source_struct="CopyManga",
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content="""
#[derive(Deserialize)]
struct ApiResponse<T> { results: T }
#[derive(Deserialize)]
struct PageResult<T> { list: Vec<T>, limit: i32, offset: i32, total: i32 }
#[derive(Deserialize)]
struct Comic { name: String }
impl CopyManga {
    fn get_search_manga_list(&self, rank: bool, url: String) -> Result<MangaPageResult> {
        if rank { let _url = "/api/v3/ranks?type=1"; }
        let response: ApiResponse<PageResult<Comic>> = self.json(url)?;
        let has_next_page = Self::has_next(&response.results);
        Ok(MangaPageResult {
            entries: response.results.list.into_iter().map(Self::manga).collect(),
            has_next_page,
        })
    }
}
""",
            )
        ],
    )

    normalized = normalize_generation_manifest(ir, manifest)
    source = normalized.files[0].content

    assert "struct C2aRankItem" in source
    assert "struct C2aRankResult" in source
    assert 'if url.contains("/ranks")' in source
    assert "ApiResponse<C2aRankResult>" in source
    assert ".map(|item| Self::manga(item.comic))" in source
    assert normalize_generation_manifest(ir, normalized) == normalized


def test_manifest_projects_recovered_named_rank_list_wrapper() -> None:
    ir = minimal_source_ir(
        source_format="decompiled_apk",
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
        ],
    )
    manifest = GenerationManifest(
        source_struct="CopyManga",
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content="""
#[derive(Deserialize)]
struct ApiResponse<T> { results: T }
#[derive(Deserialize)]
struct ComicList { list: Vec<Comic>, limit: i32, offset: i32, total: i32 }
#[derive(Deserialize)]
struct Comic { name: String }
impl CopyManga {
    fn manga_from_comic(comic: Comic) -> Manga { Manga::default() }
    fn page_result(&self, result: ComicList) -> MangaPageResult {
        MangaPageResult {
            entries: result.list.into_iter().map(Self::manga_from_comic).collect(),
            has_next_page: false,
        }
    }
    fn get_search_manga_list(&self, rank: bool, url: String) -> Result<MangaPageResult> {
        if rank { let _url = "/api/v3/ranks?type=1"; }
        let response: ApiResponse<ComicList> = self.get_json(url)?;
        Ok(self.page_result(response.results))
    }
}
""",
            )
        ],
    )

    normalized = normalize_generation_manifest(ir, manifest)
    source = normalized.files[0].content

    assert "ApiResponse<C2aRankResult>" in source
    assert ".map(|item| Self::manga_from_comic(item.comic))" in source
    assert normalize_generation_manifest(ir, normalized) == normalized


def test_manifest_normalizer_repairs_cross_file_module_topology(tmp_path: Path) -> None:
    _project, ir = scaffold_project(tmp_path)
    manifest = GenerationManifest(
        source_struct="Simple",
        files=[
            GeneratedFile(path="src/lib.rs", content="#![no_std]\n"),
            GeneratedFile(
                path="src/source.rs",
                content=(
                    "#![allow(dead_code)]\n"
                    "mod parser;\nmod paths;\nuse parser::*;\nuse paths::*;\n"
                    "fn resolution() -> i32 { 1 }\nfn api_domain() -> i32 { 2 }\n"
                    "fn endpoint() -> &'static str { API_URL }\n"
                    "register_source!(Simple);\n"
                ),
            ),
            GeneratedFile(
                path="src/parser.rs",
                content="use crate::resolution;\nfn value() -> i32 { resolution() }\n",
            ),
            GeneratedFile(
                path="src/paths.rs",
                content="use crate::api_domain;\nfn value() -> i32 { api_domain() }\n",
            ),
            GeneratedFile(
                path="src/query.rs",
                content='const API_URL: &str = "https://example.com";\n',
            ),
        ],
    )

    normalized = normalize_generation_manifest(ir, manifest)
    files = {item.path: item.content for item in normalized.files}

    assert "mod parser;" not in files["src/source.rs"]
    assert "mod paths;" not in files["src/source.rs"]
    assert "use crate::parser::*;" in files["src/source.rs"]
    assert "use crate::paths::*;" in files["src/source.rs"]
    assert "use crate::source::resolution;" in files["src/parser.rs"]
    assert "use crate::source::api_domain;" in files["src/paths.rs"]
    assert "pub(crate) fn resolution" in files["src/source.rs"]
    assert "pub(crate) fn api_domain" in files["src/source.rs"]
    assert "register_source!" not in files["src/source.rs"]
    assert "\n;\n" not in files["src/source.rs"]
    assert "use crate::query::API_URL;" in files["src/source.rs"]
    assert files["src/source.rs"].startswith("#![allow(dead_code)]")
    assert "pub(crate) const API_URL" in files["src/query.rs"]


def test_manifest_projects_recovered_static_filters_into_dynamic_provider(tmp_path: Path) -> None:
    _project, ir = scaffold_project(tmp_path)
    ir = ir.model_copy(
        update={
            "filter_specs": [
                SourceFilterSpec(
                    source_class="StatusFilter",
                    id="status",
                    title="Status",
                    kind="select",
                    options=[
                        SourceFilterOption(title="All", value=""),
                        SourceFilterOption(title="Completed", value="END"),
                    ],
                )
            ]
        }
    )
    manifest = GenerationManifest(
        source_struct="Simple",
        implemented_traits=["DynamicFilters"],
        files=[
            GeneratedFile(path="src/lib.rs", content="#![no_std]\n"),
            GeneratedFile(
                path="src/source.rs",
                content="""
fn get_dynamic_filters(&self) -> Result<Vec<Filter>> {
    Ok(vec![aidoku::SelectFilter {
        id: "category".into(),
        options: vec!["Action".into()],
        ids: Some(vec!["action".into()]),
        ..Default::default()
    }.into()])
}
""",
            ),
        ],
    )

    normalized = normalize_generation_manifest(ir, manifest)
    source = next(item.content for item in normalized.files if item.path == "src/source.rs")

    assert 'id: "category".into()' in source
    assert 'id: "status".into()' in source
    assert 'ids: Some(aidoku::alloc::vec!["".into(), "END".into()])' in source
    assert "let mut c2a_filters" in source
    assert normalize_generation_manifest(ir, normalized) == normalized


def test_manifest_projects_recovered_check_filter_lookup(tmp_path: Path) -> None:
    _project, ir = scaffold_project(tmp_path)
    ir = ir.model_copy(
        update={
            "filter_specs": [
                SourceFilterSpec(
                    source_class="SearchCategoryToggle",
                    id="search_category_toggle",
                    title="Search as category",
                    kind="check",
                )
            ]
        }
    )
    manifest = GenerationManifest(
        source_struct="Simple",
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content="""
fn search_value(filters: &[FilterValue], id: &str) -> Option<String> {
    filters.iter().find_map(|filter| match filter {
        FilterValue::Text { id: filter_id, value } if filter_id == id => Some(value.clone()),
        _ => None,
    })
}
fn search(filters: &[FilterValue]) -> bool {
    Self::search_value(filters, "search_category_toggle").is_some()
}
""",
            )
        ],
    )

    normalized = normalize_generation_manifest(ir, manifest)
    source = normalized.files[0].content

    assert 'c2a_check_value(filters, "search_category_toggle")' in source
    assert "aidoku::FilterValue::Check" in source
    assert normalize_generation_manifest(ir, normalized) == normalized


def test_manifest_synthesizes_recovered_dynamic_filter_provider(tmp_path: Path) -> None:
    _project, ir = scaffold_project(tmp_path)
    ir = ir.model_copy(
        update={
            "capabilities": [*ir.capabilities, Capability.DYNAMIC_FILTERS],
            "files": [
                SourceFile(
                    path="ApiRepo.java",
                    sha256="0",
                    content="""
                    public final String tagList() {
                        return getApiUrl() + "/theme/comic/count?limit=100";
                    }
                    """,
                )
            ],
        }
    )
    manifest = GenerationManifest(
        source_struct="Simple",
        files=[
            GeneratedFile(path="src/lib.rs", content="#![no_std]\n"),
            GeneratedFile(
                path="src/source.rs",
                content="""
struct Simple;
#[derive(Deserialize)]
struct ApiResponse<T> { results: T }
impl Simple {
    fn api_url(path: &str) -> String { path.into() }
    fn get_json<T: for<'de> Deserialize<'de>>(&self, url: String) -> Result<T> {
        self.request(url)?.send()?.get_json_owned()
    }
    fn route(&self) -> String { Self::api_url("/api/v3/comics") }
    fn selected_value<'a>(filters: &'a [FilterValue], id: &str) -> Option<&'a str> {
        None
    }
    fn get_search_manga_list(&self, filters: Vec<FilterValue>) -> Result<MangaPageResult> {
        let region = Self::selected_value(&filters, "region").unwrap_or("");
        let mut url = Self::api_url("/api/v3/comics?limit=21");
        if !region.is_empty() { url.push_str(&format!("&top={region}")); }
        url.push_str("&_update=true");
        self.list(url)
    }
}
""",
            ),
        ],
    )

    normalized = normalize_generation_manifest(ir, manifest)
    source = next(item.content for item in normalized.files if item.path == "src/source.rs")

    assert "DynamicFilters" in normalized.implemented_traits
    assert "impl DynamicFilters for Simple" in source
    assert 'Self::api_url("/api/v3/theme/comic/count?limit=100")' in source
    assert "struct C2aDynamicFilterResult" in source
    assert 'id: "theme".into()' in source
    assert 'Self::selected_value(&filters, "theme").unwrap_or("")' in source
    assert 'url.push_str(&format!("&theme={}", c2a_theme))' in source
    assert normalize_generation_manifest(ir, normalized) == normalized


def test_normalizer_repairs_raw_json_listing_and_consuming_builder_patterns() -> None:
    content = """
fn post(query: &str, variables: serde_json::Value) -> Result<Response> {
    let mut body = PageContext::new();
    body.insert("query".into(), query, "variables": variables);
    let response = Request::post(API_URL, body)?
        .header("Content-Type", "application/json")
        .send()?;
    Ok(response)
}
fn raw(response: Response) -> Result<String> {
    let json = response.get_json_owned()?;
    parse_search(&json)?;
    Ok(json)
}
fn listing(listing: Listing, page: i32) -> String {
    match listing.kind {
        ListingKind::Popular => popular_url(page),
        ListingKind::Latest => latest_url(page),
    }
}
fn image(url: String) -> Result<Request> {
    let mut req = Request::get(url)?;
    req.header("Referer", "https://example.com");
    Ok(req)
}
fn page(url: String) -> PageContent { PageContent::Url(url) }
fn retry(url: &str) -> Result<Response> {
    match Request::get(url)?.send() {
        Ok(response) => Ok(response),
        Err(_) => Request::get(url)?.send(),
    }
}
fn get_base_url(&self) -> String {
    api_domain()
}
fn manga(url: String, status: MangaStatus) -> Manga {
    Manga { url: url, status: Some(status), ..Default::default() }
}
fn chapter(url: String, number: f64) -> Chapter {
    Chapter { url: url, chapter_number: number, ..Default::default() }
}
fn result(has_next: bool) -> MangaPageResult {
    MangaPageResult {
        entries: Vec::new(),
        has_next,
    }
}
fn update(mut manga: Manga, comic: Comic) {
    let m = comic.to_manga();
    manga.description = Some(m.description);
    manga.cover = Some(m.cover);
}
fn moved(resp: ApiResponse<ListResult>) -> MangaPageResult {
    let list: Vec<_> = resp
        .results
        .list
        .into_iter()
        .map(to_manga)
        .collect();
    MangaPageResult { entries: list, has_next_page: resp.results.has_next() }
}
fn consume(chapters: Vec<Chapter>) -> usize {
    for chapter in chapters { use_chapter(chapter); }
    chapters.len()
}
fn orphan_move(resp: ApiResponse<ListResult>) -> Vec<Manga> {
    let orphan: Vec<_> = resp.results.list.into_iter().map(to_manga).collect();
    orphan
}
fn orphan_page(resp: ApiResponse<ListResult>) -> MangaPageResult {
    MangaPageResult {
        entries: Vec::new(),
        has_next_page: resp.results.has_next(),
    }
}
fn url2comic_path(url: &str) -> String {
    let after = url
        .split("/comic/")
        .nth(1)
        .unwrap_or(url)
        .split("/comic2/")
        .nth(1)
        .unwrap_or(url);
    after.to_string()
}
"""

    normalized = normalize_pinned_aidoku_rust(content)

    assert 'let body = serde_json::json!({ "query": query, "variables": variables });' in normalized
    assert "Request::post(API_URL)?.body(body.to_string().as_bytes())" in normalized
    assert "let json = response.get_string()?;" in normalized
    assert "match listing.id.as_str()" in normalized
    assert '"popular" => popular_url(page)' in normalized
    assert '"latest" => latest_url(page)' in normalized
    assert 'req = req.header("Referer", "https://example.com");' in normalized
    assert "PageContent::url(url)" in normalized
    assert "Err(_) => Ok(Request::get(url)?.send()?)," in normalized
    assert "fn get_base_url(&self) -> Result<String>" in normalized
    assert "Ok(api_domain())" in normalized
    assert "url: Some(url)" in normalized
    assert "status: status" in normalized
    assert "chapter_number: Some((number) as f32)" in normalized
    assert "has_next_page: has_next" in normalized
    assert "manga.description = m.description;" in normalized
    assert "manga.cover = m.cover;" in normalized
    assert "let list_has_next = resp.results.has_next();" in normalized
    assert "has_next_page: list_has_next" in normalized
    assert "let chapters_len = chapters.len();" in normalized
    assert "chapters_len\n}" in normalized
    assert "let orphan_has_next" not in normalized
    assert 'url.split_once("/comic/")' in normalized
    assert 'url.split_once("/comic2/")' in normalized
    assert '.split("/comic2/")' not in normalized


def test_manifest_projects_recovered_request_header_profiles(tmp_path: Path) -> None:
    _project, ir = scaffold_project(tmp_path)
    ir = ir.model_copy(
        update={
            "source_format": "decompiled_apk",
            "header_names": ["Accept", "Origin", "User-Agent", "platform"],
            "request_header_profiles": [
                RequestHeaderProfile(
                    name="COPY_HEADER",
                    domains=["api.copy.example"],
                    headers={"Accept": "application/json", "Origin": "https://copy.example"},
                ),
                RequestHeaderProfile(
                    name="HOT_HEADER",
                    domains=["api.hot.example"],
                    headers={"Accept": "application/json", "Webp": "1"},
                ),
            ],
            "shared_request_headers": {"sec-fetch-mode": "navigate"},
        }
    )
    manifest = GenerationManifest(
        source_struct="Simple",
        files=[
            GeneratedFile(path="src/lib.rs", content="#![no_std]\n"),
            GeneratedFile(
                path="src/source.rs",
                content=(
                    "fn send_get_retry(url: &str) -> Result<Response> {\n"
                    "    let request = Request::get(url)?;\n"
                    "    Ok(request.send()?)\n"
                    "}\n"
                ),
            ),
            GeneratedFile(
                path="res/settings.json",
                content="""[{"type":"group","title":"Request","items":[
                    {"type":"select","key":"v2.pref.api_domain","title":"Domain",
                     "values":["api.copy.example","api.hot.example"],
                     "default":"api.copy.example"},
                    {"type":"select","key":"v2.pref.platform","title":"Platform",
                     "values":["platform.none","platform.one"],"default":"platform.one"},
                    {"type":"text","key":"v2.key.user_agent","title":"UA","default":""}
                ]}]""",
            ),
        ],
    )

    normalized = normalize_generation_manifest(ir, manifest)
    source = next(item.content for item in normalized.files if item.path == "src/source.rs")

    assert 'url.contains("api.hot.example")' in source
    assert 'request.header("Origin", "https://copy.example")' in source
    assert 'request.header("Webp", "1")' in source
    assert 'request.header("sec-fetch-mode", "navigate")' in source
    assert 'defaults_get::<aidoku::alloc::String>("v2.pref.platform")' in source
    assert 'request.header("platform", &platform)' in source
    assert 'defaults_get::<aidoku::alloc::String>("v2.key.user_agent")' in source
    assert source.count("c2a_request(url)?.send()") == 2
    assert normalize_generation_manifest(ir, normalized) == normalized


def test_manifest_projects_shared_headers_into_existing_request_builder() -> None:
    ir = minimal_source_ir(
        shared_request_headers={
            "Accept-Language": "zh",
            "User-Agent": "Mozilla/5.0 Test Browser",
        }
    )
    manifest = GenerationManifest(
        source_struct="Simple",
        files=[
            GeneratedFile(path="src/lib.rs", content="#![no_std]\n"),
            GeneratedFile(
                path="src/source.rs",
                content=(
                    "fn request(url: String) -> Result<Request> {\n"
                    '    Ok(Request::get(url)?.header("Accept-Language", "zh"))\n'
                    "}\n"
                    "fn fetch(url: String) -> Result<Response> {\n"
                    "    Request::get(url)?.send()\n"
                    "}\n"
                ),
            ),
        ],
    )

    normalized = normalize_generation_manifest(ir, manifest)
    source = next(item.content for item in normalized.files if item.path == "src/source.rs")

    assert source.count('.header("Accept-Language", "zh")') == 2
    assert source.count('.header("User-Agent", "Mozilla/5.0 Test Browser")') == 2
    assert normalize_generation_manifest(ir, normalized) == normalized


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
const API_DOMAIN_KEY: &str = "v2.pref.http_api_domain";
const DEFAULT_API_DOMAIN: &str = "stale-default.example";
fn api_domain() -> String {
    defaults_get::<String>("api_domain").unwrap_or_else(|| "stale.example".into())
}
fn constant_api_domain() -> String {
    defaults_get::<String>(API_DOMAIN_KEY).unwrap_or_else(|| DEFAULT_API_DOMAIN.into())
}
fn validated_api_domain() -> String {
    let selected = defaults_get::<String>(API_DOMAIN_KEY)
        .unwrap_or_else(|| "stale-selected.example".to_string());
    match selected.as_str() {
        "mapi.copy20.com" => selected,
        _ => "stale-match.example".to_string(),
    }
}
fn resolution() -> String {
    defaults_get("v2.pref.resolution").unwrap_or_else(|| "1500".into())
}
fn resolution_already_mapped() -> String {
    defaults_get::<String>("v2.pref.resolution")
        .map(|value| match value.as_str() {
            "resolution.r800" => "800".to_string(),
            _ => "1500".to_string(),
        })
        .unwrap_or_else(|| "1500".to_string())
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
    assert '"v2.pref.http_api_domain"' not in lib
    assert 'const API_DOMAIN_KEY: &str = "v2.pref.api_domain";' in lib
    assert 'const DEFAULT_API_DOMAIN: &str = "mapi.copy20.com";' in lib
    assert 'defaults_get::<String>("v2.pref.api_domain")' in lib
    assert 'String::from("mapi.copy20.com")' in lib
    assert "stale-default.example" not in lib
    assert "stale-selected.example" not in lib
    assert "stale-match.example" not in lib
    assert 'Some("resolution.r800") => String::from("800")' in lib
    assert 'Some("resolution.r1500") => String::from("1500")' in lib
    assert '_ => String::from("1500")' in lib
    mapped = lib.split("fn resolution_already_mapped", 1)[1]
    assert 'String::from("mapi.copy20.com")' not in mapped
    assert '.unwrap_or_else(|| "1500".to_string())' in mapped


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
fn to_manga_direct(&self, resolution: &str) -> Manga {
    let cover = translate_resolution(&self.cover, resolution);
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
    assert "let cover = self.cover.clone();" in lib


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


def test_normalizer_adds_local_absolute_urls_without_touching_deep_link_variants() -> None:
    content = """
fn manga(key: String) -> Manga {
    Manga { key, title: "Title".into(), ..Default::default() }
}
fn deep_link(key: String) -> DeepLinkResult {
    DeepLinkResult::Manga { key, url: None }
}
"""

    normalized = normalize_pinned_aidoku_rust(
        content,
        public_base_url="https://example.com",
    )

    assert "fn absolute_url(relative: &str) -> String" in normalized
    assert "key: key.clone(), url: Some(absolute_url(&(key)))" in normalized
    assert "DeepLinkResult::Manga { key, url: None }" in normalized
    assert (
        normalize_pinned_aidoku_rust(
            normalized,
            public_base_url="https://example.com",
        )
        == normalized
    )


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


def test_chapter_key_template_preserves_and_restores_prefix_guard() -> None:
    correct = """
fn chapter_key(info: Info) -> String {
    if info.comic_path_word.starts_with("/comic/") {
        format!("{}/chapter/{}", info.comic_path_word, info.uuid)
    } else {
        format!("/comic/{}/chapter/{}", info.comic_path_word, info.uuid)
    }
}
"""
    stale = correct.replace(
        'format!("{}/chapter/{}", info.comic_path_word, info.uuid)',
        'format!("/comic/{}/chapter/{}", info.comic_path_word, info.uuid)',
    )
    options = {"chapter_key_templates": ("/comic/{comic_path}/chapter/{chapter_id}",)}

    normalized = normalize_pinned_aidoku_rust(correct, **options)
    restored = normalize_pinned_aidoku_rust(stale, **options)

    assert 'format!("{}/chapter/{}", info.comic_path_word, info.uuid)' in normalized
    assert restored == normalized
    assert normalize_pinned_aidoku_rust(normalized, **options) == normalized


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
    missing: MissingType,
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
    assert dto.count("#[serde(skip_deserializing)]") == 5
    assert "group_id: Option<String>" in dto
    assert "region: Option<Region>" in dto
    assert "next: Option<String>" in dto
    assert "missing: Option<serde_json::Value>" in dto
    assert "name: String" in dto


@pytest.mark.parametrize(
    ("dependency", "version"),
    [
        ("aes", "0.8.4"),
        ("des", "0.8.1"),
        ("cbc", "0.1.2"),
        ("hex", "0.4.3"),
        ("md-5", "0.10.6"),
        ("rand_chacha", "0.3.1"),
        ("rand_core", "0.6.4"),
        ("rsa", "0.9.8"),
    ],
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
