from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .implementation_ir import project_implementation_ir
from .listing_renderer import deterministic_search_listing_available
from .manga_detail_renderer import deterministic_manga_update_available
from .models import Capability, GeneratedFile, GenerationManifest, SourceIR
from .page_renderer import deterministic_page_list_available
from .rust_inspection import RustInspection
from .scaffold import _environment, render_generated_lib_rs

_PLACEHOLDER = re.compile(r"^\{[A-Za-z_][A-Za-z0-9_]*\}$")
_LITERAL_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]+$")
_SOURCE_SHELL_TRAITS = frozenset(
    {
        "BaseUrlProvider",
        "DeepLinkHandler",
        "ImageRequestProvider",
        "ListingProvider",
    }
)


@dataclass(frozen=True)
class _RouteTemplate:
    segments: tuple[str | None, ...]

    @classmethod
    def parse(cls, template: str | None) -> _RouteTemplate | None:
        if not template or not template.startswith("/"):
            return None
        raw_segments = template.removeprefix("/").split("/")
        if not raw_segments or any(not segment for segment in raw_segments):
            return None
        segments: list[str | None] = []
        for segment in raw_segments:
            if _PLACEHOLDER.fullmatch(segment):
                segments.append(None)
            elif _LITERAL_SEGMENT.fullmatch(segment):
                segments.append(segment)
            else:
                return None
        return cls(tuple(segments))

    def structurally_prefixes(self, other: _RouteTemplate) -> bool:
        if len(self.segments) >= len(other.segments):
            return False
        return all(
            left is None and right is None or left is not None and left == right
            for left, right in zip(self.segments, other.segments, strict=False)
        )

    @property
    def format_string(self) -> str:
        return "/" + "/".join("{}" if segment is None else segment for segment in self.segments)

    @property
    def placeholder_indices(self) -> tuple[int, ...]:
        return tuple(index for index, segment in enumerate(self.segments) if segment is None)


@dataclass(frozen=True)
class DeepLinkOwnership:
    manga: _RouteTemplate
    chapter: _RouteTemplate


@dataclass(frozen=True)
class SourceTraitOwnership:
    base_url: bool = False
    image_request: bool = False
    deep_links: DeepLinkOwnership | None = None

    @property
    def traits(self) -> tuple[str, ...]:
        traits: list[str] = []
        if self.base_url:
            traits.append("BaseUrlProvider")
        if self.image_request:
            traits.append("ImageRequestProvider")
        if self.deep_links is not None:
            traits.append("DeepLinkHandler")
        return tuple(traits)

    @property
    def java_methods(self) -> frozenset[str]:
        methods: set[str] = set()
        if self.base_url:
            methods.add("getBaseUrl")
        if self.image_request:
            methods.add("imageUrlParse")
        if self.deep_links is not None:
            methods.update({"getMangaUrl", "getChapterUrl"})
        return frozenset(methods)


def _deep_link_ownership(ir: SourceIR) -> DeepLinkOwnership | None:
    if Capability.DEEP_LINKS not in ir.capabilities or not ir.relative_url_keys:
        return None
    implementation = project_implementation_ir(ir)
    detail = implementation.manga_detail
    if detail is None or detail.chapter_list is None:
        return None
    manga = _RouteTemplate.parse(detail.mapping.key_template)
    chapter = _RouteTemplate.parse(detail.chapter_list.mapping.key_template)
    if manga is None or chapter is None or not manga.structurally_prefixes(chapter):
        return None
    return DeepLinkOwnership(manga=manga, chapter=chapter)


def source_trait_ownership(ir: SourceIR) -> SourceTraitOwnership:
    """Return traits whose complete behavior is proven by deterministic source facts."""
    deterministic_network = deterministic_search_listing_available(ir)
    deterministic_pages = deterministic_network and deterministic_page_list_available(ir)
    parsed_base = urlsplit(ir.metadata.base_url)
    static_public_base = parsed_base.scheme in {"http", "https"} and bool(parsed_base.netloc)
    return SourceTraitOwnership(
        base_url=(
            deterministic_network
            and Capability.DYNAMIC_BASE_URLS in ir.capabilities
            and static_public_base
        ),
        image_request=deterministic_pages and Capability.IMAGE_HEADERS not in ir.capabilities,
        deep_links=_deep_link_ownership(ir),
    )


def deterministic_source_traits_available(ir: SourceIR) -> bool:
    return bool(source_trait_ownership(ir).traits)


def deterministic_source_shell_available(ir: SourceIR) -> bool:
    """Return whether every required Source method has a deterministic provider."""
    return (
        deterministic_search_listing_available(ir)
        and deterministic_manga_update_available(ir)
        and deterministic_page_list_available(ir)
    )


def render_source_shell(source_struct: str, *, listing_provider: bool) -> GeneratedFile:
    content = (
        _environment()
        .get_template("source_shell.rs.j2")
        .render(source_struct=source_struct, listing_provider=listing_provider)
    )
    return GeneratedFile(path="src/source.rs", content=content)


def with_deterministic_source_shell(
    ir: SourceIR,
    manifest: GenerationManifest,
) -> GenerationManifest:
    """Replace the required Source glue only when all provider methods are proven."""
    if not deterministic_source_shell_available(ir) or any(
        trait not in _SOURCE_SHELL_TRAITS for trait in manifest.implemented_traits
    ):
        return manifest
    rendered = render_source_shell(
        manifest.source_struct,
        listing_provider="ListingProvider" in manifest.implemented_traits,
    )
    files = [
        generated
        for generated in manifest.files
        if not generated.path.endswith(".rs") or generated.path == "src/lib.rs"
    ]
    files.append(rendered)
    paths = {generated.path for generated in files}
    lib_content = render_generated_lib_rs(
        manifest.source_struct,
        manifest.implemented_traits,
        paths,
    )
    files = [
        generated.model_copy(update={"content": lib_content})
        if generated.path == "src/lib.rs"
        else generated
        for generated in files
    ]
    return manifest.model_copy(update={"files": files, "dependencies": []})


def _last_identifier(node: object | None) -> str | None:
    if node is None:
        return None
    found: str | None = None
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in {"identifier", "raw_identifier", "type_identifier"}:
            found = current.text.decode("utf-8", errors="replace").removeprefix("r#")
        stack.extend(reversed(current.children))
    return found


def _without_owned_trait_impls(content: str, traits: frozenset[str]) -> str:
    edits = [
        (implementation.start_byte, implementation.end_byte)
        for implementation in RustInspection.from_content(content).nodes("impl_item")
        if _last_identifier(implementation.child_by_field_name("trait")) in traits
    ]
    encoded = content.encode("utf-8")
    for start, end in reversed(edits):
        encoded = encoded[:start] + encoded[end:]
    return encoded.decode("utf-8")


def _condition(route: _RouteTemplate) -> str:
    checks = [f"segments.len() == {len(route.segments)}"]
    checks.extend(
        f"segments[{index}] == {segment!r}"
        if segment is not None
        else f"!segments[{index}].is_empty()"
        for index, segment in enumerate(route.segments)
    )
    return "\n            && ".join(checks).replace("'", '"')


def _format_arguments(route: _RouteTemplate) -> str:
    return "".join(f", segments[{index}]" for index in route.placeholder_indices)


def render_source_traits(
    ir: SourceIR,
    source_struct: str,
    ownership: SourceTraitOwnership | None = None,
) -> GeneratedFile:
    ownership = ownership or source_trait_ownership(ir)
    if not ownership.traits:
        raise ValueError("SourceIR has no deterministic source traits")
    deep_links = ownership.deep_links
    content = (
        _environment()
        .get_template("source_traits.rs.j2")
        .render(
            source_struct=source_struct,
            public_base_url=ir.metadata.base_url.rstrip("/"),
            base_url=ownership.base_url,
            image_request=ownership.image_request,
            deep_links=deep_links is not None,
            manga_condition=_condition(deep_links.manga) if deep_links is not None else None,
            chapter_condition=_condition(deep_links.chapter) if deep_links is not None else None,
            manga_key_format=deep_links.manga.format_string if deep_links is not None else None,
            manga_key_arguments=(
                _format_arguments(deep_links.manga) if deep_links is not None else None
            ),
        )
    )
    return GeneratedFile(path="src/c2a_source_traits.rs", content=content)


def with_deterministic_source_traits(
    ir: SourceIR,
    manifest: GenerationManifest,
) -> GenerationManifest:
    """Replace AI trait implementations when the complete deterministic contract is known."""
    ownership = source_trait_ownership(ir)
    if not ownership.traits or not any(
        generated.path == "src/source.rs" for generated in manifest.files
    ):
        return manifest
    rendered = render_source_traits(ir, manifest.source_struct, ownership)
    owned_traits = frozenset(ownership.traits)
    files: list[GeneratedFile] = []
    for generated in manifest.files:
        if generated.path == rendered.path:
            continue
        content = generated.content
        if generated.path.endswith(".rs"):
            content = _without_owned_trait_impls(content, owned_traits)
        files.append(generated.model_copy(update={"content": content}))
    files.append(rendered)
    implemented_traits = [
        trait for trait in manifest.implemented_traits if trait not in owned_traits
    ]
    implemented_traits.extend(ownership.traits)
    paths = {generated.path for generated in files}
    lib_content = render_generated_lib_rs(
        manifest.source_struct,
        implemented_traits,
        paths,
    )
    files = [
        generated.model_copy(update={"content": lib_content})
        if generated.path == "src/lib.rs"
        else generated
        for generated in files
    ]
    return manifest.model_copy(update={"files": files, "implemented_traits": implemented_traits})
