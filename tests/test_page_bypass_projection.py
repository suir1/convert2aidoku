from convert2aidoku.generation_projection import normalize_generation_manifest
from convert2aidoku.models import GeneratedFile, GenerationManifest, SourceFile
from convert2aidoku.normalization_trace import NormalizationTrace
from tests.scenarios import minimal_source_ir


def test_projects_recovered_last_page_bypass_interceptor() -> None:
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
            app.header("user-agent", "reader_android/1.2.3");
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
    manifest = GenerationManifest(
        source_struct="Example",
        files=[
            GeneratedFile(
                path="src/lib.rs",
                content="""
use aidoku::imports::net::{Request, Response};
const LAST_CHAPTER_MARK: &str = "/last_chapter";
struct Example;
impl Example {
    fn absolute_url(&self, path: &str) -> String {
        if path.starts_with("http") { path.to_string() }
        else { format!("https://www.reader.test{}", path) }
    }
    fn request_with_retry(&self, url: String) -> Result<Response> {
        let request = Request::get(url.clone())?
            .header("User-Agent", "browser")
            .header("referer", "https://www.reader.test/");
        match request.send() {
            Ok(response) => Ok(response),
            Err(_) => Ok(Request::get(url)?.send()?),
        }
    }
}
""",
            )
        ],
    )
    trace = NormalizationTrace()

    normalized = normalize_generation_manifest(ir, manifest, trace=trace)
    rust = normalized.files[0].content

    assert 'path.ends_with("/last_chapter")' in rust
    assert 'strip_suffix("/last_chapter")' in rust
    assert '"app-a.reader.test", "app-b.reader.test"' in rust
    assert '"/readerapp"' in rust
    assert '.header("app-version", "1.2.3")' in rust
    assert '.header("user-agent", "reader_android/1.2.3")' in rust
    assert '.header("User-Agent", "browser")' in rust
    assert "c2a_page_bypass_request(&url)?.send()" in rust
    assert trace.counts["project_recovered_page_bypass"] == 1
    assert normalize_generation_manifest(ir, normalized) == normalized
