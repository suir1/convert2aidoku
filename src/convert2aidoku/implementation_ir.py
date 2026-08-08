from __future__ import annotations

import json
import re
from collections.abc import Iterable
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .decompiled_input import DecompiledDtoShape, decompiled_dto_shapes
from .models import SourceFile, SourceFilterSpec, SourceIR


class ListingRole(StrEnum):
    BROWSE = "browse"
    SEARCH = "search"
    POPULAR = "popular"
    LATEST = "latest"
    RANK = "rank"


class ApiBaseIR(BaseModel):
    """Deterministic API-origin facts recovered from the Input Source."""

    model_config = ConfigDict(extra="forbid")

    scheme: Literal["http", "https"] = "https"
    path_prefix: str = ""
    dynamic: bool = False
    setting_key: str | None = None
    default_host: str | None = None
    candidate_hosts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def host_facts_are_consistent(self) -> ApiBaseIR:
        if len(self.candidate_hosts) != len(set(self.candidate_hosts)):
            raise ValueError("API base candidate hosts must be unique")
        if self.path_prefix and not self.path_prefix.startswith("/"):
            raise ValueError("API base path prefix must be absolute")
        return self


class QueryParameterIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$")
    value_template: str
    source: Literal["static", "page", "query", "filter", "unknown"]
    required: bool = True


class PaginationIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["offset", "page"]
    page_parameter: str
    page_size: int | None = Field(default=None, gt=0)
    page_size_parameter: str | None = None
    first_page: int = 1
    offset_formula: str | None = None


class ListingEndpointIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    role: ListingRole
    source_method: str = Field(min_length=1)
    path: str = Field(pattern=r"^/")
    query_parameters: list[QueryParameterIR] = Field(default_factory=list)
    pagination: PaginationIR | None = None
    response_type: str | None = None
    response_evidence: (
        Literal[
            "parser_path",
            "parser_call",
            "parser_default",
            "parser_role",
            "name_match",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def query_parameter_names_are_unique(self) -> ListingEndpointIR:
        names = [parameter.name for parameter in self.query_parameters]
        if len(names) != len(set(names)):
            raise ValueError(f"listing endpoint {self.id} has duplicate query parameters")
        if (self.response_type is None) != (self.response_evidence is None):
            raise ValueError("listing response type and evidence must be declared together")
        return self


class DataFieldIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    serialized_name: str
    source_type: str


class DataShapeIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    fields: list[DataFieldIR] = Field(min_length=1)


class ListingContainerIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type_name: str
    envelope_path: str | None = None
    items_path: str
    item_type: str
    item_wrapper_path: str | None = None
    manga_item_type: str
    next_path: str | None = None
    limit_path: str | None = None
    offset_path: str | None = None
    total_path: str | None = None


class MangaMappingIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_type: str
    key_template: str | None = None
    title_path: str | None = None
    cover_path: str | None = None
    authors_path: str | None = None
    tags_path: str | None = None
    description_path: str | None = None
    unresolved_fields: list[Literal["key", "title", "cover", "authors", "tags", "description"]] = (
        Field(default_factory=list)
    )


class ListingSelectionIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_endpoint_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    setting_key: str | None = None
    setting_default: str | None = None
    endpoint_ids_by_setting_value: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def setting_contract_is_complete(self) -> ListingSelectionIR:
        has_setting = self.setting_key is not None
        if has_setting != (self.setting_default is not None):
            raise ValueError("listing selection setting key and default must be declared together")
        if not has_setting and self.endpoint_ids_by_setting_value:
            raise ValueError("listing selection values require a setting key")
        if has_setting and self.setting_default not in self.endpoint_ids_by_setting_value:
            raise ValueError("listing selection default must map to an endpoint")
        return self


class ListingFilterActivationIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filter_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    default_value: str


class ListingConditionalRouteIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    activate_when_any: list[ListingFilterActivationIR] = Field(min_length=1)


class ListingSearchDispatchIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_endpoint_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    default_endpoint_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    conditional_routes: list[ListingConditionalRouteIR] = Field(default_factory=list)


class ListingProviderIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    popular_endpoint_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    latest: ListingSelectionIR | None = None


class ListingImplementationIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_base: ApiBaseIR
    endpoints: list[ListingEndpointIR] = Field(default_factory=list)
    data_shapes: list[DataShapeIR] = Field(default_factory=list)
    containers: list[ListingContainerIR] = Field(default_factory=list)
    manga_mappings: list[MangaMappingIR] = Field(default_factory=list)
    search_dispatch: ListingSearchDispatchIR | None = None
    provider: ListingProviderIR | None = None

    @model_validator(mode="after")
    def identities_are_unique(self) -> ListingImplementationIR:
        endpoint_ids = [endpoint.id for endpoint in self.endpoints]
        if len(endpoint_ids) != len(set(endpoint_ids)):
            raise ValueError("listing endpoint ids must be unique")
        shape_names = [shape.name for shape in self.data_shapes]
        if len(shape_names) != len(set(shape_names)):
            raise ValueError("listing data shape names must be unique")
        container_names = [container.type_name for container in self.containers]
        if len(container_names) != len(set(container_names)):
            raise ValueError("listing container type names must be unique")
        mapping_types = [mapping.item_type for mapping in self.manga_mappings]
        if len(mapping_types) != len(set(mapping_types)):
            raise ValueError("listing manga mapping item types must be unique")
        endpoint_ids = set(endpoint_ids)
        dispatch_ids: set[str] = set()
        if self.search_dispatch is not None:
            dispatch_ids.add(self.search_dispatch.query_endpoint_id)
            dispatch_ids.add(self.search_dispatch.default_endpoint_id)
            dispatch_ids.update(
                route.endpoint_id for route in self.search_dispatch.conditional_routes
            )
        if missing := dispatch_ids - endpoint_ids:
            raise ValueError(
                "listing search dispatch references unknown endpoints: "
                + ", ".join(sorted(missing))
            )
        provider_ids = set()
        if self.provider is not None:
            if self.provider.popular_endpoint_id is not None:
                provider_ids.add(self.provider.popular_endpoint_id)
            if self.provider.latest is not None:
                provider_ids.add(self.provider.latest.default_endpoint_id)
                provider_ids.update(self.provider.latest.endpoint_ids_by_setting_value.values())
        if missing := provider_ids - endpoint_ids:
            raise ValueError(
                "listing provider references unknown endpoints: " + ", ".join(sorted(missing))
            )
        return self


class ImplementationIR(BaseModel):
    """Provider-independent implementation facts used by deterministic Rust templates."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    source_id: str
    listing: ListingImplementationIR | None = None
    unresolved_facts: list[str] = Field(default_factory=list)


def _decode_java_string(value: str) -> str:
    try:
        decoded = json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value.replace(r"\"", '"').replace("\\\\", "\\")
    return decoded if isinstance(decoded, str) else value


def _snake_case(value: str) -> str:
    value = re.sub(r"(?<!^)(?=[A-Z])", "_", value)
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _brace_block(content: str, start: int) -> str:
    opening = content.find("{", start)
    if opening < 0:
        return ""
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(content)):
        char = content[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return content[start : index + 1]
    return ""


def _java_string_methods(content: str) -> list[tuple[str, tuple[str, ...], str]]:
    methods: list[tuple[str, tuple[str, ...], str]] = []
    declaration = re.compile(
        r"\b(?:public|protected|private)\s+(?:static\s+)?(?:final\s+)?"
        r"String\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*\{"
    )
    for found in declaration.finditer(content):
        parameters = tuple(
            parameter.group(1)
            for raw in found.group(2).split(",")
            if (parameter := re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*$", raw.strip()))
        )
        block = _brace_block(content, found.start())
        if block:
            methods.append((found.group(1), parameters, block))
    return methods


def _return_expression(block: str) -> str | None:
    found = re.search(r"\breturn\s+([\s\S]*?);", block)
    return " ".join(found.group(1).split()) if found else None


def _split_java_concatenation(expression: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    for index, char in enumerate(expression):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "+" and depth == 0:
            parts.append(expression[start:index].strip())
            start = index + 1
    parts.append(expression[start:].strip())
    return [part for part in parts if part]


def _string_literal(token: str) -> str | None:
    found = re.fullmatch(r'"((?:\\.|[^"\\])*)"', token.strip())
    return _decode_java_string(found.group(1)) if found else None


def _api_base(ir: SourceIR, java: str) -> ApiBaseIR:
    path_prefix = ""
    dynamic = False
    for name, _parameters, block in _java_string_methods(java):
        if name.casefold() not in {"getapiurl", "getapibaseurl", "apiurl"}:
            continue
        expression = _return_expression(block)
        if expression is None:
            continue
        literals = [
            literal
            for token in _split_java_concatenation(expression)
            if (literal := _string_literal(token)) is not None
        ]
        path_prefix = next((literal for literal in literals if literal.startswith("/api/")), "")
        dynamic = bool(re.search(r"Preferences|preferences|defaults_get|settings", expression))
        break

    setting_matches = list(
        re.finditer(
            r'\b(?:KEY|API_DOMAIN_KEY)\s*=\s*"([^"]*(?:api|domain)[^"]*)"',
            java,
            re.IGNORECASE,
        )
    )
    setting_match = next(
        (
            found
            for found in setting_matches
            if "domain" in found.group(1).casefold() and "custom" not in found.group(1).casefold()
        ),
        setting_matches[0] if setting_matches else None,
    )
    setting_key = setting_match.group(1) if setting_match else None

    default_scope = java
    if setting_match is not None:
        declarations = list(
            re.finditer(
                r"\b(?:class|enum)\s+[A-Za-z_][A-Za-z0-9_]*[^\{]*\{",
                java[: setting_match.start()],
            )
        )
        if declarations:
            scoped = _brace_block(java, declarations[-1].start())
            if scoped:
                default_scope = scoped

    default_host = None
    direct_default = re.search(
        r'\bDEFAULT\s*=\s*"((?:\\.|[^"\\])+)"\s*;',
        default_scope,
    )
    if direct_default:
        default_host = _decode_java_string(direct_default.group(1))
    else:
        symbolic_default = re.search(
            r"\bDEFAULT\s*=\s*([A-Z][A-Z0-9_]*)\.(?:entryKey|entry)\s*;",
            default_scope,
        )
        if symbolic_default:
            constant = symbolic_default.group(1)
            enum_value = re.search(
                rf"\b{re.escape(constant)}\s*\(\s*\"([^\"]+)\""
                rf"(?:\s*,\s*\"([^\"]+)\")?",
                default_scope,
            )
            if enum_value:
                default_host = enum_value.group(2) or enum_value.group(1)

    candidates = list(
        dict.fromkeys(
            domain
            for profile in ir.request_header_profiles
            for domain in profile.domains
            if domain != "custom"
        )
    )
    if default_host and default_host not in candidates:
        candidates.insert(0, default_host)
    return ApiBaseIR(
        scheme="https",
        path_prefix=path_prefix,
        dynamic=dynamic or bool(setting_key),
        setting_key=setting_key,
        default_host=default_host,
        candidate_hosts=candidates,
    )


def _api_repo_method_calls(content: str) -> list[str]:
    return re.findall(
        r"ApiRepo\.INSTANCE\.([A-Za-z_][A-Za-z0-9_]*)\s*\(",
        content,
    )


def _search_request_branches(search_block: str) -> list[tuple[str, str, list[str]]]:
    result: list[tuple[str, str, list[str]]] = []
    for branch in re.finditer(
        r"\b(?:else\s+)?if\s*\((?P<condition>[^{}]+)\)\s*\{",
        search_block,
    ):
        block = _brace_block(search_block, branch.start())
        result.append((branch.group("condition"), block, _api_repo_method_calls(block)))
    return result


def _request_method_roles(java: str) -> dict[str, ListingRole]:
    candidates: dict[str, set[ListingRole]] = {}

    def add(block: str, role: ListingRole) -> None:
        for method_name in _api_repo_method_calls(block):
            candidates.setdefault(method_name, set()).add(role)

    add(_java_method_block(java, "popularMangaRequest"), ListingRole.POPULAR)
    add(_java_method_block(java, "latestUpdatesRequest"), ListingRole.LATEST)
    search_block = _java_method_block(java, "searchMangaRequest")
    branched: set[str] = set()
    for condition, _block, methods in _search_request_branches(search_block):
        role = ListingRole.SEARCH if re.search(r"\bquery\b", condition) else ListingRole.RANK
        for method_name in methods:
            branched.add(method_name)
            candidates.setdefault(method_name, set()).add(role)
    for method_name in _api_repo_method_calls(search_block):
        if method_name not in branched:
            candidates.setdefault(method_name, set()).add(ListingRole.BROWSE)
    return {
        method_name: next(iter(roles))
        for method_name, roles in candidates.items()
        if len(roles) == 1
    }


def _listing_role(
    method_name: str,
    path: str,
    call_role: ListingRole | None,
) -> ListingRole | None:
    value = f"{method_name} {path}".casefold()
    if any(marker in value for marker in ("member", "collect", "comment", "theme/count")):
        return None
    if call_role is not None:
        return call_role
    if "search" in value:
        return ListingRole.SEARCH
    if "rank" in value:
        return ListingRole.RANK
    if any(marker in value for marker in ("recommend", "/recs", "popular")):
        return ListingRole.POPULAR
    if any(marker in value for marker in ("newest", "latest", "/update/")):
        return ListingRole.LATEST
    if any(marker in value for marker in ("comiclist", "/comics")):
        return ListingRole.BROWSE
    return None


def _expression_path(
    expression: str,
    *,
    parameters: tuple[str, ...],
    base_path: str,
) -> str | None:
    result = ""
    for token in _split_java_concatenation(expression):
        literal = _string_literal(token)
        if literal is not None:
            result += literal
        elif re.search(r"\b(?:getApiUrl|getApiBaseUrl|apiUrl)\s*\(", token):
            result += base_path
        elif "page" in parameters and re.search(
            r"\(\s*page\s*-\s*1\s*\)\s*\*\s*[A-Za-z_][A-Za-z0-9_]*",
            token,
        ):
            result += "{offset}"
        elif token in parameters:
            result += f"{{{_snake_case(token)}}}"
        else:
            parameter = next(
                (name for name in parameters if re.search(rf"\b{re.escape(name)}\b", token)),
                None,
            )
            if parameter:
                result += f"{{{_snake_case(parameter)}}}"
    return result if result.startswith("/") else None


def _parameter_source(value: str) -> Literal["static", "page", "query", "filter", "unknown"]:
    if "{" not in value:
        return "static"
    if value in {"{offset}", "{page}"}:
        return "page"
    if value in {"{query}", "{q}"}:
        return "query"
    if value.startswith("{filter:"):
        return "filter"
    return "unknown"


def _query_parameters(query: str) -> list[QueryParameterIR]:
    parameters: list[QueryParameterIR] = []
    for item in query.split("&"):
        name, separator, value = item.partition("=")
        if not separator or not name:
            continue
        parameters.append(
            QueryParameterIR(
                name=name,
                value_template=value,
                source=_parameter_source(value),
            )
        )
    return parameters


def _brace_depth(content: str, end: int) -> int:
    depth = 0
    quote: str | None = None
    escaped = False
    for char in content[:end]:
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
    return depth


def _filter_id_from_evidence(
    expression: str,
    *,
    preceding: str,
    filter_specs: tuple[SourceFilterSpec, ...],
) -> str | None:
    by_name: dict[str, str] = {}
    for spec in filter_specs:
        by_name[spec.id] = spec.id
        by_name[_snake_case(spec.source_class.removesuffix("Filter"))] = spec.id

    def accessor_id(content: str) -> str | None:
        accessors = re.findall(r"\bget([A-Za-z_][A-Za-z0-9_]*)Filter\s*\(", content)
        if not accessors:
            return None
        recovered = _snake_case(accessors[-1])
        return by_name.get(recovered, recovered)

    if recovered := accessor_id(expression):
        return recovered

    normalized_expression = _snake_case(expression.strip())
    for suffix in ("_value", "_index", "_state"):
        normalized_expression = normalized_expression.removesuffix(suffix)
    if normalized_expression in by_name:
        return by_name[normalized_expression]
    for name, filter_id in by_name.items():
        if re.search(rf"\b{re.escape(name)}\b", _snake_case(expression)):
            return filter_id

    if recovered := accessor_id(preceding):
        return recovered

    option_matches = {
        spec.id
        for spec in filter_specs
        if any(option.value and f'"{option.value}"' in preceding for option in spec.options)
    }
    return next(iter(option_matches)) if len(option_matches) == 1 else None


def _query_binding_value(
    expression: str,
    *,
    preceding: str,
    filter_specs: tuple[SourceFilterSpec, ...],
) -> tuple[str, Literal["static", "query", "filter", "unknown"]]:
    literal = _string_literal(expression.strip())
    if literal is not None:
        return literal, "static"
    if expression.strip() == "query":
        return "{query}", "query"
    filter_id = _filter_id_from_evidence(
        expression,
        preceding=preceding,
        filter_specs=filter_specs,
    )
    if filter_id is not None:
        return f"{{filter:{filter_id}}}", "filter"
    return f"{{{_snake_case(expression.strip())}}}", "unknown"


def _request_query_bindings(
    java: str,
    filter_specs: tuple[SourceFilterSpec, ...],
) -> dict[str, list[QueryParameterIR]]:
    """Associate builder query additions with the API helper selected by each branch."""
    bindings: dict[str, list[QueryParameterIR]] = {}
    method = re.search(r"\bsearchMangaRequest\s*\([^)]*\)\s*\{", java)
    if method is None:
        return bindings
    block = _brace_block(java, method.start())
    references = list(
        re.finditer(r"ApiRepo\.INSTANCE\.([A-Za-z_][A-Za-z0-9_]*)\s*\([^;]*?\)", block)
    )
    if not references:
        return bindings
    for index, reference in enumerate(references):
        end = references[index + 1].start() if index + 1 < len(references) else len(block)
        segment = block[reference.end() : end]
        reference_depth = _brace_depth(block, reference.start())
        values = bindings.setdefault(reference.group(1), [])
        previous_binding_end = 0
        for found in re.finditer(
            r'\.addQueryParameter\(\s*"([^"]+)"\s*,\s*([^;]+?)\s*\)\s*;',
            segment,
        ):
            value, source = _query_binding_value(
                found.group(2),
                preceding=segment[previous_binding_end : found.start()],
                filter_specs=filter_specs,
            )
            previous_binding_end = found.end()
            values.append(
                QueryParameterIR(
                    name=found.group(1),
                    value_template=value,
                    source=source,
                    required=(
                        _brace_depth(block, reference.end() + found.start()) <= reference_depth
                    ),
                )
            )
    common = next(
        (
            QueryParameterIR(
                name=found.group(1),
                value_template=(
                    value := _query_binding_value(
                        found.group(2),
                        preceding=block[: found.start()],
                        filter_specs=filter_specs,
                    )
                )[0],
                source=value[1],
            )
            for found in re.finditer(
                r'\.addQueryParameter\(\s*"(_[^"]+)"\s*,\s*([^;]+?)\s*\)\s*;',
                block,
            )
        ),
        None,
    )
    if common is not None:
        for values in bindings.values():
            if all(parameter.name != common.name for parameter in values):
                values.append(common.model_copy(deep=True))
    return bindings


def _pagination(
    parameters: list[QueryParameterIR],
    *,
    page_size: int | None,
) -> PaginationIR | None:
    by_name = {parameter.name: parameter for parameter in parameters}
    offset = next(
        (parameter for parameter in parameters if parameter.value_template == "{offset}"),
        None,
    )
    if offset is not None:
        limit = by_name.get("limit")
        detected_size = (
            int(limit.value_template)
            if limit is not None and limit.value_template.isdecimal()
            else page_size
        )
        return PaginationIR(
            kind="offset",
            page_parameter=offset.name,
            page_size=detected_size,
            page_size_parameter="limit" if limit is not None else None,
            offset_formula="(page - 1) * page_size",
        )
    page = next(
        (parameter for parameter in parameters if parameter.value_template == "{page}"),
        None,
    )
    if page is not None:
        return PaginationIR(
            kind="page",
            page_parameter=page.name,
            page_size=page_size,
        )
    return None


def _listing_endpoints(
    java: str,
    base: ApiBaseIR,
    filter_specs: tuple[SourceFilterSpec, ...],
) -> list[ListingEndpointIR]:
    page_size_match = re.search(
        r"\b(?:pageSize|PAGE_SIZE)\s*=\s*(\d+)\s*;",
        java,
    )
    page_size = int(page_size_match.group(1)) if page_size_match else None
    builder_bindings = _request_query_bindings(java, filter_specs)
    call_roles = _request_method_roles(java)
    endpoints: list[ListingEndpointIR] = []
    seen_ids: set[str] = set()
    for name, parameters, block in _java_string_methods(java):
        expression = _return_expression(block)
        if expression is None:
            continue
        template = _expression_path(
            expression,
            parameters=parameters,
            base_path=base.path_prefix,
        )
        if template is None:
            continue
        role = _listing_role(name, template, call_roles.get(name))
        if role is None:
            continue
        path, _, query = template.partition("?")
        query_parameters = _query_parameters(query)
        for binding in builder_bindings.get(name, []):
            query_parameters = [
                parameter for parameter in query_parameters if parameter.name != binding.name
            ]
            query_parameters.append(binding)
        endpoint_id = _snake_case(name.removesuffix("Url"))
        if endpoint_id in seen_ids:
            continue
        seen_ids.add(endpoint_id)
        endpoints.append(
            ListingEndpointIR(
                id=endpoint_id,
                role=role,
                source_method=name,
                path=path,
                query_parameters=query_parameters,
                pagination=_pagination(query_parameters, page_size=page_size),
            )
        )
    return endpoints


def _listing_search_dispatch(
    java: str,
    endpoints: list[ListingEndpointIR],
    filter_specs: tuple[SourceFilterSpec, ...],
) -> ListingSearchDispatchIR | None:
    request_block = _java_method_block(java, "searchMangaRequest")
    if not request_block:
        return None
    specs = {spec.id: spec for spec in filter_specs}
    specs_by_class = {spec.source_class: spec for spec in filter_specs}
    state_variables: dict[str, str] = {}
    for assignment in re.finditer(
        r"\b(?:int|Integer)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^;]+);",
        request_block,
    ):
        matched = [
            spec
            for source_class, spec in specs_by_class.items()
            if re.search(rf"\b{re.escape(source_class)}\b", assignment.group(2))
        ]
        if len(matched) == 1:
            state_variables[assignment.group(1)] = matched[0].id

    def filter_for_variable(variable: str) -> SourceFilterSpec | None:
        filter_id = state_variables.get(variable)
        if filter_id is None:
            normalized = _snake_case(variable)
            for suffix in ("_index", "_state", "_value"):
                normalized = normalized.removesuffix(suffix)
            candidates = [
                spec.id
                for spec in filter_specs
                if normalized
                in {
                    spec.id,
                    _snake_case(spec.source_class.removesuffix("Filter")),
                }
            ]
            filter_id = candidates[0] if len(candidates) == 1 else None
        return specs.get(filter_id) if filter_id is not None else None

    def condition_filters(condition: str) -> list[SourceFilterSpec]:
        result: list[SourceFilterSpec] = []
        for clause in condition.split("||"):
            comparison = re.fullmatch(
                r"\s*\(*\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:>|!=)\s*0\s*\)*\s*",
                clause,
            )
            if comparison is None:
                return []
            spec = filter_for_variable(comparison.group(1))
            if spec is None or spec.default_index != 0 or spec in result:
                return []
            result.append(spec)
        return result

    query_endpoint_id: str | None = None
    routes: list[ListingConditionalRouteIR] = []
    branched_endpoint_ids: set[str] = set()
    for condition, branch_block, _methods in _search_request_branches(request_block):
        called = _called_listing_endpoints(branch_block, endpoints)
        if len(called) > 1:
            return None
        if not called:
            continue
        endpoint = called[0]
        branched_endpoint_ids.add(endpoint.id)
        if re.search(r"\bquery\b", condition):
            compact = re.sub(r"\s+", "", condition)
            nonempty_query = any(
                re.fullmatch(pattern, compact)
                for pattern in (
                    r"!query\.(?:isBlank|isEmpty)\(\)",
                    r"!StringsKt\.isBlank\(query\)",
                )
            )
            if query_endpoint_id is not None or not nonempty_query:
                return None
            query_endpoint_id = endpoint.id
            continue
        branch_specs = condition_filters(condition)
        bound_filter_ids = {
            binding.group(1)
            for parameter in endpoint.query_parameters
            if parameter.source == "filter"
            and (
                binding := re.fullmatch(
                    r"\{filter:([a-z][a-z0-9_]*)\}",
                    parameter.value_template,
                )
            )
            is not None
        }
        if not branch_specs or any(
            spec.kind != "select" or spec.id not in bound_filter_ids for spec in branch_specs
        ):
            return None
        routes.append(
            ListingConditionalRouteIR(
                endpoint_id=endpoint.id,
                activate_when_any=[
                    ListingFilterActivationIR(
                        filter_id=spec.id,
                        default_value=spec.options[spec.default_index].value,
                    )
                    for spec in branch_specs
                ],
            )
        )
    default = [
        endpoint
        for endpoint in _called_listing_endpoints(request_block, endpoints)
        if endpoint.id not in branched_endpoint_ids
    ]
    if query_endpoint_id is None or len(default) != 1:
        return None
    return ListingSearchDispatchIR(
        query_endpoint_id=query_endpoint_id,
        default_endpoint_id=default[0].id,
        conditional_routes=routes,
    )


def _java_method_block(java: str, name: str) -> str:
    declaration = re.search(rf"\b{re.escape(name)}\s*\([^)]*\)\s*(?:throws\s+[^\{{]+)?\{{", java)
    return _brace_block(java, declaration.start()) if declaration is not None else ""


def _called_listing_endpoints(
    block: str,
    endpoints: list[ListingEndpointIR],
) -> list[ListingEndpointIR]:
    return [
        endpoint
        for endpoint in endpoints
        if re.search(rf"\.{re.escape(endpoint.source_method)}\s*\(", block)
    ]


def _option_class_block(java: str, class_name: str) -> str:
    declaration = re.search(rf"\b(?:class|enum)\s+{re.escape(class_name)}\b[^\{{]*\{{", java)
    return _brace_block(java, declaration.start()) if declaration is not None else ""


def _latest_setting_selection(
    java: str,
    request_block: str,
    endpoints: list[ListingEndpointIR],
) -> ListingSelectionIR | None:
    ordinal_indices = {
        (found.group("class"), found.group("option")): int(found.group("index"))
        for found in re.finditer(
            r"(?P<class>[A-Z][A-Za-z0-9_]*)\.(?P<option>[A-Z][A-Z0-9_]*)"
            r"\.ordinal\(\)\]\s*=\s*(?P<index>\d+)\s*;",
            java,
        )
    }
    option_class_blocks = {
        class_name: _option_class_block(java, class_name) for class_name, _option in ordinal_indices
    }
    latest_option_classes = [
        class_name
        for class_name, block in option_class_blocks.items()
        if re.search(r'\bKEY\s*=\s*"[^"]*latest[^"]*"', block, re.IGNORECASE)
    ]
    if len(latest_option_classes) != 1:
        return None
    option_class = latest_option_classes[0]
    class_block = option_class_blocks[option_class]
    if not class_block:
        return None
    key_match = re.search(r'\bKEY\s*=\s*"([^"]+)"', class_block)
    if key_match is None:
        return None
    option_values = {
        found.group("option"): found.group("value")
        for found in re.finditer(
            r'\b(?P<option>[A-Z][A-Z0-9_]*)\s*\(\s*"(?:\\.|[^"\\])*"\s*,\s*'
            r'"(?P<value>(?:\\.|[^"\\])+)"\s*\)',
            class_block,
        )
    }
    default_match = re.search(
        rf"\bDEFAULT\s*=\s*new\s+{re.escape(option_class)}\s*\("
        r'\s*"(?:\\.|[^"\\])*"\s*,\s*"(?P<value>(?:\\.|[^"\\])+)"\s*\)'
        r"\.entryKey",
        class_block,
    )
    symbolic_default = re.search(
        r"\bDEFAULT\s*=\s*(?P<option>[A-Z][A-Z0-9_]*)\.entryKey",
        class_block,
    )
    if default_match is not None:
        default_value = _decode_java_string(default_match.group("value"))
    elif symbolic_default is not None:
        default_value = option_values.get(symbolic_default.group("option"))
    else:
        default_value = None
    if default_value is None:
        return None

    endpoint_by_index: dict[int, str] = {}
    for branch in re.finditer(r"\bif\s*\(\s*[A-Za-z_]\w*\s*==\s*(\d+)\s*\)\s*\{", request_block):
        branch_block = _brace_block(request_block, branch.start())
        called = _called_listing_endpoints(branch_block, endpoints)
        if len(called) == 1:
            endpoint_by_index[int(branch.group(1))] = called[0].id
    choices = {
        value: endpoint_by_index[index]
        for (class_name, option), index in ordinal_indices.items()
        if class_name == option_class
        and (value := option_values.get(option)) is not None
        and index in endpoint_by_index
    }
    if len(choices) != len(endpoints) or default_value not in choices:
        return None
    return ListingSelectionIR(
        default_endpoint_id=choices[default_value],
        setting_key=key_match.group(1),
        setting_default=default_value,
        endpoint_ids_by_setting_value=choices,
    )


def _listing_provider(
    java: str,
    endpoints: list[ListingEndpointIR],
) -> ListingProviderIR | None:
    popular_block = _java_method_block(java, "popularMangaRequest")
    popular = _called_listing_endpoints(popular_block, endpoints)
    popular_endpoint = popular[0] if len(popular) == 1 else None

    latest_block = _java_method_block(java, "latestUpdatesRequest")
    latest_endpoints = _called_listing_endpoints(latest_block, endpoints)
    latest: ListingSelectionIR | None = None
    if len(latest_endpoints) == 1:
        latest = ListingSelectionIR(default_endpoint_id=latest_endpoints[0].id)
    elif len(latest_endpoints) > 1:
        latest = _latest_setting_selection(java, latest_block, latest_endpoints)
    if popular_endpoint is None and latest is None:
        return None
    return ListingProviderIR(
        popular_endpoint_id=popular_endpoint.id if popular_endpoint is not None else None,
        latest=latest,
    )


def _listing_parser_facts(
    java: str, container_names: set[str]
) -> list[tuple[str, str, str | None]]:
    """Return parser name, response container, and optional URL discriminator."""
    declaration = re.compile(
        r"\b(?:public|protected)\s+(?:final\s+)?MangasPage\s+"
        r"([A-Za-z_][A-Za-z0-9_]*Parse)\s*\([^)]*\)\s*(?:throws\s+[^\{]+)?\{"
    )
    facts: list[tuple[str, str, str | None]] = []
    for method in declaration.finditer(java):
        block = _brace_block(java, method.start())
        types = [
            found
            for found in re.finditer(
                r"Reflection\.typeOf\(\s*([A-Za-z_][A-Za-z0-9_]*)\.class\s*\)",
                block,
            )
            if found.group(1) in container_names
        ]
        previous = 0
        for found in types:
            prefix = block[previous : found.start()]
            markers = re.findall(
                r"contains[^\n;]{0,300}?\"(/[^\"]+)\"",
                prefix,
            )
            facts.append(
                (
                    method.group(1),
                    found.group(1),
                    markers[-1] if markers else None,
                )
            )
            previous = found.end()
    return facts


def _parser_role(parser_name: str) -> ListingRole | None:
    value = parser_name.casefold()
    if "popular" in value:
        return ListingRole.POPULAR
    if "latest" in value or "newest" in value:
        return ListingRole.LATEST
    if "search" in value:
        return ListingRole.SEARCH
    return None


def _name_matched_container(
    endpoint: ListingEndpointIR,
    container_names: set[str],
) -> str | None:
    preferred: tuple[str, ...]
    if endpoint.role == ListingRole.SEARCH:
        preferred = ("search",)
    elif endpoint.role == ListingRole.RANK:
        preferred = ("rank",)
    elif endpoint.role == ListingRole.POPULAR:
        preferred = ("recommend", "popular")
    elif endpoint.role == ListingRole.LATEST and "newest" in endpoint.path.casefold():
        preferred = ("newest", "latest")
    else:
        preferred = ("comicslist", "comiclist", "mangalist")
    matches = [
        name for name in container_names if any(marker in name.casefold() for marker in preferred)
    ]
    return matches[0] if len(matches) == 1 else None


def _associate_listing_responses(
    endpoints: list[ListingEndpointIR],
    containers: list[ListingContainerIR],
    java: str,
    search_dispatch: ListingSearchDispatchIR | None,
) -> list[ListingEndpointIR]:
    container_names = {container.type_name for container in containers}
    facts = _listing_parser_facts(java, container_names)
    parser_endpoint_ids = {
        parser: {
            endpoint.id
            for endpoint in _called_listing_endpoints(
                _java_method_block(java, f"{parser.removesuffix('Parse')}Request"),
                endpoints,
            )
        }
        for parser, _type_name, _marker in facts
    }
    associated: list[ListingEndpointIR] = []
    for endpoint in endpoints:
        relevant = [
            fact for fact in facts if endpoint.id in parser_endpoint_ids.get(fact[0], set())
        ]
        explicit = {
            type_name
            for _parser, type_name, marker in relevant
            if marker is not None and marker in endpoint.path
        }
        if len(explicit) == 1:
            associated.append(
                endpoint.model_copy(
                    update={
                        "response_type": explicit.pop(),
                        "response_evidence": "parser_path",
                    }
                )
            )
            continue
        direct = {
            type_name
            for parser, type_name, _marker in relevant
            if len(parser_endpoint_ids.get(parser, set())) == 1
        }
        if len(direct) == 1:
            associated.append(
                endpoint.model_copy(
                    update={
                        "response_type": direct.pop(),
                        "response_evidence": "parser_call",
                    }
                )
            )
            continue
        default_types = {
            type_name
            for parser, type_name, marker in relevant
            if marker is None
            and search_dispatch is not None
            and endpoint.id == search_dispatch.default_endpoint_id
            and endpoint.id in parser_endpoint_ids.get(parser, set())
        }
        if len(default_types) == 1:
            associated.append(
                endpoint.model_copy(
                    update={
                        "response_type": default_types.pop(),
                        "response_evidence": "parser_default",
                    }
                )
            )
            continue
        role_types = {
            type_name
            for parser, type_name, marker in relevant
            if marker is None and _parser_role(parser) == endpoint.role
        }
        if len(role_types) == 1:
            associated.append(
                endpoint.model_copy(
                    update={
                        "response_type": role_types.pop(),
                        "response_evidence": "parser_role",
                    }
                )
            )
            continue
        matched = _name_matched_container(endpoint, container_names)
        associated.append(
            endpoint.model_copy(
                update={
                    "response_type": matched,
                    "response_evidence": "name_match" if matched else None,
                }
            )
        )
    return associated


def _data_shapes(shapes: Iterable[DecompiledDtoShape]) -> list[DataShapeIR]:
    return [
        DataShapeIR(
            name=shape.name,
            fields=[
                DataFieldIR(
                    name=field.name,
                    serialized_name=field.serialized_name,
                    source_type=field.java_type,
                )
                for field in shape.fields
            ],
        )
        for shape in shapes
    ]


def _list_item_type(source_type: str) -> str | None:
    found = re.fullmatch(r"(?:java\.util\.)?List<\s*([^<>]+?)\s*>", source_type)
    return found.group(1).strip() if found else None


def _response_envelope_paths(
    java: str,
    shapes: tuple[DecompiledDtoShape, ...],
) -> dict[str, str]:
    generic_fields = {
        shape.name: field.serialized_name
        for shape in shapes
        for field in shape.fields
        if field.java_type == "T"
    }
    candidates: dict[str, set[str]] = {}
    for found in re.finditer(
        r"Reflection\.typeOf\(\s*([A-Za-z_][A-Za-z0-9_]*)\.class"
        r"[\s\S]{0,500}?Reflection\.typeOf\(\s*([A-Za-z_][A-Za-z0-9_]*)\.class",
        java,
    ):
        envelope_path = generic_fields.get(found.group(1))
        if envelope_path is not None:
            candidates.setdefault(found.group(2), set()).add(envelope_path)
    return {
        response_type: next(iter(paths))
        for response_type, paths in candidates.items()
        if len(paths) == 1
    }


def _listing_containers(
    shapes: tuple[DecompiledDtoShape, ...],
    *,
    envelope_paths: dict[str, str],
) -> list[ListingContainerIR]:
    by_name = {shape.name: shape for shape in shapes}
    containers: list[ListingContainerIR] = []
    for shape in shapes:
        if re.search(r"(?:Chapter|Comment|Content|Detail|Theme)", shape.name):
            continue
        list_field = next(
            (
                (field, item_type)
                for field in shape.fields
                if (item_type := _list_item_type(field.java_type)) is not None
                and field.serialized_name in {"list", "items", "comics", "results", "data"}
            ),
            None,
        )
        if list_field is None:
            continue
        field, item_type = list_field
        names = {item.serialized_name for item in shape.fields}
        next_field = next(
            (item for item in shape.fields if item.serialized_name == "next"),
            None,
        )
        looks_like_container = bool(names & {"limit", "offset", "total"}) or shape.name.endswith(
            ("Result", "List", "Page")
        )
        if not looks_like_container:
            continue
        item_shape = by_name.get(item_type)
        wrapper = None
        manga_item_type = item_type
        if item_shape is not None:
            wrapper_field = next(
                (
                    item
                    for item in item_shape.fields
                    if item.serialized_name == "comic" or item.name == "comic"
                ),
                None,
            )
            if wrapper_field is not None:
                wrapper = wrapper_field.serialized_name
                manga_item_type = wrapper_field.java_type
        containers.append(
            ListingContainerIR(
                type_name=shape.name,
                envelope_path=envelope_paths.get(shape.name),
                items_path=field.serialized_name,
                item_type=item_type,
                item_wrapper_path=wrapper,
                manga_item_type=manga_item_type,
                next_path=(
                    next_field.serialized_name
                    if next_field is not None
                    and next_field.java_type in {"String", "java.lang.String"}
                    else None
                ),
                limit_path="limit" if "limit" in names else None,
                offset_path="offset" if "offset" in names else None,
                total_path="total" if "total" in names else None,
            )
        )
    return containers


def _serialized_field(shape: DecompiledDtoShape, java_name: str) -> str:
    field = next((field for field in shape.fields if field.name == java_name), None)
    return field.serialized_name if field is not None else java_name


def _setter_argument(block: str, setter: str) -> str | None:
    calls = list(re.finditer(rf"\b{re.escape(setter)}\s*\(", block))
    if len(calls) != 1 or _brace_depth(block, calls[0].start()) != 1:
        return None
    opening = block.find("(", calls[0].start())
    depth = 0
    quote: str | None = None
    escaped = False
    argument = ""
    for index in range(opening, len(block)):
        char = block[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in {'"', "'"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                argument = block[opening + 1 : index]
                break
    return argument or None


def _setter_field(block: str, setter: str) -> str | None:
    argument = _setter_argument(block, setter)
    found = re.fullmatch(r"\s*this\.([A-Za-z_][A-Za-z0-9_]*)\s*", argument or "")
    return found.group(1) if found else None


def _setter_projection_field(
    block: str,
    setter: str,
    *,
    allowed_wrapper_variables: frozenset[str] = frozenset(),
    allow_wrapper: bool = False,
) -> str | None:
    direct = _setter_field(block, setter)
    if direct is not None:
        return direct
    argument = _setter_argument(block, setter)
    if argument is None or len(_split_java_concatenation(argument)) != 1:
        return None
    fields = set(re.findall(r"\bthis\.([A-Za-z_][A-Za-z0-9_]*)", argument))
    has_evidence_variable = any(
        re.search(rf"\b{re.escape(variable)}\b", argument) for variable in allowed_wrapper_variables
    )
    return (
        next(iter(fields))
        if len(fields) == 1 and (allow_wrapper or has_evidence_variable)
        else None
    )


def _setter_path(
    block: str,
    shape: DecompiledDtoShape,
    setter: str,
    *,
    allowed_wrapper_variables: frozenset[str] = frozenset(),
    allow_wrapper: bool = False,
) -> str | None:
    field = _setter_projection_field(
        block,
        setter,
        allowed_wrapper_variables=allowed_wrapper_variables,
        allow_wrapper=allow_wrapper,
    )
    if field is None or not any(item.name == field for item in shape.fields):
        return None
    return field


def _string_template(argument: str, shape: DecompiledDtoShape) -> str | None:
    result = ""
    has_field = False
    for token in _split_java_concatenation(argument):
        literal = _string_literal(token)
        if literal is not None:
            if "{" in literal or "}" in literal:
                return None
            result += literal
            continue
        field_match = re.fullmatch(r"\s*this\.([A-Za-z_][A-Za-z0-9_]*)\s*", token)
        if field_match is None:
            return None
        field = next(
            (item for item in shape.fields if item.name == field_match.group(1)),
            None,
        )
        if field is None:
            return None
        result += f"{{{field.name}}}"
        has_field = True
    return result if has_field and result.startswith("/") and "://" not in result else None


def _setter_collection_path(
    block: str,
    shape: DecompiledDtoShape,
    setter: str,
    shapes: dict[str, DecompiledDtoShape],
    allowed_wrapper_variables: frozenset[str] = frozenset(),
) -> str | None:
    argument = _setter_argument(block, setter)
    if argument is None:
        return None
    fields = set(re.findall(r"\bthis\.([A-Za-z_][A-Za-z0-9_]*)", argument))
    if len(fields) != 1:
        return None
    java_name = next(iter(fields))
    field = next((item for item in shape.fields if item.name == java_name), None)
    if field is None:
        return None
    item_type = _list_item_type(field.java_type) if field is not None else None
    if item_type is None:
        return (
            java_name
            if field.java_type in {"String", "java.lang.String"}
            and _setter_projection_field(
                block,
                setter,
                allowed_wrapper_variables=allowed_wrapper_variables,
            )
            == java_name
            else None
        )
    if not re.match(
        r"\s*(?:(?:[A-Za-z_][A-Za-z0-9_$]*)\.)*"
        r"(?:join|joinToString(?:\$default)?)\s*\(",
        argument,
    ):
        return None
    if item_type in {"String", "java.lang.String"}:
        return f"{java_name}[]"
    item_shape = shapes.get(item_type)
    if item_shape is None:
        return None
    getter_fields = set()
    for _variable, getter in re.findall(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*->\s*\1\.get"
        r"([A-Z][A-Za-z0-9_]*)\s*\(\s*\)",
        argument,
    ):
        getter_fields.add(getter[:1].lower() + getter[1:])
    for variable, getter in re.findall(
        r"\breturn\s+([A-Za-z_][A-Za-z0-9_]*)\.get"
        r"([A-Z][A-Za-z0-9_]*)\s*\(\s*\)\s*;",
        argument,
    ):
        if re.search(rf"\b{re.escape(item_type)}\s+{re.escape(variable)}\b", argument):
            getter_fields.add(getter[:1].lower() + getter[1:])
    if allowed_wrapper_variables:
        has_evidence_variable = any(
            re.search(rf"\b{re.escape(variable)}\b", argument)
            for variable in allowed_wrapper_variables
        )
        if has_evidence_variable:
            for _variable, getter in re.findall(
                r"\b([A-Za-z_][A-Za-z0-9_]*)\s*->[^;{}]*?"
                r"\b\1\.get([A-Z][A-Za-z0-9_]*)\s*\(\s*\)",
                argument,
            ):
                getter_fields.add(getter[:1].lower() + getter[1:])
            for variable, getter in re.findall(
                r"\b([A-Za-z_][A-Za-z0-9_]*)\.get"
                r"([A-Z][A-Za-z0-9_]*)\s*\(\s*\)",
                argument,
            ):
                if re.search(rf"\b{re.escape(item_type)}\s+{re.escape(variable)}\b", argument):
                    getter_fields.add(getter[:1].lower() + getter[1:])
    children = [field for field in item_shape.fields if field.name in getter_fields]
    if len(children) != 1:
        return None
    return f"{java_name}[].{children[0].name}"


def _manga_mappings(
    files: Iterable[SourceFile],
    shapes: tuple[DecompiledDtoShape, ...],
    *,
    allowed_types: set[str],
    allow_script_conversion_fallback: bool,
    preserve_cover_urls: bool,
) -> list[MangaMappingIR]:
    by_name = {shape.name: shape for shape in shapes}
    mappings: list[MangaMappingIR] = []
    for source in files:
        if not source.path.endswith(".java") or "toSManga" not in source.content:
            continue
        type_name = PurePosixPath(source.path).stem
        if type_name not in allowed_types:
            continue
        shape = by_name.get(type_name)
        if shape is None:
            continue
        found = re.search(r"\btoSManga\s*\(([^)]*)\)\s*\{", source.content)
        if found is None:
            continue
        block = _brace_block(source.content, found.start())
        wrapper_variables = (
            frozenset(
                match.group(1)
                for match in re.finditer(
                    r"\bCCOption\s+([A-Za-z_][A-Za-z0-9_]*)",
                    found.group(1),
                )
            )
            if allow_script_conversion_fallback
            else frozenset()
        )
        url_argument = _setter_argument(block, "setUrl")
        key_template = _string_template(url_argument, shape) if url_argument else None
        projections = {
            "title": _setter_path(
                block,
                shape,
                "setTitle",
                allowed_wrapper_variables=wrapper_variables,
            ),
            "cover": _setter_path(
                block,
                shape,
                "setThumbnail_url",
                allow_wrapper=preserve_cover_urls,
            ),
            "authors": _setter_collection_path(
                block,
                shape,
                "setAuthor",
                by_name,
                wrapper_variables,
            ),
            "tags": _setter_collection_path(
                block,
                shape,
                "setGenre",
                by_name,
                wrapper_variables,
            ),
            "description": _setter_path(
                block,
                shape,
                "setDescription",
                allowed_wrapper_variables=wrapper_variables,
            ),
        }
        setters = {
            "key": "setUrl",
            "title": "setTitle",
            "cover": "setThumbnail_url",
            "authors": "setAuthor",
            "tags": "setGenre",
            "description": "setDescription",
        }
        unresolved_fields = []
        for field_name, setter in setters.items():
            projection = key_template if field_name == "key" else projections[field_name]
            calls = re.findall(rf"\b{re.escape(setter)}\s*\(", block)
            argument = _setter_argument(block, setter)
            is_empty = argument is not None and _string_literal(argument.strip()) == ""
            if calls and projection is None and not is_empty:
                unresolved_fields.append(field_name)

        mappings.append(
            MangaMappingIR(
                item_type=type_name,
                key_template=key_template,
                title_path=projections["title"],
                cover_path=projections["cover"],
                authors_path=projections["authors"],
                tags_path=projections["tags"],
                description_path=projections["description"],
                unresolved_fields=unresolved_fields,
            )
        )
    return mappings


def _listing_data_shapes(
    shapes: tuple[DecompiledDtoShape, ...],
    containers: list[ListingContainerIR],
) -> list[DataShapeIR]:
    """Keep only the DTO closure required by the deterministic listing slice."""
    by_name = {shape.name: shape for shape in shapes}
    selected = {
        *[container.type_name for container in containers],
        *[container.item_type for container in containers],
        *[container.manga_item_type for container in containers],
    }
    pending = list(selected)
    while pending:
        shape = by_name.get(pending.pop())
        if shape is None:
            continue
        for field in shape.fields:
            candidates = re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", field.java_type)
            for candidate in candidates:
                if candidate in by_name and candidate not in selected:
                    selected.add(candidate)
                    pending.append(candidate)
    return _data_shapes(shape for shape in shapes if shape.name in selected)


def project_implementation_ir(ir: SourceIR) -> ImplementationIR:
    """Project the deterministic listing slice without consulting an AI provider."""
    if ir.source_format != "decompiled_apk":
        return ImplementationIR(
            source_id=ir.metadata.source_id,
            unresolved_facts=[
                "deterministic listing projection is not implemented for Kotlin modules"
            ],
        )

    java = "\n\n".join(source.content for source in ir.files if source.path.endswith(".java"))
    shapes = decompiled_dto_shapes(ir.files)
    base = _api_base(ir, java)
    endpoints = _listing_endpoints(java, base, tuple(ir.filter_specs))
    containers = _listing_containers(
        shapes,
        envelope_paths=_response_envelope_paths(java, shapes),
    )
    search_dispatch = _listing_search_dispatch(java, endpoints, tuple(ir.filter_specs))
    endpoints = _associate_listing_responses(
        endpoints,
        containers,
        java,
        search_dispatch,
    )
    manga_item_types = {container.manga_item_type for container in containers}
    mappings = _manga_mappings(
        ir.files,
        shapes,
        allowed_types=manga_item_types,
        allow_script_conversion_fallback=(
            ir.feature_scope == "public_only"
            and any(
                "ChineseUtils script conversion" in feature for feature in ir.unsupported_features
            )
        ),
        preserve_cover_urls=(
            ir.image_url_policy is not None and ir.image_url_policy.preserve_cover_urls
        ),
    )
    unresolved: list[str] = []
    filter_ids = {spec.id for spec in ir.filter_specs}
    if not endpoints:
        unresolved.append("no deterministic listing endpoint template was recovered")
    if not containers:
        unresolved.append("no deterministic listing response container was recovered")
    if search_dispatch is None:
        unresolved.append("listing search dispatch is unresolved")
    for endpoint in endpoints:
        if endpoint.response_type is None:
            unresolved.append(
                f"listing response container is unresolved for endpoint {endpoint.id}"
            )
        elif endpoint.response_evidence == "name_match":
            unresolved.append(
                f"listing response container has only name evidence for endpoint {endpoint.id}"
            )
        for parameter in endpoint.query_parameters:
            if parameter.source == "unknown":
                unresolved.append(
                    f"listing query binding is unresolved for {endpoint.id}.{parameter.name}"
                )
            elif parameter.source == "filter" and parameter.required:
                binding = re.fullmatch(
                    r"\{filter:([a-z][a-z0-9_]*)\}",
                    parameter.value_template,
                )
                if binding is None or binding.group(1) not in filter_ids:
                    unresolved.append(
                        "required listing filter contract is unresolved for "
                        f"{endpoint.id}.{parameter.name}"
                    )
    if not mappings:
        unresolved.append("no deterministic manga field mapping was recovered")
    mapped_types = {mapping.item_type for mapping in mappings}
    for item_type in sorted(manga_item_types - mapped_types):
        unresolved.append(f"listing manga field mapping is unresolved for {item_type}")
    for mapping in mappings:
        for field in mapping.unresolved_fields:
            unresolved.append(
                f"listing manga {field} mapping is unresolved for {mapping.item_type}"
            )
    return ImplementationIR(
        source_id=ir.metadata.source_id,
        listing=ListingImplementationIR(
            api_base=base,
            endpoints=endpoints,
            data_shapes=_listing_data_shapes(shapes, containers),
            containers=containers,
            manga_mappings=mappings,
            search_dispatch=search_dispatch,
            provider=_listing_provider(java, endpoints),
        ),
        unresolved_facts=unresolved,
    )
