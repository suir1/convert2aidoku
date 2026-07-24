from pathlib import Path

import pytest

from convert2aidoku.errors import SecurityError
from convert2aidoku.generated_source_metadata import GeneratedSourceMetadata
from convert2aidoku.models import (
    DependencyRequest,
    GeneratedFile,
    GenerationManifest,
    ImageUrlPolicy,
)
from convert2aidoku.scaffold import (
    apply_generation_manifest,
    normalize_pinned_aidoku_rust,
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


def test_scaffold_optionalizes_unused_decompiled_dto_strings(tmp_path: Path) -> None:
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
}
fn chapter_name(chapter: &ChapterDetail) -> &str { &chapter.name }
""",
        )
    )
    project, ir = scaffold_project(tmp_path)
    ir = ir.model_copy(update={"source_format": "decompiled_apk"})

    apply_generation_manifest(project, ir, manifest, query=None)

    dto = (project / "src" / "dto.rs").read_text(encoding="utf-8")
    assert "group_id: Option<String>" in dto
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
