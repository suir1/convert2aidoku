from __future__ import annotations

import hashlib

import pytest

from convert2aidoku.errors import InputError
from convert2aidoku.generation_context import build_generation_context
from convert2aidoku.models import SourceFile, SourceIR, SourceMetadata


def _file(path: str, content: str) -> SourceFile:
    return SourceFile(
        path=path,
        content=content,
        sha256=hashlib.sha256(content.encode()).hexdigest(),
    )


def _ir(*files: SourceFile, source_format: str = "decompiled_apk") -> SourceIR:
    return SourceIR(
        input_ref="fixture",
        source_format=source_format,
        metadata=SourceMetadata(
            source_id="zh.example",
            package_name="example",
            name="Example",
            language="zh",
            base_url="https://example.com",
        ),
        main_class="Example",
        files=list(files),
    )


def test_decompiled_generation_context_keeps_behavior_and_drops_jadx_noise() -> None:
    main = _file(
        "sources/example/Example.java",
        """
        package example;
        import java.util.Objects;
        public final class Example extends HttpSource {
            private final String baseUrl = "https://example.com";
            public Example() { this.client = buildClient(); }
            public Request searchMangaRequest(int page) {
                return GET(baseUrl + "/comics?page=" + page);
            }
            public String toString() { return "generated-noise"; }
            public boolean equals(Object value) { return Objects.equals(this, value); }
            public static final class WhenMappings {
                static { int latest = 1; }
            }
        }
        """,
    )
    interceptor = _file(
        "sources/example/interceptor/UserAgentInterceptor.java",
        """
        package example.interceptor;
        public final class UserAgentInterceptor implements Interceptor {
            private final String fallback = "Mozilla/5.0";
            public Response intercept(Interceptor.Chain chain) {
                return chain.proceed(chain.request().newBuilder()
                    .header("User-Agent", fallback).build());
            }
            static final class GeneratedDto {
                public String component1() { return fallback; }
                public String copy() { return fallback; }
                public String toString() { return fallback; }
            }
        }
        """,
    )
    dto = _file(
        "sources/example/api/dto/Comic.java",
        "package example; public final class Comic { private final String pathWord; }",
    )
    manifest = _file("resources/AndroidManifest.xml", "<manifest package='example'/>")
    ir = _ir(main, interceptor, dto, manifest)

    context = build_generation_context(ir)
    payload = context.as_payload()
    evidence = {item["path"]: item["content"] for item in payload["source_evidence"]}

    assert payload["context_stats"]["mode"] == "decompiled_behavior_evidence"
    assert "searchMangaRequest" in evidence[main.path]
    assert '"/comics?page="' in evidence[main.path]
    assert "WhenMappings" in evidence[main.path]
    assert "generated-noise" not in evidence[main.path]
    assert "component1" not in evidence[interceptor.path]
    assert 'header("User-Agent", fallback)' in evidence[interceptor.path]
    assert "pathWord" in evidence[dto.path]
    assert manifest.path not in evidence
    assert any(
        item["path"] == manifest.path and item["reason"] == "represented_in_source_ir"
        for item in payload["omitted_source_files"]
    )
    assert payload["context_stats"]["evidence_chars"] < payload["context_stats"]["original_chars"]


def test_generation_context_never_silently_truncates_essential_main_source() -> None:
    invalid_main = _file("sources/example/Example.java", "not valid java " * 100)
    ir = _ir(invalid_main)

    with pytest.raises(InputError, match="essential generation evidence exceeds"):
        build_generation_context(ir, max_chars=100)


def test_kotlin_generation_context_preserves_complete_source_files() -> None:
    kotlin = _file(
        "src/example/Example.kt",
        'class Example : HttpSource() { override val baseUrl = "https://example.com" }',
    )
    build = _file("build.gradle.kts", 'ext { lang = "zh" }')
    ir = _ir(build, kotlin, source_format="kotlin_module")

    payload = build_generation_context(ir).as_payload()

    assert payload["context_stats"]["mode"] == "complete_kotlin_source"
    assert payload["source_evidence"] == [
        build.model_dump(mode="json"),
        kotlin.model_dump(mode="json"),
    ]
    assert payload["omitted_source_files"] == []
