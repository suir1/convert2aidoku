from __future__ import annotations

import re
from dataclasses import dataclass

from .implementation_ir import (
    DataShapeIR,
    ImplementationIR,
    MangaDetailImplementationIR,
    MangaStatusIR,
    project_implementation_ir,
)
from .listing_renderer import (
    _environment,
    _inner_list_type,
    _mapping_field,
    _quoted,
    _rust_identifier,
    _rust_type,
    _RustField,
    _RustStruct,
    _with_serde_derive,
)
from .models import GeneratedFile, GenerationManifest, SourceIR
from .scaffold import render_generated_lib_rs


@dataclass(frozen=True)
class _CollectionPathView:
    field: str
    child: str | None
    collection: bool


@dataclass(frozen=True)
class _StatusValueView:
    value: int
    status: str


@dataclass(frozen=True)
class _DetailView:
    response_type: str
    envelope_path: str | None
    item_field: str
    endpoint_format: str
    endpoint_argument: str
    key_prefix: str
    key_suffix: str
    key_expression: str
    title_field: str
    cover_field: str | None
    authors: _CollectionPathView | None
    tags: tuple[_CollectionPathView, ...]
    description_field: str | None
    status: _CollectionPathView | None
    status_values: tuple[_StatusValueView, ...]


_STATUS_VARIANTS: dict[MangaStatusIR, str] = {
    "unknown": "Unknown",
    "ongoing": "Ongoing",
    "completed": "Completed",
    "cancelled": "Cancelled",
    "hiatus": "Hiatus",
}


def _path_view(
    mapping_type: str,
    path: str,
    shapes: dict[str, DataShapeIR],
) -> _CollectionPathView:
    shape = shapes[mapping_type]
    if "[]." in path:
        root, child = path.split("[].", 1)
        collection = True
    elif path.endswith("[]"):
        root, child = path.removesuffix("[]"), ""
        collection = True
    elif "." in path:
        root, child = path.split(".", 1)
        collection = False
    else:
        root, child, collection = path, "", False
    root_field = _mapping_field(shape, root)
    child_field = None
    if child:
        nested_type = (
            _inner_list_type(root_field.source_type)
            if collection
            else root_field.source_type.rsplit(".", 1)[-1]
        )
        nested = shapes.get(nested_type or "")
        if nested is None:
            raise ValueError(f"detail mapping {mapping_type} has invalid nested path {path}")
        child_field = _mapping_field(nested, child)
    return _CollectionPathView(
        field=_rust_identifier(root_field.serialized_name),
        child=_rust_identifier(child_field.serialized_name) if child_field else None,
        collection=collection,
    )


def _single_placeholder_template(template: str, *, label: str) -> tuple[str, str, str]:
    placeholders = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", template)
    if len(placeholders) != 1:
        raise ValueError(f"{label} requires exactly one field placeholder")
    marker = "{" + placeholders[0] + "}"
    prefix, suffix = template.split(marker, 1)
    return placeholders[0], prefix, suffix


def _detail_view(
    detail: MangaDetailImplementationIR,
    shapes: dict[str, DataShapeIR],
) -> _DetailView:
    mapping = detail.mapping
    if mapping.unresolved_fields:
        raise ValueError(
            f"manga detail mapping {mapping.item_type} has unresolved fields: "
            + ", ".join(mapping.unresolved_fields)
        )
    if mapping.key_template is None or mapping.title_path is None:
        raise ValueError("deterministic manga detail requires key and title mappings")
    key_field, key_prefix, key_suffix = _single_placeholder_template(
        mapping.key_template,
        label="manga detail key",
    )
    endpoint_parameter, endpoint_prefix, endpoint_suffix = _single_placeholder_template(
        detail.endpoint.path,
        label="manga detail endpoint",
    )
    if endpoint_parameter != detail.endpoint.key_parameter:
        raise ValueError("manga detail endpoint parameter does not match its path")
    shape = shapes[mapping.item_type]
    key_argument = _rust_identifier(_mapping_field(shape, key_field).serialized_name)
    endpoint_format = endpoint_prefix + "{}" + endpoint_suffix
    response_shape = shapes[detail.endpoint.response_type]
    item_field = _rust_identifier(
        _mapping_field(response_shape, detail.endpoint.item_path).serialized_name
    )
    status = (
        _path_view(mapping.item_type, mapping.status_path, shapes) if mapping.status_path else None
    )
    key_format = mapping.key_template.replace("{" + key_field + "}", "{}")
    return _DetailView(
        response_type=detail.endpoint.response_type,
        envelope_path=detail.endpoint.envelope_path,
        item_field=item_field,
        endpoint_format=endpoint_format,
        endpoint_argument="manga_path",
        key_prefix=key_prefix,
        key_suffix=key_suffix,
        key_expression=f"format!({_quoted(key_format)}, item.{key_argument})",
        title_field=_rust_identifier(_mapping_field(shape, mapping.title_path).serialized_name),
        cover_field=(
            _rust_identifier(_mapping_field(shape, mapping.cover_path).serialized_name)
            if mapping.cover_path
            else None
        ),
        authors=(
            _path_view(mapping.item_type, mapping.authors_path, shapes)
            if mapping.authors_path
            else None
        ),
        tags=tuple(_path_view(mapping.item_type, path, shapes) for path in mapping.tags_paths),
        description_field=(
            _rust_identifier(_mapping_field(shape, mapping.description_path).serialized_name)
            if mapping.description_path
            else None
        ),
        status=status,
        status_values=tuple(
            _StatusValueView(value=value, status=_STATUS_VARIANTS[status_value])
            for value, status_value in sorted(mapping.status_values.items())
        ),
    )


def _required_structs(
    detail: MangaDetailImplementationIR,
    shapes: dict[str, DataShapeIR],
) -> tuple[_RustStruct, ...]:
    required: dict[str, set[str]] = {}

    def add(type_name: str, path: str) -> None:
        shape = shapes[type_name]
        if "[]." in path:
            root, child = path.split("[].", 1)
            collection = True
        elif "." in path:
            root, child = path.split(".", 1)
            collection = False
        else:
            root, child, collection = path.removesuffix("[]"), "", path.endswith("[]")
        field = _mapping_field(shape, root)
        required.setdefault(type_name, set()).add(field.serialized_name)
        if child:
            nested_type = (
                _inner_list_type(field.source_type)
                if collection
                else field.source_type.rsplit(".", 1)[-1]
            )
            if nested_type is None or nested_type not in shapes:
                raise ValueError(f"detail mapping has invalid nested path {path}")
            add(nested_type, child)

    mapping = detail.mapping
    add(detail.endpoint.response_type, detail.endpoint.item_path)
    for path in (
        *re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", mapping.key_template or ""),
        mapping.title_path,
        mapping.cover_path,
        mapping.authors_path,
        *mapping.tags_paths,
        mapping.description_path,
        mapping.status_path,
    ):
        if path:
            add(mapping.item_type, path)

    structs: list[_RustStruct] = []
    for shape in shapes.values():
        fields = required.get(shape.name)
        if not fields:
            continue
        structs.append(
            _RustStruct(
                name=shape.name,
                fields=tuple(
                    _RustField(
                        name=_rust_identifier(field.serialized_name),
                        serialized_name=field.serialized_name,
                        rust_type=_rust_type(field.source_type),
                    )
                    for field in shape.fields
                    if field.serialized_name in fields
                ),
            )
        )
    return tuple(structs)


def render_manga_detail(
    ir: SourceIR,
    implementation: ImplementationIR | None = None,
) -> GeneratedFile:
    """Render the proven manga-detail request and DTO mapping as an isolated helper."""
    implementation = implementation or project_implementation_ir(ir)
    detail = implementation.manga_detail
    if detail is None:
        raise ValueError("Implementation IR has no deterministic manga detail slice")
    shapes = {shape.name: shape for shape in detail.data_shapes}
    view = _detail_view(detail, shapes)
    structs = _required_structs(detail, shapes)
    content = (
        _environment()
        .get_template("manga_detail.rs.j2")
        .render(
            site_url=ir.metadata.base_url,
            structs=structs,
            detail=view,
            uses_vec=(
                view.authors is not None
                or bool(view.tags)
                or any(
                    field.rust_type.startswith("Vec<")
                    for struct in structs
                    for field in struct.fields
                )
            ),
        )
    )
    return GeneratedFile(path="src/c2a_manga_detail.rs", content=content)


def deterministic_manga_detail_available(ir: SourceIR) -> bool:
    try:
        render_manga_detail(ir)
    except (KeyError, ValueError):
        return False
    return True


def with_deterministic_manga_detail(
    ir: SourceIR,
    manifest: GenerationManifest,
) -> GenerationManifest:
    """Add the proven detail helper when the shared deterministic network module is present."""
    try:
        implementation = project_implementation_ir(ir)
        rendered = render_manga_detail(ir, implementation)
    except (KeyError, ValueError):
        return manifest
    if not any(generated.path == "src/c2a_listing.rs" for generated in manifest.files):
        return manifest
    files = [generated for generated in manifest.files if generated.path != rendered.path]
    files.append(rendered)
    generated_paths = {generated.path for generated in files}
    if "src/source.rs" in generated_paths:
        lib_content = render_generated_lib_rs(
            manifest.source_struct,
            manifest.implemented_traits,
            generated_paths,
        )
        files = [
            generated.model_copy(update={"content": lib_content})
            if generated.path == "src/lib.rs"
            else generated
            for generated in files
        ]
    warnings = list(manifest.warnings)
    detail_fallbacks = [
        fact for fact in implementation.policy_fallback_facts if fact.startswith("manga detail")
    ]
    if detail_fallbacks:
        warning = (
            "Deterministic manga detail intentionally uses raw API fields for "
            "SourceIR-excluded presentation transforms: " + "; ".join(detail_fallbacks)
        )
        if warning not in warnings:
            warnings.append(warning)
    return manifest.model_copy(
        update={
            "files": files,
            "dependencies": _with_serde_derive(manifest.dependencies),
            "warnings": warnings,
        }
    )
