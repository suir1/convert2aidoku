from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files as resource_files

from jinja2 import Environment, StrictUndefined

from .implementation_ir import (
    DataFieldIR,
    DataShapeIR,
    ImplementationIR,
    ListingContainerIR,
    ListingEndpointIR,
    ListingRole,
    MangaMappingIR,
    project_implementation_ir,
)
from .models import (
    Capability,
    DependencyRequest,
    GeneratedFile,
    GenerationManifest,
    RequestHeaderProfile,
    SourceFilterSpec,
    SourceIR,
)
from .rust_inspection import RustInspection
from .scaffold import render_generated_lib_rs


@dataclass(frozen=True)
class _RustField:
    name: str
    serialized_name: str
    rust_type: str

    @property
    def renamed(self) -> bool:
        return self.name.removeprefix("r#") != self.serialized_name


@dataclass(frozen=True)
class _RustStruct:
    name: str
    fields: tuple[_RustField, ...]


@dataclass(frozen=True)
class _MappingView:
    type_name: str
    function_name: str
    key_expression: str
    title_field: str
    cover_field: str | None
    authors_field: str | None
    authors_name_field: str | None
    authors_is_collection: bool
    tags_field: str | None
    tags_name_field: str | None
    tags_is_collection: bool
    description_field: str | None


@dataclass(frozen=True)
class _EndpointView:
    id: str
    path: str
    url_function: str
    fetch_function: str
    response_type: str
    items_field: str
    item_expression: str
    mapping_function: str
    has_next_expression: str
    query_lines: tuple[str, ...]


@dataclass(frozen=True)
class _HeaderProfileView:
    domains: tuple[str, ...]
    headers: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SearchListingOwnership:
    java_methods: frozenset[str]
    dto_types: frozenset[str]


def _provider_is_complete(ir: SourceIR, implementation: ImplementationIR) -> bool:
    listing = implementation.listing
    provider = listing.provider if listing is not None else None
    if provider is None:
        return False
    if Capability.POPULAR in ir.capabilities and provider.popular_endpoint_id is None:
        return False
    if Capability.LATEST in ir.capabilities and provider.latest is None:
        return False
    return Capability.POPULAR in ir.capabilities or Capability.LATEST in ir.capabilities


_RUST_KEYWORDS = {
    "as",
    "break",
    "const",
    "continue",
    "crate",
    "else",
    "enum",
    "extern",
    "false",
    "fn",
    "for",
    "if",
    "impl",
    "in",
    "let",
    "loop",
    "match",
    "mod",
    "move",
    "mut",
    "pub",
    "ref",
    "return",
    "self",
    "Self",
    "static",
    "struct",
    "super",
    "trait",
    "true",
    "type",
    "unsafe",
    "use",
    "where",
    "while",
}


def _environment() -> Environment:
    template_dir = resource_files("convert2aidoku").joinpath("resources", "templates")
    environment = Environment(
        loader=__import__("jinja2").FileSystemLoader(str(template_dir)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters["rust_string"] = _quoted
    return environment


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace(r"\b", r"\u{8}").replace(r"\f", r"\u{c}")


def _snake_case(value: str) -> str:
    value = re.sub(r"(?<!^)(?=[A-Z])", "_", value)
    return re.sub(r"[^a-zA-Z0-9_]", "_", value).lower().strip("_") or "value"


def _rust_identifier(value: str) -> str:
    name = _snake_case(value)
    return f"r#{name}" if name in _RUST_KEYWORDS else name


def _inner_list_type(source_type: str) -> str | None:
    found = re.fullmatch(r"(?:java\.util\.)?List<\s*([^<>]+?)\s*>", source_type)
    return found.group(1).strip() if found else None


def _rust_type(source_type: str) -> str:
    source_type = source_type.strip()
    inner = _inner_list_type(source_type)
    if inner is not None:
        return f"Vec<{_rust_type(inner)}>"
    return {
        "String": "String",
        "int": "i32",
        "Integer": "i32",
        "long": "i64",
        "Long": "i64",
        "boolean": "bool",
        "Boolean": "bool",
    }.get(source_type, source_type.rsplit(".", 1)[-1])


def _field(shape: DataShapeIR, path: str) -> DataFieldIR:
    root = path.split("[]", 1)[0].split(".", 1)[0]
    found = next(
        (field for field in shape.fields if field.serialized_name == root or field.name == root),
        None,
    )
    if found is None:
        raise ValueError(f"Implementation IR shape {shape.name} has no field {root}")
    return found


def _endpoint_for_role(
    endpoints: list[ListingEndpointIR],
    role: ListingRole,
    *,
    required: bool,
) -> ListingEndpointIR | None:
    matches = [endpoint for endpoint in endpoints if endpoint.role == role]
    if len(matches) == 1:
        return matches[0]
    if not matches and not required:
        return None
    raise ValueError(f"deterministic search listing requires exactly one {role.value} endpoint")


def _filter_default(spec: SourceFilterSpec | None) -> str:
    return spec.options[spec.default_index].value if spec is not None else ""


def _sort_default(spec: SourceFilterSpec) -> str:
    value = spec.options[spec.default_index].value
    return value if spec.default_ascending else f"-{value}"


def _query_lines(
    endpoint: ListingEndpointIR,
    filter_specs: dict[str, SourceFilterSpec],
) -> tuple[str, ...]:
    lines = ["let _ = (query, filters);"]
    offset_declared = False
    for parameter in endpoint.query_parameters:
        name = _quoted(parameter.name)
        if parameter.source == "static":
            lines.append(f"push_query(&mut url, {name}, {_quoted(parameter.value_template)});")
            continue
        if parameter.source == "page":
            if parameter.value_template == "{offset}":
                if not offset_declared:
                    page_size = endpoint.pagination.page_size if endpoint.pagination else None
                    if page_size is None:
                        raise ValueError(f"endpoint {endpoint.id} has no deterministic page size")
                    lines.append(
                        f"let offset = page.saturating_sub(1).saturating_mul({page_size});"
                    )
                    offset_declared = True
                lines.append(f"push_query(&mut url, {name}, &offset.to_string());")
            elif parameter.value_template == "{page}":
                lines.append(f"push_query(&mut url, {name}, &page.to_string());")
            else:
                raise ValueError(f"endpoint {endpoint.id} has an unknown page binding")
            continue
        if parameter.source == "query":
            lines.append(f"push_query(&mut url, {name}, query);")
            continue
        if parameter.source != "filter":
            raise ValueError(f"endpoint {endpoint.id} contains an unresolved query binding")
        binding = re.fullmatch(r"\{filter:([a-z][a-z0-9_]*)\}", parameter.value_template)
        if binding is None:
            raise ValueError(f"endpoint {endpoint.id} has an invalid filter binding")
        filter_id = binding.group(1)
        spec = filter_specs.get(filter_id)
        variable = f"query_{_snake_case(parameter.name)}"
        if spec is not None and spec.kind == "sort":
            options = ", ".join(_quoted(option.value) for option in spec.options)
            lines.extend(
                [
                    f"let {variable} = selected_sort(filters, {_quoted(filter_id)})",
                    "    .map(|(index, ascending)| {",
                    f"        let values = [{options}];",
                    "        let value = values.get(index as usize).copied().unwrap_or(values[0]);",
                    '        if ascending { String::from(value) } else { format!("-{value}") }',
                    "    })",
                    f"    .unwrap_or_else(|| String::from({_quoted(_sort_default(spec))}));",
                ]
            )
        else:
            default = _filter_default(spec)
            lines.append(
                f"let {variable} = selected_value(filters, {_quoted(filter_id)})"
                f".unwrap_or({_quoted(default)});"
            )
        if parameter.required:
            lines.append(f"push_query(&mut url, {name}, &{variable});")
        else:
            lines.extend(
                [
                    f"if !{variable}.is_empty() {{",
                    f"    push_query(&mut url, {name}, &{variable});",
                    "}",
                ]
            )
    return tuple(lines)


def _mapping_view(mapping: MangaMappingIR, shapes: dict[str, DataShapeIR]) -> _MappingView:
    shape = shapes[mapping.item_type]
    if mapping.key_template is None or mapping.title_path is None:
        raise ValueError(f"manga mapping {mapping.item_type} lacks key or title")
    placeholders = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", mapping.key_template)
    format_string = re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}", "{}", mapping.key_template)
    literal_format = format_string.replace("{}", "")
    if "{" in literal_format or "}" in literal_format or not placeholders:
        raise ValueError(f"manga mapping {mapping.item_type} has an invalid key template")
    arguments = ", ".join(
        f"item.{_rust_identifier(_field(shape, name).serialized_name)}" for name in placeholders
    )

    def nested(path: str | None) -> tuple[str | None, str | None, bool]:
        if path is None:
            return None, None, False
        if "[]." in path:
            root, child = path.split("[].", 1)
            is_collection = True
        elif path.endswith("[]"):
            root = path.removesuffix("[]")
            child = ""
            is_collection = True
        else:
            root = path
            child = ""
            is_collection = False
        return (
            _rust_identifier(_field(shape, root).serialized_name),
            (_rust_identifier(child) if child else None),
            is_collection,
        )

    authors_field, authors_name, authors_is_collection = nested(mapping.authors_path)
    tags_field, tags_name, tags_is_collection = nested(mapping.tags_path)
    return _MappingView(
        type_name=mapping.item_type,
        function_name=f"manga_from_{_snake_case(mapping.item_type)}",
        key_expression=f"format!({_quoted(format_string)}, {arguments})",
        title_field=_rust_identifier(_field(shape, mapping.title_path).serialized_name),
        cover_field=(
            _rust_identifier(_field(shape, mapping.cover_path).serialized_name)
            if mapping.cover_path
            else None
        ),
        authors_field=authors_field,
        authors_name_field=authors_name,
        authors_is_collection=authors_is_collection,
        tags_field=tags_field,
        tags_name_field=tags_name,
        tags_is_collection=tags_is_collection,
        description_field=(
            _rust_identifier(_field(shape, mapping.description_path).serialized_name)
            if mapping.description_path
            else None
        ),
    )


def _required_structs(
    endpoints: list[ListingEndpointIR],
    containers: dict[str, ListingContainerIR],
    mappings: dict[str, MangaMappingIR],
    shapes: dict[str, DataShapeIR],
) -> tuple[_RustStruct, ...]:
    required: dict[str, set[str]] = {}

    def add(type_name: str, *paths: str | None) -> None:
        shape = shapes.get(type_name)
        if shape is None:
            raise ValueError(f"Implementation IR has no data shape for {type_name}")
        fields = required.setdefault(type_name, set())
        for path in paths:
            if path:
                fields.add(_field(shape, path).serialized_name)

    for endpoint in endpoints:
        if endpoint.response_type is None:
            raise ValueError(f"endpoint {endpoint.id} has no response type")
        container = containers[endpoint.response_type]
        add(
            container.type_name,
            container.items_path,
            container.next_path,
            container.limit_path,
            container.offset_path,
            container.total_path,
        )
        add(container.item_type, container.item_wrapper_path)
        mapping = mappings.get(container.manga_item_type)
        if mapping is None:
            raise ValueError(
                f"Implementation IR has no manga mapping for {container.manga_item_type}"
            )
        add(
            mapping.item_type,
            *re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", mapping.key_template or ""),
            mapping.title_path,
            mapping.cover_path,
            mapping.authors_path,
            mapping.tags_path,
            mapping.description_path,
        )
        for nested_path in (mapping.authors_path, mapping.tags_path):
            if not nested_path or "[]." not in nested_path:
                continue
            root, child = nested_path.split("[].", 1)
            root_field = _field(shapes[mapping.item_type], root)
            nested_type = _inner_list_type(root_field.source_type)
            if nested_type:
                add(nested_type, child)

    structs = []
    for shape in shapes.values():
        names = required.get(shape.name)
        if not names:
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
                    if field.serialized_name in names
                ),
            )
        )
    return tuple(structs)


def _endpoint_view(
    endpoint: ListingEndpointIR,
    containers: dict[str, ListingContainerIR],
    mappings: dict[str, MangaMappingIR],
    filter_specs: dict[str, SourceFilterSpec],
) -> _EndpointView:
    assert endpoint.response_type is not None
    container = containers[endpoint.response_type]
    mapping = mappings[container.manga_item_type]
    item_expression = "item"
    if container.item_wrapper_path:
        item_expression += f".{_rust_identifier(container.item_wrapper_path)}"
    if container.next_path:
        has_next = f"!result.{_rust_identifier(container.next_path)}.is_empty()"
    elif container.limit_path and container.offset_path and container.total_path:
        has_next = (
            f"result.{_rust_identifier(container.offset_path)} + "
            f"result.{_rust_identifier(container.limit_path)} < "
            f"result.{_rust_identifier(container.total_path)}"
        )
    elif endpoint.pagination is not None and endpoint.pagination.page_size is not None:
        has_next = (
            f"result.{_rust_identifier(container.items_path)}.len() >= "
            f"{endpoint.pagination.page_size}"
        )
    else:
        has_next = "!result." + _rust_identifier(container.items_path) + ".is_empty()"
    return _EndpointView(
        id=endpoint.id,
        path=endpoint.path,
        url_function=f"{endpoint.id}_url",
        fetch_function=f"fetch_{endpoint.id}",
        response_type=endpoint.response_type,
        items_field=_rust_identifier(container.items_path),
        item_expression=item_expression,
        mapping_function=f"manga_from_{_snake_case(mapping.item_type)}",
        has_next_expression=has_next,
        query_lines=_query_lines(endpoint, filter_specs),
    )


def _user_agent(ir: SourceIR) -> str | None:
    for source in ir.files:
        found = re.search(r'\bUSER_AGENT\s*=\s*"((?:\\.|[^"\\])+)"', source.content)
        if found:
            try:
                value = json.loads(f'"{found.group(1)}"')
            except json.JSONDecodeError:
                continue
            if isinstance(value, str):
                return value
    return None


def _header_views(
    ir: SourceIR,
    default_host: str,
) -> tuple[tuple[tuple[str, str], ...], _HeaderProfileView, tuple[_HeaderProfileView, ...]]:
    global_headers = dict(ir.shared_request_headers)
    if user_agent := _user_agent(ir):
        global_headers.setdefault("User-Agent", user_agent)
    profiles = list(ir.request_header_profiles)
    default = next(
        (profile for profile in profiles if default_host in profile.domains),
        profiles[0] if profiles else RequestHeaderProfile(name="default"),
    )
    conditional = tuple(
        _HeaderProfileView(
            domains=tuple(domain for domain in profile.domains if domain != "custom"),
            headers=tuple(profile.headers.items()),
        )
        for profile in profiles
        if profile != default and any(domain != "custom" for domain in profile.domains)
    )
    return (
        tuple(global_headers.items()),
        _HeaderProfileView(domains=tuple(default.domains), headers=tuple(default.headers.items())),
        conditional,
    )


def render_search_listing(
    ir: SourceIR,
    implementation: ImplementationIR | None = None,
) -> GeneratedFile:
    """Render the Source search/rank/browse vertical slice without generated Rust input."""
    implementation = implementation or project_implementation_ir(ir)
    listing = implementation.listing
    if listing is None:
        raise ValueError("Implementation IR has no deterministic listing slice")
    search = _endpoint_for_role(listing.endpoints, ListingRole.SEARCH, required=True)
    browse = _endpoint_for_role(listing.endpoints, ListingRole.BROWSE, required=True)
    rank = _endpoint_for_role(listing.endpoints, ListingRole.RANK, required=False)
    assert search is not None and browse is not None
    provider = listing.provider if _provider_is_complete(ir, implementation) else None
    endpoints_by_id = {endpoint.id: endpoint for endpoint in listing.endpoints}
    provider_endpoint_ids: list[str] = []
    if provider is not None:
        if provider.popular_endpoint_id is not None:
            provider_endpoint_ids.append(provider.popular_endpoint_id)
        if provider.latest is not None:
            provider_endpoint_ids.extend(provider.latest.endpoint_ids_by_setting_value.values())
            provider_endpoint_ids.append(provider.latest.default_endpoint_id)
    selected_ids = dict.fromkeys(
        [search.id, *([rank.id] if rank else []), browse.id, *provider_endpoint_ids]
    )
    selected = [endpoints_by_id[endpoint_id] for endpoint_id in selected_ids]
    if listing.api_base.default_host is None:
        raise ValueError("deterministic listing renderer requires a default API host")
    containers = {container.type_name: container for container in listing.containers}
    mappings = {mapping.item_type: mapping for mapping in listing.manga_mappings}
    shapes = {shape.name: shape for shape in listing.data_shapes}
    filter_specs = {spec.id: spec for spec in ir.filter_specs}
    views = [_endpoint_view(endpoint, containers, mappings, filter_specs) for endpoint in selected]
    views_by_id = {view.id: view for view in views}
    mapping_types = {
        containers[endpoint.response_type].manga_item_type
        for endpoint in selected
        if endpoint.response_type is not None
    }
    mapping_views = [_mapping_view(mappings[type_name], shapes) for type_name in mapping_types]
    global_headers, default_profile, conditional_profiles = _header_views(
        ir,
        listing.api_base.default_host,
    )
    rank_filter = (
        next(
            (
                parameter.value_template.removeprefix("{filter:").removesuffix("}")
                for parameter in rank.query_parameters
                if parameter.source == "filter" and parameter.value_template == "{filter:rank}"
            ),
            None,
        )
        if rank is not None
        else None
    )
    content = (
        _environment()
        .get_template("search_listing.rs.j2")
        .render(
            site_url=ir.metadata.base_url,
            api_scheme=listing.api_base.scheme,
            api_path_prefix=listing.api_base.path_prefix,
            api_setting_key=listing.api_base.setting_key,
            api_custom_setting_key=(
                f"{listing.api_base.setting_key}_custom" if listing.api_base.setting_key else None
            ),
            api_default_host=listing.api_base.default_host,
            structs=_required_structs(selected, containers, mappings, shapes),
            mappings=sorted(mapping_views, key=lambda item: item.type_name),
            endpoints=views,
            search_endpoint=views_by_id[search.id],
            rank_endpoint=views_by_id[rank.id] if rank else None,
            browse_endpoint=views_by_id[browse.id],
            rank_filter=rank_filter,
            popular_endpoint=(
                views_by_id[provider.popular_endpoint_id]
                if provider is not None and provider.popular_endpoint_id is not None
                else None
            ),
            latest_endpoint=(
                views_by_id[provider.latest.default_endpoint_id]
                if provider is not None and provider.latest is not None
                else None
            ),
            latest_setting_key=(
                provider.latest.setting_key
                if provider is not None and provider.latest is not None
                else None
            ),
            latest_setting_default=(
                provider.latest.setting_default
                if provider is not None and provider.latest is not None
                else None
            ),
            latest_alternates=(
                tuple(
                    (value, views_by_id[endpoint_id])
                    for value, endpoint_id in provider.latest.endpoint_ids_by_setting_value.items()
                    if endpoint_id != provider.latest.default_endpoint_id
                )
                if provider is not None and provider.latest is not None
                else ()
            ),
            global_headers=global_headers,
            default_profile=default_profile,
            conditional_profiles=conditional_profiles,
        )
    )
    return GeneratedFile(path="src/c2a_listing.rs", content=content)


def deterministic_search_listing_available(ir: SourceIR) -> bool:
    try:
        render_search_listing(ir)
    except (KeyError, ValueError):
        return False
    return True


def deterministic_listing_provider_available(ir: SourceIR) -> bool:
    try:
        implementation = project_implementation_ir(ir)
        render_search_listing(ir, implementation)
    except (KeyError, ValueError):
        return False
    return _provider_is_complete(ir, implementation)


def search_listing_ownership(ir: SourceIR) -> SearchListingOwnership | None:
    try:
        implementation = project_implementation_ir(ir)
        listing = implementation.listing
        if listing is None:
            return None
        search = _endpoint_for_role(listing.endpoints, ListingRole.SEARCH, required=True)
        browse = _endpoint_for_role(listing.endpoints, ListingRole.BROWSE, required=True)
        rank = _endpoint_for_role(listing.endpoints, ListingRole.RANK, required=False)
        assert search is not None and browse is not None
        provider = listing.provider if _provider_is_complete(ir, implementation) else None
        endpoints_by_id = {endpoint.id: endpoint for endpoint in listing.endpoints}
        provider_endpoint_ids: list[str] = []
        if provider is not None:
            if provider.popular_endpoint_id is not None:
                provider_endpoint_ids.append(provider.popular_endpoint_id)
            if provider.latest is not None:
                provider_endpoint_ids.extend(provider.latest.endpoint_ids_by_setting_value.values())
                provider_endpoint_ids.append(provider.latest.default_endpoint_id)
        selected_ids = dict.fromkeys(
            [search.id, *([rank.id] if rank else []), browse.id, *provider_endpoint_ids]
        )
        selected = [endpoints_by_id[endpoint_id] for endpoint_id in selected_ids]
        containers = {container.type_name: container for container in listing.containers}
        mappings = {mapping.item_type: mapping for mapping in listing.manga_mappings}
        shapes = {shape.name: shape for shape in listing.data_shapes}
        structs = _required_structs(selected, containers, mappings, shapes)
        render_search_listing(ir, implementation)
    except (KeyError, ValueError):
        return None
    return SearchListingOwnership(
        java_methods=frozenset(
            {
                "searchMangaParse",
                "searchMangaRequest",
                *(
                    {"popularMangaParse", "popularMangaRequest"}
                    if provider is not None and provider.popular_endpoint_id is not None
                    else set()
                ),
                *(
                    {"latestUpdatesParse", "latestUpdatesRequest"}
                    if provider is not None and provider.latest is not None
                    else set()
                ),
                *(endpoint.source_method for endpoint in selected),
            }
        ),
        dto_types=frozenset(struct.name for struct in structs),
    )


def _delegate_search_function(content: str) -> str | None:
    inspection = RustInspection.from_content(content)
    for function in inspection.named("get_search_manga_list"):
        body = function.node.child_by_field_name("body")
        if body is None:
            continue
        relative_start = body.start_byte - function.node.start_byte
        relative_end = body.end_byte - function.node.start_byte
        encoded = function.text.encode("utf-8")
        if len(function.parameter_names) < 3:
            continue
        query, page, filters = function.parameter_names[-3:]
        replacement_body = (
            "{\n"
            "        crate::c2a_listing::get_search_manga_list("
            f"{query}, {page}, {filters})\n"
            "    }"
        ).encode()
        replacement = (encoded[:relative_start] + replacement_body + encoded[relative_end:]).decode(
            "utf-8"
        )
        return content.replace(function.text, replacement, 1)
    return None


def _with_listing_provider_delegate(content: str, source_struct: str) -> str:
    inspection = RustInspection.from_content(content)
    for function in inspection.named("get_manga_list"):
        body = function.node.child_by_field_name("body")
        if body is None:
            continue
        relative_start = body.start_byte - function.node.start_byte
        relative_end = body.end_byte - function.node.start_byte
        encoded = function.text.encode("utf-8")
        if len(function.parameter_names) < 2:
            continue
        listing, page = function.parameter_names[-2:]
        replacement_body = (
            f"{{\n        crate::c2a_listing::get_manga_list({listing}, {page})\n    }}"
        ).encode()
        replacement = (encoded[:relative_start] + replacement_body + encoded[relative_end:]).decode(
            "utf-8"
        )
        return content.replace(function.text, replacement, 1)
    implementation = (
        "\n\nimpl aidoku::ListingProvider for "
        f"{source_struct} {{\n"
        "    fn get_manga_list(\n"
        "        &self,\n"
        "        listing: aidoku::Listing,\n"
        "        page: i32,\n"
        "    ) -> aidoku::Result<aidoku::MangaPageResult> {\n"
        "        crate::c2a_listing::get_manga_list(listing, page)\n"
        "    }\n"
        "}\n"
    )
    return content.rstrip() + implementation


def _with_serde_derive(dependencies: list[DependencyRequest]) -> list[DependencyRequest]:
    result = list(dependencies)
    for index, dependency in enumerate(result):
        if dependency.name != "serde":
            continue
        features = list(dict.fromkeys([*dependency.features, "derive"]))
        result[index] = dependency.model_copy(update={"features": features})
        return result
    result.append(
        DependencyRequest(
            name="serde",
            features=["derive"],
            reason="deterministic listing response DTOs",
        )
    )
    return result


def with_deterministic_search_listing(
    ir: SourceIR,
    manifest: GenerationManifest,
) -> GenerationManifest:
    """Own the search-listing Rust file and Source delegation in an effective manifest."""
    try:
        implementation = project_implementation_ir(ir)
        rendered = render_search_listing(ir, implementation)
    except (KeyError, ValueError):
        return manifest
    provider_owned = _provider_is_complete(ir, implementation)
    implemented_traits = list(manifest.implemented_traits)
    if provider_owned and "ListingProvider" not in implemented_traits:
        implemented_traits.append("ListingProvider")
    files: list[GeneratedFile] = []
    delegated = False
    for generated in manifest.files:
        if generated.path == rendered.path:
            continue
        content = generated.content
        if not delegated and generated.path.endswith(".rs"):
            replacement = _delegate_search_function(content)
            if replacement is not None:
                content = replacement
                delegated = True
        if provider_owned and generated.path == "src/source.rs":
            content = _with_listing_provider_delegate(content, manifest.source_struct)
        files.append(generated.model_copy(update={"content": content}))
    if not delegated:
        return manifest
    files.append(rendered)
    generated_paths = {generated.path for generated in files}
    if "src/source.rs" in generated_paths:
        lib_content = render_generated_lib_rs(
            manifest.source_struct,
            implemented_traits,
            generated_paths,
        )
        files = [
            generated.model_copy(update={"content": lib_content})
            if generated.path == "src/lib.rs"
            else generated
            for generated in files
        ]
    return manifest.model_copy(
        update={
            "files": files,
            "dependencies": _with_serde_derive(manifest.dependencies),
            "implemented_traits": implemented_traits,
        }
    )
