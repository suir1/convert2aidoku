from pathlib import Path

from convert2aidoku.generation_projection import normalize_generation_manifest
from convert2aidoku.models import (
    GeneratedFile,
    GeneratedResources,
    GenerationManifest,
    PageRequestBypass,
    ValidationResult,
    ValidationStage,
)
from convert2aidoku.normalization_trace import NormalizationTrace
from convert2aidoku.request_policy import RequestPolicy
from convert2aidoku.scaffold import apply_generation_manifest
from tests.scenarios import minimal_source_ir, scaffold_project


def _page_bypass_source_ir():
    return minimal_source_ir(
        source_format="decompiled_apk",
        relative_url_keys=True,
        page_bypass=PageRequestBypass(
            marker="/last_chapter",
            path_prefix="/readerapp",
            hosts=["app-a.reader.test", "app-b.reader.test"],
            headers={
                "referer": "https://app.reader.test/",
                "app-version": "1.2.3",
            },
        ),
    )


def test_request_policy_projects_recovered_request_behavior() -> None:
    ir = _page_bypass_source_ir()
    files = [
        GeneratedFile(
            path="src/lib.rs",
            content="""
use aidoku::imports::net::{Request, Response};
struct Example;
impl Example {
    fn absolute_url(&self, path: &str) -> String {
        if path.starts_with("http") { path.to_string() }
        else { format!("{}comic/{}", self.api_domain(), path) }
    }
    fn request_with_retry(&self, url: String) -> Result<Response> {
        let request = Request::get(url.clone())?.header("User-Agent", "browser");
        request.send()
    }
}
""",
        )
    ]

    projected = RequestPolicy.from_source_ir(ir).project(files)
    rust = projected[0].content

    assert 'format!("{}comic/{}", self.api_domain(), path)' in rust
    assert 'path.ends_with("/last_chapter")' in rust
    assert '"app-a.reader.test", "app-b.reader.test"' in rust
    assert '"/readerapp"' in rust

    assert '.header("app-version", "1.2.3")' in rust
    assert '.header("User-Agent", "browser")' in rust
    assert "c2a_page_bypass_request(&url)?.send()" in rust
    assert RequestPolicy.from_source_ir(ir).project(projected) == projected

    trace = NormalizationTrace()
    normalize_generation_manifest(
        ir,
        GenerationManifest(source_struct="Example", files=files),
        trace=trace,
    )
    assert trace.counts["project_request_policy"] == 1


def test_request_policy_projects_page_bypass_into_free_functions(tmp_path: Path) -> None:
    files = [
        GeneratedFile(
            path="src/lib.rs",
            content="""
use aidoku::imports::net::{Request, Response};
fn absolute_url(path: &str) -> String {
    format!("https://reader.test/{}", path)
}
fn request_once(url: String) -> Result<Response> {
    Request::get(url)?.send()
}
""",
        )
    ]

    projected = RequestPolicy.from_source_ir(_page_bypass_source_ir()).project(files)
    rust = projected[0].content

    assert 'path.ends_with("/last_chapter")' in rust
    assert "c2a_page_bypass_request(&url)?.send()" in rust
    assert "fn c2a_page_bypass_url(path: &str)" in rust
    assert '"app-a.reader.test", "app-b.reader.test"' in rust
    assert '"/readerapp"' in rust

    manifest = GenerationManifest(source_struct="Example", files=files)
    normalized = normalize_generation_manifest(_page_bypass_source_ir(), manifest)
    renormalized = normalize_generation_manifest(_page_bypass_source_ir(), normalized)

    assert renormalized == normalized

    project, ir = scaffold_project(tmp_path, transform=lambda _: _page_bypass_source_ir())
    apply_generation_manifest(project, ir, normalized, query=None)
    materialized = (project / "src/lib.rs").read_text()

    assert 'path.ends_with("/last_chapter")' in materialized


def test_apply_generation_manifest_only_materializes_the_effective_manifest(
    tmp_path: Path,
) -> None:
    files = [
        GeneratedFile(
            path="src/lib.rs",
            content="""
fn absolute_url(path: &str) -> String {
    format!("https://reader.test/{}", path)
}
fn request_once(url: String) -> Result<Response> {
    Request::get(url)?.send()
}
""",
        )
    ]
    raw = GenerationManifest(source_struct="Example", files=files)
    project, ir = scaffold_project(tmp_path, transform=lambda _: _page_bypass_source_ir())

    apply_generation_manifest(project, ir, raw, query=None)

    materialized = (project / "src/lib.rs").read_text()
    assert "c2a_page_bypass_url" not in materialized
    assert 'path.ends_with("/last_chapter")' not in materialized


def test_request_policy_does_not_guess_url_semantics_from_rust_names() -> None:
    content = r"""
fn urls(&self, api_domain: String, name: &str) {
    let search = format!("{}search?q={}", api_domain, name);
    let api = format!("{}api/bzmhq/list?page={}", api_domain, 1);
    let latest = format!("{}list/new", self.api_domain());
    let already_joined = format!("{}/comic", api_domain);
    let dynamic_path = format!("{}{}", api_domain, name);
    let query = format!("{}?q={}", api_domain, name);
    let unrelated = format!("{}suffix", name);
}
"""
    files = [GeneratedFile(path="src/lib.rs", content=content)]
    policy = RequestPolicy.from_source_ir(minimal_source_ir())

    projected = policy.project(files)
    assert projected == files


def test_request_policy_remediates_image_cdn_default_and_rust_fallback_atomically() -> None:
    manifest = GenerationManifest(
        source_struct="Example",
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content="""
fn image_cdn_host() -> Option<String> {
    let pref = match defaults_get::<String>("v1.key.image_cdn_option") {
        Some(value) => value,
        _ => "default".to_string(),
    };
    match pref.as_str() { "default" => None, other => Some(other.to_string()) }
}
""",
            ),
            GeneratedFile(
                path="res/settings.json",
                content="""[
                    {"type":"group","title":"Images","items":[
                        {"type":"select","key":"v1.key.image_cdn_option",
                         "title":"CDN","default":"default",
                         "values":["default","random","static.example.com",
                                   "hk.images-cdn.net","us.images-cdn.net"],
                         "titles":["Default","Random","Static","HK","US"]}
                    ]}
                ]""",
            ),
        ],
    )
    failure = ValidationResult(
        build_ok=True,
        package_ok=True,
        stages=[
            ValidationStage(
                name="core-live-smoke",
                kind="live_test",
                ok=False,
                output="cover image returned HTTP 403",
            )
        ],
    )
    policy = RequestPolicy.from_source_ir(minimal_source_ir())

    remediation = policy.remediate(manifest, failure)

    assert remediation is not None
    assert GeneratedResources(remediation.manifest).setting_defaults() == {
        "v1.key.image_cdn_option": "hk.images-cdn.net"
    }
    assert '_ => String::from("hk.images-cdn.net"),' in remediation.manifest.files[0].content
    assert remediation.warning.endswith("hk.images-cdn.net")

    second = policy.remediate(remediation.manifest, failure)
    assert second is not None
    assert GeneratedResources(second.manifest).setting_defaults() == {
        "v1.key.image_cdn_option": "us.images-cdn.net"
    }
    assert '_ => String::from("us.images-cdn.net"),' in second.manifest.files[0].content
    assert policy.remediate(second.manifest, failure) is None

    server_error = failure.model_copy(
        update={
            "stages": [
                failure.stages[0].model_copy(update={"output": "first image returned HTTP 500"})
            ]
        }
    )
    assert policy.remediate(manifest, server_error) is None


def test_request_policy_remediates_image_request_error_with_unwrap_fallback() -> None:
    key = "v1.key.image_cdn_option"
    manifest = GenerationManifest(
        source_struct="Example",
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content=f'''
fn apply_image_cdn(url: &str) -> String {{
    let cdn = defaults_get::<String>("{key}")
        .unwrap_or_else(|| String::from("default"));
    format!("https://{{}}/{{}}", cdn, url)
}}
''',
            ),
            GeneratedFile(
                path="res/settings.json",
                content=f'''[{{"type":"group","items":[{{
                    "type":"select","key":"{key}","default":"default",
                    "values":["default","random","static.example.com","hk.images-cdn.net"]
                }}]}}]''',
            ),
        ],
    )
    validation = ValidationResult(
        blocked=True,
        stages=[
            ValidationStage(
                name="core-live-smoke",
                kind="live_test",
                ok=False,
                blocked=True,
                output="cover image request failed after retry: RequestError",
            )
        ],
    )

    remediation = RequestPolicy.from_source_ir(minimal_source_ir()).remediate(manifest, validation)

    assert remediation is not None
    assert GeneratedResources(remediation.manifest).setting_defaults()[key] == "hk.images-cdn.net"
    assert 'String::from("hk.images-cdn.net")' in remediation.manifest.files[0].content
