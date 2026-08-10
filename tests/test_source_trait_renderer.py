from __future__ import annotations

from convert2aidoku.models import (
    Capability,
    GeneratedFile,
    GenerationManifest,
    ImageUrlPolicy,
    RequestHeaderProfile,
)
from convert2aidoku.source_trait_renderer import (
    deterministic_source_shell_available,
    render_source_shell,
    render_source_traits,
    source_trait_ownership,
    with_deterministic_source_shell,
    with_deterministic_source_traits,
)
from tests.scenarios import minimal_source_ir
from tests.test_implementation_ir import (
    _copymanga_filter_specs,
    _copymanga_listing_files,
    _copymanga_page_files,
    _copymanga_page_routes,
)


def _copymanga_ir(**updates: object):
    files = {
        source.path: source for source in [*_copymanga_listing_files(), *_copymanga_page_files()]
    }
    ir = minimal_source_ir(
        source_id="zh.copymanga",
        language="zh",
        source_format="decompiled_apk",
        feature_scope="public_only",
        main_class="CopyManga",
        capabilities=[
            Capability.SEARCH,
            Capability.POPULAR,
            Capability.LATEST,
            Capability.DETAILS,
            Capability.CHAPTERS,
            Capability.PAGES,
            Capability.DEEP_LINKS,
            Capability.DYNAMIC_BASE_URLS,
        ],
        filter_specs=_copymanga_filter_specs(),
        files=list(files.values()),
        chapter_page_routes=_copymanga_page_routes(),
        relative_url_keys=True,
        image_url_policy=ImageUrlPolicy(
            preserve_cover_urls=True,
            chapter_resolution_regex=r"\d+(?=x\.(?:jpg|webp)$)",
        ),
        request_header_profiles=[
            RequestHeaderProfile(
                name="HOT_MANGA_HEADER",
                domains=["api.hot.example", "mapi.hot.example"],
            )
        ],
    )
    return ir.model_copy(update=updates)


def test_renders_proven_base_image_and_deep_link_traits(monkeypatch) -> None:
    monkeypatch.setattr(
        "convert2aidoku.source_trait_renderer.deterministic_search_listing_available",
        lambda _ir: True,
    )
    monkeypatch.setattr(
        "convert2aidoku.source_trait_renderer.deterministic_page_list_available",
        lambda _ir: True,
    )
    ir = _copymanga_ir()

    ownership = source_trait_ownership(ir)
    rendered = render_source_traits(ir, "CopyManga", ownership)

    assert ownership.traits == (
        "BaseUrlProvider",
        "ImageRequestProvider",
        "DeepLinkHandler",
    )
    assert rendered.path == "src/c2a_source_traits.rs"
    assert 'String::from("https://example.com")' in rendered.content
    assert "Request::get(url)?" in rendered.content
    assert 'segments[2] == "chapter"' in rendered.content
    assert 'format!("/comic/{}", segments[1])' in rendered.content
    assert "DeepLinkResult::Chapter" in rendered.content
    assert "DeepLinkResult::Manga" in rendered.content


def test_replaces_ai_trait_implementations_and_rebuilds_lib(monkeypatch) -> None:
    monkeypatch.setattr(
        "convert2aidoku.source_trait_renderer.deterministic_search_listing_available",
        lambda _ir: True,
    )
    monkeypatch.setattr(
        "convert2aidoku.source_trait_renderer.deterministic_page_list_available",
        lambda _ir: True,
    )
    ir = _copymanga_ir()
    manifest = GenerationManifest(
        source_struct="CopyManga",
        implemented_traits=[
            "DeepLinkHandler",
            "ImageRequestProvider",
            "BaseUrlProvider",
            "DeepLinkHandler",
        ],
        files=[
            GeneratedFile(path="src/lib.rs", content="AI lib"),
            GeneratedFile(
                path="src/source.rs",
                content="""
pub struct CopyManga;
impl aidoku::BaseUrlProvider for CopyManga { /* ai base */ }
impl aidoku::ImageRequestProvider for CopyManga { /* ai image */ }
impl aidoku::DeepLinkHandler for CopyManga { /* ai links */ }
""",
            ),
        ],
    )

    effective = with_deterministic_source_traits(ir, manifest)
    files = {generated.path: generated.content for generated in effective.files}

    assert "ai base" not in files["src/source.rs"]
    assert "ai image" not in files["src/source.rs"]
    assert "ai links" not in files["src/source.rs"]
    assert "mod c2a_source_traits;" in files["src/lib.rs"]
    assert effective.implemented_traits == [
        "BaseUrlProvider",
        "ImageRequestProvider",
        "DeepLinkHandler",
    ]


def test_replaces_ai_source_shell_when_all_required_providers_are_proven(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "convert2aidoku.source_trait_renderer.deterministic_search_listing_available",
        lambda _ir: True,
    )
    monkeypatch.setattr(
        "convert2aidoku.source_trait_renderer.deterministic_manga_update_available",
        lambda _ir: True,
    )
    monkeypatch.setattr(
        "convert2aidoku.source_trait_renderer.deterministic_page_list_available",
        lambda _ir: True,
    )
    ir = _copymanga_ir()
    manifest = GenerationManifest(
        source_struct="CopyManga",
        files=[
            GeneratedFile(path="src/lib.rs", content="AI lib"),
            GeneratedFile(path="src/source.rs", content="AI invalid source shell"),
        ],
    )

    assert deterministic_source_shell_available(ir)
    effective = with_deterministic_source_shell(ir, manifest)
    files = {generated.path: generated.content for generated in effective.files}

    assert "AI invalid source shell" not in files["src/source.rs"]
    assert "impl Source for CopyManga" in files["src/source.rs"]
    assert "crate::c2a_listing::get_search_manga_list" in files["src/source.rs"]
    assert "crate::c2a_manga_detail::get_manga_update" in files["src/source.rs"]
    assert "crate::c2a_pages::get_page_list" in files["src/source.rs"]
    assert "mod source;" in files["src/lib.rs"]


def test_source_shell_preserves_unowned_optional_traits(monkeypatch) -> None:
    monkeypatch.setattr(
        "convert2aidoku.source_trait_renderer.deterministic_search_listing_available",
        lambda _ir: True,
    )
    monkeypatch.setattr(
        "convert2aidoku.source_trait_renderer.deterministic_manga_update_available",
        lambda _ir: True,
    )
    monkeypatch.setattr(
        "convert2aidoku.source_trait_renderer.deterministic_page_list_available",
        lambda _ir: True,
    )
    ir = _copymanga_ir()
    manifest = GenerationManifest(
        source_struct="CopyManga",
        implemented_traits=["PageDescriptionProvider"],
        files=[
            GeneratedFile(path="src/lib.rs", content="mod source;"),
            GeneratedFile(path="src/source.rs", content="keep optional implementation"),
        ],
    )

    assert with_deterministic_source_shell(ir, manifest) is manifest
    assert "keep optional implementation" in manifest.files[1].content


def test_source_shell_template_includes_owned_listing_provider() -> None:
    rendered = render_source_shell("CopyManga", listing_provider=True)

    assert "impl aidoku::ListingProvider for CopyManga" in rendered.content


def test_preserves_ai_traits_when_evidence_is_ambiguous() -> None:
    ir = minimal_source_ir(
        capabilities=[Capability.DEEP_LINKS, Capability.IMAGE_HEADERS],
        relative_url_keys=True,
    )
    manifest = GenerationManifest(
        source_struct="Simple",
        implemented_traits=["ImageRequestProvider", "DeepLinkHandler"],
        files=[
            GeneratedFile(path="src/lib.rs", content="mod source;"),
            GeneratedFile(
                path="src/source.rs",
                content="""
pub struct Simple;
impl aidoku::ImageRequestProvider for Simple { /* keep image */ }
impl aidoku::DeepLinkHandler for Simple { /* keep links */ }
""",
            ),
        ],
    )

    effective = with_deterministic_source_traits(ir, manifest)

    assert effective is manifest
    assert "keep image" in effective.files[1].content
    assert "keep links" in effective.files[1].content


def test_image_headers_prevent_direct_image_request_ownership(monkeypatch) -> None:
    monkeypatch.setattr(
        "convert2aidoku.source_trait_renderer.deterministic_search_listing_available",
        lambda _ir: True,
    )
    monkeypatch.setattr(
        "convert2aidoku.source_trait_renderer.deterministic_page_list_available",
        lambda _ir: True,
    )
    ir = _copymanga_ir(capabilities=[*_copymanga_ir().capabilities, Capability.IMAGE_HEADERS])

    assert not source_trait_ownership(ir).image_request
