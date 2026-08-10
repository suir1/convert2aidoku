from __future__ import annotations

from dataclasses import dataclass

from .implementation_ir import (
    DataShapeIR,
    ImplementationIR,
    PageListImplementationIR,
    PageRouteVariantIR,
    project_implementation_ir,
)
from .listing_renderer import (
    _environment,
    _mapping_field,
    _quoted,
    _rust_identifier,
    _with_serde_derive,
)
from .models import GeneratedFile, GenerationManifest, SourceIR
from .rust_inspection import RustInspection
from .scaffold import render_generated_lib_rs


@dataclass(frozen=True)
class _FieldView:
    name: str
    serialized_name: str


@dataclass(frozen=True)
class _VariantView:
    is_default: bool
    domains: tuple[str, ...]
    expression: str


@dataclass(frozen=True)
class _PageView:
    response_type: str
    chapter_field: _FieldView
    chapter_type: str
    contents_field: _FieldView
    content_item_type: str
    url_field: _FieldView
    words_field: _FieldView
    words_nullable: bool
    endpoint_format: str
    envelope_path: str
    variants: tuple[_VariantView, ...]
    resolution_setting_key: str
    resolution_default_pixels: str
    resolution_values: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PageListOwnership:
    java_methods: frozenset[str]
    java_method_prefixes: tuple[str, ...]
    dto_types: frozenset[str]
    source_stems: frozenset[str]


def _field_view(shape: DataShapeIR, path: str) -> _FieldView:
    field = _mapping_field(shape, path)
    return _FieldView(
        name=_rust_identifier(field.serialized_name),
        serialized_name=field.serialized_name,
    )


def _variant_expression(variant: PageRouteVariantIR) -> str:
    key = "key"
    if variant.strip_prefix:
        key = f"{key}.strip_prefix({_quoted(variant.strip_prefix)}).unwrap_or({key})"
    expression = f"String::from({key})"
    for old, new in variant.replacements:
        expression += f".replace({_quoted(old)}, {_quoted(new)})"
    return expression


def _page_view(page: PageListImplementationIR) -> _PageView:
    mapping = page.mapping
    if not mapping.reject_empty_first_url or not mapping.sort_by_words:
        raise ValueError(
            "deterministic pages require complete empty-first and words-order behavior"
        )
    if mapping.resolution_regex != r"\d+(?=x\.(?:jpg|webp)$)":
        raise ValueError("deterministic pages require the proven terminal x.jpg/x.webp rule")
    shapes = {shape.name: shape for shape in page.data_shapes}
    response_shape = shapes[mapping.response_type]
    chapter_shape = shapes[mapping.chapter_type]
    content_shape = shapes[mapping.content_item_type]
    marker = "{" + page.normalized_key_parameter + "}"
    endpoint_format = page.endpoint_template.replace(marker, "{}", 1)
    return _PageView(
        response_type=mapping.response_type,
        chapter_field=_field_view(response_shape, mapping.chapter_path),
        chapter_type=mapping.chapter_type,
        contents_field=_field_view(chapter_shape, mapping.contents_path),
        content_item_type=mapping.content_item_type,
        url_field=_field_view(content_shape, mapping.url_path),
        words_field=_field_view(chapter_shape, mapping.words_path),
        words_nullable=mapping.words_nullable,
        endpoint_format=endpoint_format,
        envelope_path=page.envelope_path,
        variants=tuple(
            _VariantView(
                is_default=variant.is_default,
                domains=tuple(variant.domains),
                expression=_variant_expression(variant),
            )
            for variant in page.variants
        ),
        resolution_setting_key=mapping.resolution_setting_key,
        resolution_default_pixels=mapping.resolution_values[mapping.resolution_default],
        resolution_values=tuple(mapping.resolution_values.items()),
    )


def render_page_list(
    ir: SourceIR,
    implementation: ImplementationIR | None = None,
) -> GeneratedFile:
    """Render a page-list helper only when route, DTO, ordering, and image rules are proven."""
    implementation = implementation or project_implementation_ir(ir)
    if implementation.page_list is None:
        raise ValueError("Implementation IR has no deterministic page-list slice")
    view = _page_view(implementation.page_list)
    content = (
        _environment()
        .get_template("page_list.rs.j2")
        .render(site_url=ir.metadata.base_url, page=view)
    )
    return GeneratedFile(path="src/c2a_pages.rs", content=content)


def deterministic_page_list_available(ir: SourceIR) -> bool:
    try:
        render_page_list(ir)
    except (KeyError, ValueError):
        return False
    return True


def page_list_ownership(ir: SourceIR) -> PageListOwnership | None:
    try:
        implementation = project_implementation_ir(ir)
        rendered = render_page_list(ir, implementation)
    except (KeyError, ValueError):
        return None
    page = implementation.page_list
    if page is None or not rendered.content:
        return None
    mapping = page.mapping
    resolution_stems = frozenset(
        source.path.rsplit("/", 1)[-1].removesuffix(".java")
        for source in ir.files
        if source.path.endswith(".java")
        and mapping.resolution_setting_key in source.content
        and mapping.resolution_regex.replace("\\", "\\\\") in source.content
    )
    if len(resolution_stems) != 1:
        return None
    return PageListOwnership(
        java_methods=frozenset(
            {
                "pageListParse",
                "pageListRequest",
                "fetchPageList",
                "chapterContentDetailUrl",
                "fixChapterId",
            }
        ),
        java_method_prefixes=("fetchPageList",),
        dto_types=frozenset(
            {mapping.response_type, mapping.chapter_type, mapping.content_item_type}
        ),
        source_stems=resolution_stems,
    )


def _delegate_page_list_function(content: str) -> str | None:
    inspection = RustInspection.from_content(content)
    for function in inspection.named("get_page_list"):
        body = function.node.child_by_field_name("body")
        if body is None or not function.parameter_names:
            continue
        chapter = function.parameter_names[-1]
        relative_start = body.start_byte - function.node.start_byte
        relative_end = body.end_byte - function.node.start_byte
        encoded = function.text.encode("utf-8")
        replacement_body = (
            f"{{\n        crate::c2a_pages::get_page_list({chapter})\n    }}"
        ).encode()
        replacement = (encoded[:relative_start] + replacement_body + encoded[relative_end:]).decode(
            "utf-8"
        )
        return content.replace(function.text, replacement, 1)
    return None


def with_deterministic_page_list(
    ir: SourceIR,
    manifest: GenerationManifest,
) -> GenerationManifest:
    """Replace Source::get_page_list with the proven deterministic page helper."""
    try:
        rendered = render_page_list(ir)
    except (KeyError, ValueError):
        return manifest
    if not any(generated.path == "src/c2a_listing.rs" for generated in manifest.files):
        return manifest
    files = [generated for generated in manifest.files if generated.path != rendered.path]
    delegated = False
    rewritten: list[GeneratedFile] = []
    for generated in files:
        content = generated.content
        if not delegated and generated.path == "src/source.rs":
            replacement = _delegate_page_list_function(content)
            if replacement is not None:
                content = replacement
                delegated = True
        rewritten.append(generated.model_copy(update={"content": content}))
    if not delegated:
        return manifest
    rewritten.append(rendered)
    generated_paths = {generated.path for generated in rewritten}
    if "src/source.rs" in generated_paths:
        lib_content = render_generated_lib_rs(
            manifest.source_struct,
            manifest.implemented_traits,
            generated_paths,
        )
        rewritten = [
            generated.model_copy(update={"content": lib_content})
            if generated.path == "src/lib.rs"
            else generated
            for generated in rewritten
        ]
    return manifest.model_copy(
        update={
            "files": rewritten,
            "dependencies": _with_serde_derive(manifest.dependencies),
        }
    )
