import json
from pathlib import Path

import pytest

from convert2aidoku.errors import SecurityError
from convert2aidoku.models import DependencyRequest, GeneratedFile, GenerationManifest
from convert2aidoku.scaffold import apply_generation_manifest
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
    assert "static filter returned no manga" in smoke
    assert "static filter returned a manga with an empty title" in smoke
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

    source = json.loads((project / "res" / "source.json").read_text(encoding="utf-8"))
    assert source["info"]["minAppVersion"] == "0.7.1"


def test_scaffold_declares_minimum_app_version_for_request_timeout(tmp_path: Path) -> None:
    project, ir = scaffold_project(tmp_path)
    manifest = _manifest()
    manifest.files[0].content += (
        "\nuse aidoku::imports::net::Request;\n"
        'fn image_request() { let _ = Request::get("https://example.com")'
        ".unwrap().timeout(60.0); }\n"
    )
    apply_generation_manifest(project, ir, manifest, query=None)

    source = json.loads((project / "res" / "source.json").read_text(encoding="utf-8"))
    assert source["info"]["minAppVersion"] == "0.8.3"


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
