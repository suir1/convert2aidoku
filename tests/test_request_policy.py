from convert2aidoku.generation_projection import normalize_generation_manifest
from convert2aidoku.models import (
    GeneratedFile,
    GeneratedResources,
    GenerationManifest,
    SourceFile,
    ValidationResult,
    ValidationStage,
)
from convert2aidoku.normalization_trace import NormalizationTrace
from convert2aidoku.request_policy import RequestPolicy
from tests.scenarios import minimal_source_ir


def test_request_policy_projects_recovered_request_behavior() -> None:
    ir = minimal_source_ir(
        source_format="decompiled_apk",
        files=[
            SourceFile(
                path="HttpSourceRepo.java",
                sha256="0",
                content="""
final class HttpSourceRepo {
    private static final List<String> bypassHosts =
        CollectionsKt.listOf(new String[]{"app-a.reader.test", "app-b.reader.test"});
    public static final String lastChapterMark = "/last_chapter";
}
""",
            ),
            SourceFile(
                path="PageBypassInterceptor.java",
                sha256="1",
                content="""
final class PageBypassInterceptor implements Interceptor {
    public Response intercept(Interceptor.Chain chain) {
        Request request = chain.request();
        if (HttpSourceRepo.INSTANCE.isPageListLink(request.url().toString()) &&
            StringsKt.endsWith(request.url().toString(), HttpSourceRepo.lastChapterMark)) {
            String route = "/readerapp" + StringsKt.removeSuffix(
                request.url().encodedPath(), HttpSourceRepo.lastChapterMark);
            Request.Builder app = request.newBuilder();
            app.header("referer", "https://app.reader.test/");
            app.header("app-version", "1.2.3");
            return chain.proceed(app.url(request.url().newBuilder().host(host).encodedPath(route)
                .build()).build());
        }
        return chain.proceed(request);
    }
}
""",
            ),
        ],
    )
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

    assert 'format!("{}/comic/{}", self.api_domain(), path)' in rust
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


def test_request_policy_joins_only_named_base_providers_to_literal_paths() -> None:
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
    rust = projected[0].content

    assert 'format!("{}/search?q={}", api_domain, name)' in rust
    assert 'format!("{}/api/bzmhq/list?page={}", api_domain, 1)' in rust
    assert 'format!("{}/list/new", self.api_domain())' in rust
    assert 'format!("{}/comic", api_domain)' in rust
    assert 'format!("{}{}", api_domain, name)' in rust
    assert 'format!("{}?q={}", api_domain, name)' in rust
    assert 'format!("{}suffix", name)' in rust
    assert policy.project(projected) == projected


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
