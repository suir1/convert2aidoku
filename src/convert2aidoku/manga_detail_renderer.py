from __future__ import annotations

import re
from dataclasses import dataclass

from .implementation_ir import (
    ChapterListImplementationIR,
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
from .rust_inspection import RustInspection
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
class _ChapterView:
    response_type: str
    envelope_path: str | None
    endpoint_format: str
    endpoint_arguments: tuple[str, ...]
    page_size: int
    items_field: str
    total_field: str
    groups_field: str
    group_type: str
    group_name_field: str
    group_key_field: str
    item_type: str
    comic_path_field: str
    chapter_id_field: str
    title_field: str
    group_path_field: str | None
    default_group_value: str | None
    title_separator: str | None
    date_field: str | None
    date_format: str | None
    add_position_to_date: bool
    sort_field: str | None
    sort_descending: bool
    key_expression: str


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
    chapter: _ChapterView | None


@dataclass(frozen=True)
class MangaUpdateOwnership:
    java_methods: frozenset[str]
    java_method_prefixes: tuple[str, ...]
    dto_types: frozenset[str]


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


def _chapter_view(
    detail: MangaDetailImplementationIR,
    chapter: ChapterListImplementationIR,
    shapes: dict[str, DataShapeIR],
) -> _ChapterView:
    mapping = chapter.mapping
    if mapping.unresolved_fields:
        raise ValueError(
            f"chapter mapping {mapping.item_type} has unresolved fields: "
            + ", ".join(mapping.unresolved_fields)
        )
    if (
        mapping.key_template is None
        or mapping.comic_path_path is None
        or mapping.chapter_id_path is None
        or mapping.title_path is None
    ):
        raise ValueError("deterministic chapters require key and title mappings")
    group_shape = shapes[chapter.group_type]
    group_key_identifier = _rust_identifier(
        _mapping_field(group_shape, chapter.group_key_path).serialized_name
    )
    endpoint = chapter.endpoint
    endpoint_template = endpoint.path
    for parameter in endpoint.query_parameters:
        separator = "&" if "?" in endpoint_template else "?"
        endpoint_template += f"{separator}{parameter.name}={parameter.value_template}"
    endpoint_placeholders = re.findall(
        r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
        endpoint_template,
    )
    endpoint_arguments = []
    for placeholder in endpoint_placeholders:
        if placeholder == "comic_path":
            endpoint_arguments.append("manga_path")
        elif placeholder == "group":
            endpoint_arguments.append("group." + group_key_identifier)
        elif placeholder == "offset":
            endpoint_arguments.append("offset")
        else:
            raise ValueError(f"unsupported deterministic chapter endpoint field {placeholder}")
    endpoint_format = re.sub(
        r"\{[A-Za-z_][A-Za-z0-9_]*\}",
        "{}",
        endpoint_template,
    )
    item_shape = shapes[mapping.item_type]
    key_placeholders = re.findall(
        r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
        mapping.key_template,
    )
    if key_placeholders != ["comic_path", "chapter_id"]:
        raise ValueError("deterministic chapter key must contain comic_path then chapter_id")
    key_format = re.sub(
        r"\{[A-Za-z_][A-Za-z0-9_]*\}",
        "{}",
        mapping.key_template,
    )
    response_shape = shapes[endpoint.response_type]
    detail_shape = shapes[detail.endpoint.response_type]
    chapter_id_identifier = _rust_identifier(
        _mapping_field(item_shape, mapping.chapter_id_path).serialized_name
    )
    return _ChapterView(
        response_type=endpoint.response_type,
        envelope_path=endpoint.envelope_path,
        endpoint_format=endpoint_format,
        endpoint_arguments=tuple(endpoint_arguments),
        page_size=endpoint.pagination.page_size or 0,
        items_field=_rust_identifier(
            _mapping_field(response_shape, endpoint.items_path).serialized_name
        ),
        total_field=_rust_identifier(
            _mapping_field(response_shape, endpoint.total_path).serialized_name
        ),
        groups_field=_rust_identifier(
            _mapping_field(detail_shape, chapter.detail_groups_path).serialized_name
        ),
        group_type=chapter.group_type,
        group_name_field=_rust_identifier(
            _mapping_field(group_shape, chapter.group_name_path).serialized_name
        ),
        group_key_field=group_key_identifier,
        item_type=mapping.item_type,
        comic_path_field=_rust_identifier(
            _mapping_field(item_shape, mapping.comic_path_path).serialized_name
        ),
        chapter_id_field=_rust_identifier(
            _mapping_field(item_shape, mapping.chapter_id_path).serialized_name
        ),
        title_field=_rust_identifier(
            _mapping_field(item_shape, mapping.title_path).serialized_name
        ),
        group_path_field=(
            _rust_identifier(_mapping_field(item_shape, mapping.group_path_path).serialized_name)
            if mapping.group_path_path
            else None
        ),
        default_group_value=mapping.default_group_value,
        title_separator=mapping.title_separator,
        date_field=(
            _rust_identifier(_mapping_field(item_shape, mapping.date_path).serialized_name)
            if mapping.date_path
            else None
        ),
        date_format=mapping.date_format,
        add_position_to_date=mapping.add_position_to_date,
        sort_field=(
            _rust_identifier(_mapping_field(item_shape, mapping.sort_path).serialized_name)
            if mapping.sort_path
            else None
        ),
        sort_descending=mapping.sort_descending,
        key_expression=(
            f"format!({_quoted(key_format)}, chapter_comic_path, item.{chapter_id_identifier})"
        ),
    )


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
    chapter = _chapter_view(detail, detail.chapter_list, shapes) if detail.chapter_list else None
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
        chapter=chapter,
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
    chapter = detail.chapter_list
    if chapter is not None:
        add(detail.endpoint.response_type, chapter.detail_groups_path)
        add(chapter.endpoint.response_type, chapter.endpoint.items_path)
        add(chapter.endpoint.response_type, chapter.endpoint.total_path)
        add(chapter.group_type, chapter.group_name_path)
        add(chapter.group_type, chapter.group_key_path)
        chapter_mapping = chapter.mapping
        for path in (
            chapter_mapping.comic_path_path,
            chapter_mapping.chapter_id_path,
            chapter_mapping.title_path,
            chapter_mapping.group_path_path,
            chapter_mapping.date_path,
            chapter_mapping.sort_path,
        ):
            if path:
                add(chapter_mapping.item_type, path)

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
    if detail.chapter_list is not None:
        shapes.update({shape.name: shape for shape in detail.chapter_list.data_shapes})
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
            uses_btree_map=any(
                field.rust_type.startswith("BTreeMap<")
                for struct in structs
                for field in struct.fields
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


def deterministic_manga_update_available(ir: SourceIR) -> bool:
    try:
        implementation = project_implementation_ir(ir)
        render_manga_detail(ir, implementation)
    except (KeyError, ValueError):
        return False
    return bool(
        implementation.manga_detail is not None
        and implementation.manga_detail.chapter_list is not None
    )


def manga_update_ownership(ir: SourceIR) -> MangaUpdateOwnership | None:
    try:
        implementation = project_implementation_ir(ir)
        rendered = render_manga_detail(ir, implementation)
    except (KeyError, ValueError):
        return None
    detail = implementation.manga_detail
    if detail is None or detail.chapter_list is None:
        return None
    shapes = {shape.name: shape for shape in detail.data_shapes}
    shapes.update({shape.name: shape for shape in detail.chapter_list.data_shapes})
    structs = _required_structs(detail, shapes)
    if not rendered.content:
        return None
    return MangaUpdateOwnership(
        java_methods=frozenset(
            {
                "mangaDetailsParse",
                "mangaDetailsRequest",
                "chapterListParse",
                "chapterListRequest",
                "fetchChapterList",
                detail.endpoint.source_method,
                detail.chapter_list.endpoint.source_method,
            }
        ),
        java_method_prefixes=("fetchChapterList",),
        dto_types=frozenset(struct.name for struct in structs),
    )


def _delegate_manga_update_function(content: str) -> str | None:
    inspection = RustInspection.from_content(content)
    for function in inspection.named("get_manga_update"):
        body = function.node.child_by_field_name("body")
        if body is None or len(function.parameter_names) < 3:
            continue
        manga, needs_details, needs_chapters = function.parameter_names[-3:]
        relative_start = body.start_byte - function.node.start_byte
        relative_end = body.end_byte - function.node.start_byte
        encoded = function.text.encode("utf-8")
        replacement_body = (
            "{\n"
            "        crate::c2a_manga_detail::get_manga_update("
            f"{manga}, {needs_details}, {needs_chapters})\n"
            "    }"
        ).encode()
        replacement = (encoded[:relative_start] + replacement_body + encoded[relative_end:]).decode(
            "utf-8"
        )
        return content.replace(function.text, replacement, 1)
    return None


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
    owns_update = implementation.manga_detail.chapter_list is not None
    delegated = False
    if owns_update:
        rewritten: list[GeneratedFile] = []
        for generated in files:
            content = generated.content
            if not delegated and generated.path.endswith(".rs"):
                replacement = _delegate_manga_update_function(content)
                if replacement is not None:
                    content = replacement
                    delegated = True
            rewritten.append(generated.model_copy(update={"content": content}))
        if not delegated:
            return manifest
        files = rewritten
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
