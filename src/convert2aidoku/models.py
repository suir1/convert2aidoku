from __future__ import annotations

import json
import re
from copy import deepcopy
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .constants import (
    MAX_GENERATED_FILE_CHARS,
    MAX_GENERATED_FILES,
    MAX_GENERATED_TOTAL_CHARS,
    MAX_REPAIR_PATCH_EDITS,
)
from .errors import SecurityError


class Capability(StrEnum):
    SEARCH = "search"
    POPULAR = "popular"
    LATEST = "latest"
    DETAILS = "details"
    CHAPTERS = "chapters"
    CONTEXTUAL_CHAPTER_URLS = "contextual_chapter_urls"
    PAGES = "pages"
    FILTERS = "filters"
    DYNAMIC_FILTERS = "dynamic_filters"
    SETTINGS = "settings"
    IMAGE_HEADERS = "image_headers"
    DEEP_LINKS = "deep_links"
    JSON_API = "json_api"
    ENCRYPTED_JSON = "encrypted_json"
    TRIPLE_DES_CBC = "triple_des_cbc"
    RSA_PKCS1_V15 = "rsa_pkcs1_v15"
    MD5_REQUEST_SIGNING = "md5_request_signing"
    DYNAMIC_BASE_URLS = "dynamic_base_urls"


class ContentRating(StrEnum):
    SAFE = "safe"
    MIXED = "mixed"
    NSFW = "nsfw"

    @property
    def aidoku_value(self) -> int:
        return {self.SAFE: 0, self.MIXED: 1, self.NSFW: 2}[self]


class SourceMetadata(BaseModel):
    source_id: str
    package_name: str
    name: str
    language: str
    base_url: str
    version: int = 1
    content_rating: ContentRating = ContentRating.SAFE


class SourceFile(BaseModel):
    path: str
    content: str
    sha256: str


class RouteReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    old: str = Field(min_length=1)
    new: str = Field(min_length=1)


class ChapterPageRouteVariant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    condition: str = Field(min_length=1)
    is_default: bool = False
    strip_prefix: str = ""
    replacements: list[RouteReplacement] = Field(default_factory=list)


class ChapterPageRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_method: str = Field(min_length=1)
    chapter_key_template: str = Field(min_length=1)
    endpoint_template: str = Field(min_length=1)
    variants: list[ChapterPageRouteVariant] = Field(min_length=1)

    @model_validator(mode="after")
    def has_one_default_variant(self) -> ChapterPageRoute:
        if sum(variant.is_default for variant in self.variants) != 1:
            raise ValueError("chapter page route must contain exactly one default variant")
        return self


class ImageUrlPolicy(BaseModel):
    """Source-declared boundaries for image URL transformations."""

    model_config = ConfigDict(extra="forbid")

    preserve_cover_urls: bool = True
    chapter_resolution_regex: str | None = None


class SourceFilterOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    value: str


class SourceFilterSpec(BaseModel):
    """A stable filter contract recovered from the input source."""

    model_config = ConfigDict(extra="forbid")

    source_class: str
    id: str = Field(pattern=r"^[a-z][a-z0-9_\[\]-]*$")
    title: str
    kind: Literal["check", "text", "select", "sort"]
    options: list[SourceFilterOption] = Field(default_factory=list)
    default_index: int = Field(default=0, ge=0)
    default_ascending: bool | None = None

    @model_validator(mode="after")
    def default_is_valid(self) -> SourceFilterSpec:
        if self.kind in {"select", "sort"} and not self.options:
            raise ValueError("select and sort filters require options")
        if self.kind in {"check", "text"} and self.options:
            raise ValueError("check and text filters cannot declare options")
        if self.options and self.default_index >= len(self.options):
            raise ValueError("filter default index is outside its options")
        if self.kind != "sort" and self.default_ascending is not None:
            raise ValueError("only sort filters can declare an ascending default")
        return self


class RequestHeaderProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    domains: list[str] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)


class SourceIR(BaseModel):
    schema_version: Literal[1, 2, 3, 4, 5, 6, 7] = 7
    input_ref: str
    commit: str | None = None
    source_format: Literal["kotlin_module", "decompiled_apk"] = "kotlin_module"
    feature_scope: Literal["full", "public_only"] = "full"
    metadata: SourceMetadata
    main_class: str
    parent_classes: list[str] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)
    method_names: list[str] = Field(default_factory=list)
    header_names: list[str] = Field(default_factory=list)
    request_header_profiles: list[RequestHeaderProfile] = Field(default_factory=list)
    shared_request_headers: dict[str, str] = Field(default_factory=dict)
    relative_url_keys: bool = False
    chapter_page_routes: list[ChapterPageRoute] = Field(default_factory=list)
    image_url_policy: ImageUrlPolicy | None = None
    filter_specs: list[SourceFilterSpec] = Field(default_factory=list)
    files: list[SourceFile] = Field(default_factory=list)
    license_name: str | None = None
    license_text: str | None = None
    warnings: list[str] = Field(default_factory=list)
    unsupported_features: list[str] = Field(default_factory=list)
    analysis_rule_ids: list[str] = Field(default_factory=list)


class DependencyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Kept as a string so untrusted manifests can be parsed and rejected by
    # the scaffold's explicit allowlist without turning a security failure into
    # an unrelated model-construction error.
    name: str
    features: list[str] = Field(default_factory=list)
    reason: str = ""


def _require_string_list(
    item: dict[str, Any],
    key: str,
    *,
    location: str,
    required: bool = False,
) -> list[str] | None:
    value = item.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
        raise ValueError(f"{location}.{key} must be an array of strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{location}.{key} must contain unique strings")
    return value


_SETTING_TITLE_ACRONYMS = {
    "api": "API",
    "http": "HTTP",
    "https": "HTTPS",
    "id": "ID",
    "ip": "IP",
    "ua": "UA",
    "url": "URL",
}


def _humanize_setting_key(key: str) -> str:
    suffix = key.rsplit(".", 1)[-1]
    words = [word for word in re.split(r"[_\-\s]+", suffix) if word]
    return " ".join(
        _SETTING_TITLE_ACRONYMS.get(word.casefold(), word[:1].upper() + word[1:]) for word in words
    )


def _normalize_protocol_setting_values(item: dict[str, Any], setting_type: Any) -> None:
    key = item.get("key")
    if (
        setting_type not in {"select", "multi-select", "picker"}
        or not isinstance(key, str)
        or key.rsplit(".", 1)[-1] != "resolution"
    ):
        return
    values = item.get("values")
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(value, str) and value.isdecimal() for value in values)
    ):
        return
    item["values"] = [f"resolution.r{value}" for value in values]
    default = item.get("default")
    if isinstance(default, str) and default.isdecimal():
        item["default"] = f"resolution.r{default}"
    elif isinstance(default, list):
        item["default"] = [
            f"resolution.r{value}" if isinstance(value, str) and value.isdecimal() else value
            for value in default
        ]


def _normalize_setting_item(raw_item: Any, *, location: str) -> dict[str, Any]:
    if not isinstance(raw_item, dict):
        raise ValueError(f"{location} must be a JSON object")
    item: dict[str, Any] = raw_item
    setting_type = item.get("type")
    if setting_type not in {"group", "page"} and "id" in item:
        legacy_id = item.pop("id")
        if not isinstance(legacy_id, str):
            raise ValueError(f"{location}.id must be a string")
        key = item.get("key")
        if key is None:
            item["key"] = legacy_id
        elif key != legacy_id:
            raise ValueError(f"{location}.id and key must match when both are present")
    if setting_type in {"select", "multi-select"} and "options" in item:
        options = item.pop("options")
        if not isinstance(options, list) or not options:
            raise ValueError(f"{location}.options must be a non-empty array")
        titles: list[str] = []
        values: list[str] = []
        for option_index, option in enumerate(options):
            if not isinstance(option, dict):
                raise ValueError(f"{location}.options[{option_index}] must contain title and value")
            title = option.get("title")
            value = option.get("value")
            if not isinstance(title, str) or not isinstance(value, str):
                raise ValueError(
                    f"{location}.options[{option_index}] must contain string title and value fields"
                )
            titles.append(title)
            values.append(value)
        item.setdefault("titles", titles)
        item.setdefault("values", values)
    _normalize_protocol_setting_values(item, setting_type)
    if setting_type not in {"group", "page"} and "title" not in item:
        key = item.get("key")
        if isinstance(key, str):
            title = _humanize_setting_key(key)
            if title:
                item["title"] = title
    if setting_type == "text":
        item.setdefault("default", "")
    if setting_type in {"select", "multi-select"}:
        values = _require_string_list(item, "values", location=location, required=True)
        titles = _require_string_list(item, "titles", location=location)
        if titles is not None and values is not None and len(titles) != len(values):
            raise ValueError(f"{location}.titles must have the same length as values")
        default = item.get("default")
        if (
            setting_type == "select"
            and default is not None
            and (not isinstance(default, str) or (values is not None and default not in values))
        ):
            raise ValueError(f"{location}.default must be one of values")
        if (
            setting_type == "multi-select"
            and default is not None
            and (
                not isinstance(default, list)
                or not all(
                    isinstance(entry, str) and (values is None or entry in values)
                    for entry in default
                )
            )
        ):
            raise ValueError(f"{location}.default must contain only entries from values")
    if setting_type in {"group", "page"}:
        children = item.get("items")
        if not isinstance(children, list):
            raise ValueError(f"{location}.items must be an array")
        normalized_children = [
            _normalize_setting_item(child, location=f"{location}.items[{index}]")
            for index, child in enumerate(children)
        ]
        if setting_type == "page":
            page_groups: list[dict[str, Any]] = []
            loose_items: list[dict[str, Any]] = []

            def flush_loose_items() -> None:
                if loose_items:
                    page_groups.append({"type": "group", "items": list(loose_items)})
                    loose_items.clear()

            for child in normalized_children:
                if child.get("type") == "group":
                    flush_loose_items()
                    page_groups.append(child)
                else:
                    loose_items.append(child)
            flush_loose_items()
            item["items"] = _promote_nested_setting_groups(page_groups)
        else:
            item["items"] = normalized_children
    return item


def _promote_nested_setting_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    promoted: list[dict[str, Any]] = []
    for group in groups:
        items = group.get("items")
        if not isinstance(items, list):
            promoted.append(group)
            continue
        own_items: list[dict[str, Any]] = []
        had_nested_group = False
        for item in items:
            if item.get("type") == "group":
                had_nested_group = True
                promoted.extend(_promote_nested_setting_groups([item]))
            else:
                own_items.append(item)
        if own_items or not had_nested_group:
            group["items"] = own_items
            promoted.append(group)
    return promoted


def normalize_generated_json_resource(path: str, content: str) -> str:
    if path not in {"res/filters.json", "res/settings.json"}:
        return content
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON array")
    if path == "res/settings.json":
        top_level_types = [item.get("type") if isinstance(item, dict) else None for item in data]
        if data and all(setting_type != "group" for setting_type in top_level_types):
            data = [{"type": "group", "items": data}]
        elif any(setting_type != "group" for setting_type in top_level_types):
            raise ValueError("res/settings.json top-level items must all be groups")
        data = _promote_nested_setting_groups(
            [
                _normalize_setting_item(item, location=f"res/settings.json[{index}]")
                for index, item in enumerate(data)
            ]
        )
        return json.dumps(data, ensure_ascii=False, indent="\t") + "\n"

    allowed_types = {"text", "sort", "check", "select", "multi-select", "note", "range"}
    option_types = {"sort", "select", "multi-select"}
    for index, raw_item in enumerate(data):
        location = f"res/filters.json[{index}]"
        if not isinstance(raw_item, dict):
            raise ValueError(f"{location} must be a JSON object")
        item: dict[str, Any] = raw_item
        filter_type = item.get("type")
        if filter_type not in allowed_types:
            raise ValueError(f"{location}.type must be one of: {', '.join(sorted(allowed_types))}")
        if filter_type in option_types:
            options = item.get("options")
            if not isinstance(options, list) or not options:
                raise ValueError(f"{location}.options must be a non-empty array")
            if all(isinstance(option, dict) for option in options):
                titles: list[str] = []
                values: list[str] = []
                for option_index, option in enumerate(options):
                    assert isinstance(option, dict)
                    title = option.get("title")
                    value = option.get("value")
                    if not isinstance(title, str) or not isinstance(value, str):
                        raise ValueError(
                            f"{location}.options[{option_index}] must contain string title and "
                            "value fields"
                        )
                    titles.append(title)
                    values.append(value)
                item["options"] = titles
                item.setdefault("ids", values)
            option_strings = _require_string_list(item, "options", location=location, required=True)
            ids = _require_string_list(item, "ids", location=location)
            if ids is not None and option_strings is not None and len(ids) != len(option_strings):
                raise ValueError(f"{location}.ids must have the same length as options")
        if filter_type == "note" and not isinstance(item.get("text"), str):
            raise ValueError(f"{location}.text must be a string")
    return json.dumps(data, ensure_ascii=False, indent="\t") + "\n"


def validate_generated_path(value: str) -> str:
    if "\\" in value:
        raise SecurityError(f"generated path must use forward slashes: {value}")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SecurityError(f"unsafe generated path: {value}")
    if path.parts[0] == "src" and path.suffix == ".rs":
        if path.as_posix() == "src/generated_smoke.rs":
            raise SecurityError("src/generated_smoke.rs is reserved by the validator")
        return path.as_posix()
    if path.as_posix() in {"res/filters.json", "res/settings.json"}:
        return path.as_posix()
    raise SecurityError(f"generated file is outside the allowlist: {value}")


class GeneratedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    content: str = Field(max_length=MAX_GENERATED_FILE_CHARS)

    @field_validator("path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        return validate_generated_path(value)

    @model_validator(mode="after")
    def resource_json_matches_aidoku_shape(self) -> GeneratedFile:
        self.content = normalize_generated_json_resource(self.path, self.content)
        return self


OptionalTrait = Literal[
    "ListingProvider",
    "Home",
    "DynamicListings",
    "DynamicFilters",
    "DynamicSettings",
    "PageImageProcessor",
    "ImageRequestProvider",
    "PageDescriptionProvider",
    "AlternateCoverProvider",
    "BaseUrlProvider",
    "NotificationHandler",
    "DeepLinkHandler",
    "BasicLoginHandler",
    "WebLoginHandler",
    "MigrationHandler",
]


class TemplateSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: Literal["rust", "selector", "url", "header", "json"]
    required: bool = True
    description: str


class TemplateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(pattern=r"^[a-z][a-z0-9-]+$")
    aidoku_revision: str
    description: str
    required_capabilities: list[Capability] = Field(default_factory=list)
    optional_capabilities: list[Capability] = Field(default_factory=list)
    slots: list[TemplateSlot] = Field(default_factory=list)
    provenance: str
    license_note: str


class TemplateMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str
    aidoku_revision: str
    ready: bool
    score: float = Field(ge=0.0, le=1.0)
    matched_capabilities: list[Capability] = Field(default_factory=list)
    missing_capabilities: list[Capability] = Field(default_factory=list)
    slots: list[TemplateSlot] = Field(default_factory=list)
    provenance: str
    license_note: str


class GenerationManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_struct: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    implemented_traits: list[OptionalTrait] = Field(default_factory=list)
    files: list[GeneratedFile] = Field(min_length=1, max_length=MAX_GENERATED_FILES)
    dependencies: list[DependencyRequest] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    unsupported_features: list[str] = Field(default_factory=list)

    @field_validator("files")
    @classmethod
    def requires_lib_rs_and_unique_paths(cls, files: list[GeneratedFile]) -> list[GeneratedFile]:
        paths = [item.path for item in files]
        if len(paths) != len(set(paths)):
            raise ValueError("generated file paths must be unique")
        if "src/lib.rs" not in paths:
            raise ValueError("generation manifest must include src/lib.rs")
        return files

    @model_validator(mode="after")
    def generated_content_has_reasonable_total_size(self) -> GenerationManifest:
        if sum(len(item.content) for item in self.files) > MAX_GENERATED_TOTAL_CHARS:
            raise ValueError(
                f"generated file contents exceed the {MAX_GENERATED_TOTAL_CHARS:,} character limit"
            )
        return self


class GeneratedResources:
    """Parsed filters/settings with deterministic transformations behind one interface."""

    FILTERS = "res/filters.json"
    SETTINGS = "res/settings.json"

    def __init__(self, manifest: GenerationManifest):
        self._manifest = manifest
        self._data: dict[str, list[Any]] = {}
        for generated in manifest.files:
            if generated.path not in {self.FILTERS, self.SETTINGS}:
                continue
            data = json.loads(generated.content)
            if isinstance(data, list):
                self._data[generated.path] = data

    def has(self, path: str) -> bool:
        return path in self._data

    def is_empty(self, path: str) -> bool:
        if path == self.SETTINGS:
            return not self.has_nonempty_setting_items()
        return not self._data.get(path)

    def _setting_items(self) -> tuple[dict[str, Any], ...]:
        items: list[dict[str, Any]] = []
        pending = list(reversed(self._data.get(self.SETTINGS, [])))
        while pending:
            item = pending.pop()
            if not isinstance(item, dict):
                continue
            children = item.get("items")
            if item.get("type") in {"group", "page"} and isinstance(children, list):
                pending.extend(reversed(children))
            else:
                items.append(item)
        return tuple(items)

    def has_nonempty_setting_items(self) -> bool:
        return bool(self._setting_items())

    def setting_keys(self) -> tuple[str, ...]:
        return tuple(
            item["key"] for item in self._setting_items() if isinstance(item.get("key"), str)
        )

    def contains_text(self, path: str, needle: str) -> bool:
        lowered = needle.lower()

        def contains(value: Any) -> bool:
            if isinstance(value, str):
                return lowered in value.lower()
            if isinstance(value, list):
                return any(contains(item) for item in value)
            if isinstance(value, dict):
                return any(contains(key) or contains(item) for key, item in value.items())
            return False

        return contains(self._data.get(path, []))

    def setting_defaults(self) -> dict[str, str]:
        defaults: dict[str, str] = {}
        for item in self._setting_items():
            key = item.get("key")
            default = item.get("default")
            if isinstance(key, str) and isinstance(default, str):
                defaults[key] = default
        return defaults

    def setting_values(self) -> dict[str, tuple[str, ...]]:
        values_by_key: dict[str, tuple[str, ...]] = {}
        for item in self._setting_items():
            key = item.get("key")
            values = item.get("values")
            if (
                isinstance(key, str)
                and isinstance(values, list)
                and all(isinstance(value, str) for value in values)
            ):
                values_by_key[key] = tuple(values)
        return values_by_key

    def with_source_filters(self, specs: list[SourceFilterSpec]) -> GenerationManifest:
        if not specs:
            return self._manifest
        filters: list[dict[str, Any]] = []
        for spec in specs:
            values = [option.value for option in spec.options]
            item: dict[str, Any] = {
                "type": spec.kind,
                "id": spec.id,
                "title": spec.title,
            }
            if spec.kind in {"select", "sort"}:
                item.update(
                    {
                        "options": [option.title for option in spec.options],
                        "ids": values,
                    }
                )
            if spec.kind == "check":
                item["default"] = False
            elif spec.kind == "text":
                item["default"] = ""
            elif spec.kind == "sort":
                item.update(
                    {
                        "default": {
                            "index": spec.default_index,
                            "ascending": bool(spec.default_ascending),
                        },
                        "canAscend": True,
                    }
                )
            elif spec.kind == "select":
                item["default"] = values[spec.default_index]
            filters.append(item)
        generated = GeneratedFile(
            path=self.FILTERS,
            content=json.dumps(filters, ensure_ascii=False),
        )
        files = [item for item in self._manifest.files if item.path != self.FILTERS]
        return self._manifest.model_copy(update={"files": [*files, generated]})

    def filter_contract_gaps(
        self,
        specs: list[SourceFilterSpec],
        *,
        has_sort_mapping: bool,
    ) -> list[str]:
        raw_filters = self._data.get(self.FILTERS)
        if not specs or raw_filters is None:
            return []
        filters = {
            item.get("id"): item
            for item in raw_filters
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        gaps: list[str] = []
        for spec in specs:
            item = filters.get(spec.id)
            if item is None:
                gaps.append(
                    f"recovered Tachi filter {spec.source_class} is missing Aidoku id {spec.id!r}"
                )
                continue
            if item.get("type") != spec.kind:
                gaps.append(
                    f"filter {spec.id!r} has type {item.get('type')!r}; "
                    f"recovered Tachi {spec.source_class} requires {spec.kind!r}"
                )
            expected_titles = [option.title for option in spec.options]
            expected_values = [option.value for option in spec.options]
            if spec.kind in {"select", "sort"} and item.get("options") != expected_titles:
                gaps.append(
                    f"filter {spec.id!r} does not preserve recovered display options "
                    f"{expected_titles!r}"
                )
            if spec.kind == "check":
                if item.get("default") is not False:
                    gaps.append(f"filter {spec.id!r} default must be false")
            elif spec.kind == "text":
                if item.get("default") != "":
                    gaps.append(f"filter {spec.id!r} default must be empty text")
            elif spec.kind == "select":
                if item.get("ids") != expected_values:
                    gaps.append(f"filter {spec.id!r} site values must be {expected_values!r}")
                expected_default: Any = expected_values[spec.default_index]
                if item.get("default") != expected_default:
                    gaps.append(
                        f"filter {spec.id!r} default must be recovered site value "
                        f"{expected_default!r}"
                    )
            else:
                expected_default = {
                    "index": spec.default_index,
                    "ascending": bool(spec.default_ascending),
                }
                if item.get("default") != expected_default:
                    gaps.append(f"filter {spec.id!r} default must be {expected_default!r}")
                if not has_sort_mapping:
                    gaps.append(
                        f"filter {spec.id!r} requires a FilterValue::Sort index/ascending mapping"
                    )
        return gaps

    def with_defaults(
        self,
        *,
        filter_specs: list[SourceFilterSpec] | None = None,
    ) -> GenerationManifest:
        specs = {spec.id: spec for spec in filter_specs or []}
        updated_files: list[GeneratedFile] = []
        changed = False
        for generated in self._manifest.files:
            data = deepcopy(self._data.get(generated.path))
            if generated.path == self.FILTERS and data is not None and specs:
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    spec = specs.get(item.get("id"))
                    if spec is None:
                        continue
                    if spec.kind == "check":
                        value = False
                    elif spec.kind == "text":
                        value = ""
                    elif spec.kind == "select":
                        value: Any = spec.options[spec.default_index].value
                    else:
                        value = {
                            "index": spec.default_index,
                            "ascending": bool(spec.default_ascending),
                        }
                        item.setdefault("canAscend", True)
                    item["default"] = value
            if data is None:
                updated_files.append(generated)
                continue
            content = json.dumps(data, ensure_ascii=False, indent="\t") + "\n"
            if content != generated.content:
                changed = True
                updated_files.append(generated.model_copy(update={"content": content}))
            else:
                updated_files.append(generated)
        if not changed:
            return self._manifest
        return self._manifest.model_copy(update={"files": updated_files})

    def static_filter_cases(self) -> list[dict[str, Any]]:
        filters = self._data.get(self.FILTERS)
        if filters is None:
            return []
        cases: list[dict[str, Any]] = []
        for raw_filter in filters:
            if not isinstance(raw_filter, dict):
                continue
            filter_type = raw_filter.get("type")
            filter_id = raw_filter.get("id") or raw_filter.get("title") or filter_type
            if not isinstance(filter_id, str):
                continue
            if filter_type == "select":
                options = raw_filter.get("options")
                ids = raw_filter.get("ids", options)
                if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
                    continue
                default = raw_filter.get("default")
                candidates = [
                    candidate
                    for candidate in ids
                    if candidate and (not isinstance(default, str) or candidate != default)
                ]
                year_candidates = [
                    candidate
                    for candidate in candidates
                    if re.fullmatch(r"(?:19|20)\d{2}", candidate)
                ]
                if len(year_candidates) == len(candidates) and len(year_candidates) >= 3:
                    value = year_candidates[2]
                elif candidates:
                    value = candidates[0]
                else:
                    value = default if isinstance(default, str) else ids[0] if ids else None
                if value is not None:
                    cases.append({"kind": "select", "id": filter_id, "value": value})
            elif filter_type == "sort":
                options = raw_filter.get("options")
                if not isinstance(options, list) or not options:
                    continue
                default = raw_filter.get("default")
                index = default.get("index", 0) if isinstance(default, dict) else 0
                ascending = default.get("ascending", False) if isinstance(default, dict) else False
                if isinstance(index, int) and isinstance(ascending, bool):
                    cases.append(
                        {
                            "kind": "sort",
                            "id": filter_id,
                            "index": max(0, min(index, len(options) - 1)),
                            "ascending": ascending,
                        }
                    )
        return cases


class RepairEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    old_text: str = Field(min_length=1, max_length=24_000)
    new_text: str = Field(max_length=32_000)

    @field_validator("path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        return validate_generated_path(value)

    @model_validator(mode="after")
    def replacement_must_change_content(self) -> RepairEdit:
        if self.old_text == self.new_text:
            raise ValueError("repair replacement must change the matched text")
        return self


class RepairPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edits: list[RepairEdit] = Field(min_length=1, max_length=MAX_REPAIR_PATCH_EDITS)

    @field_validator("edits")
    @classmethod
    def edits_are_unique(cls, edits: list[RepairEdit]) -> list[RepairEdit]:
        identities = [(edit.path, edit.old_text) for edit in edits]
        if len(identities) != len(set(identities)):
            raise ValueError("repair edits must have unique path/old_text pairs")
        return edits


class StageKind(StrEnum):
    TOOLCHAIN = "toolchain"
    FORMAT = "format"
    CHECK = "check"
    CLIPPY = "clippy"
    LIVE_TEST = "live_test"
    PACKAGE = "package"
    VERIFY = "verify"


class ValidationBlocker(StrEnum):
    ANONYMOUS_INITIALIZATION = "anonymous_initialization"
    SITE_NETWORK_ERROR = "site_network_error"
    SITE_HTTP_BLOCK = "site_http_block"
    SITE_BROWSER_CHALLENGE = "site_browser_challenge"
    RUNNER_FINGERPRINT = "runner_fingerprint"
    RUNNER_TRANSPORT = "runner_transport"
    API_HTTP_BLOCK = "api_http_block"
    API_BROWSER_CHALLENGE = "api_browser_challenge"


class ValidationStage(BaseModel):
    name: str
    kind: StageKind
    ok: bool
    command: list[str] = Field(default_factory=list)
    output: str = ""
    duration_seconds: float = 0.0
    skipped: bool = False
    blocked: bool = False
    blocker_reason: ValidationBlocker | None = None


class ValidationResult(BaseModel):
    stages: list[ValidationStage] = Field(default_factory=list)
    build_ok: bool = False
    package_ok: bool = False
    live_ok: bool = False
    blocked: bool = False
    blocker_reason: ValidationBlocker | None = None
    contract_ok: bool = True

    @property
    def diagnostics(self) -> str:
        chunks = [stage.output for stage in self.stages if not stage.ok and stage.output]
        return "\n\n".join(chunks)


class ConversionStatus(StrEnum):
    VERIFIED = "verified"
    BUILD_ONLY = "build_only"
    BLOCKED = "blocked"
    FAILED = "failed"


class AIUsage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_prompt_tokens: int | None = None


type RepairMode = Literal["compiler_patch", "contract_patch", "full"]


class AIRound(BaseModel):
    round: int
    purpose: Literal["generate", "repair"]
    repair_mode: RepairMode | None = None
    structured_output: bool
    reasoning_effort: Literal["auto", "off", "low", "medium", "high"] | None = None
    usage: AIUsage | None = None
    warnings: list[str] = Field(default_factory=list)
    normalization_rewrites: dict[str, int] = Field(default_factory=dict)
    projection_rewrites: dict[str, int] = Field(default_factory=dict)
    contract_rule_ids: list[str] = Field(default_factory=list)


class AIFailedExchange(BaseModel):
    purpose: Literal["generate", "repair"]
    usage: AIUsage | None = None
    diagnostics: list[str] = Field(default_factory=list)


class ConversionCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    input_ref: str
    output: str
    provider_base_url: str
    model: str
    query: str | None = None
    live: bool = True
    force: bool = False
    phase: Literal["analyzed", "manifest_saved", "validated", "complete"] = "analyzed"
    current_manifest: str | None = None
    ai_rounds: list[AIRound] = Field(default_factory=list)
    failed_ai_exchanges: list[AIFailedExchange] = Field(default_factory=list)
    repair_attempt_signatures: list[str] = Field(default_factory=list)
    generated_files: list[str] = Field(default_factory=list)
    capability_gaps: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    manifest_warnings: list[str] = Field(default_factory=list)
    unsupported_features: list[str] = Field(default_factory=list)
    preflight_rule_ids: list[str] = Field(default_factory=list)
    validation: ValidationResult | None = None


class ConversionReport(BaseModel):
    schema_version: Literal[1] = 1
    status: ConversionStatus
    input_ref: str
    source_id: str
    provider_base_url: str | None = None
    model: str | None = None
    ai_rounds: list[AIRound] = Field(default_factory=list)
    failed_ai_exchanges: list[AIFailedExchange] = Field(default_factory=list)
    generated_files: list[str] = Field(default_factory=list)
    template_matches: list[TemplateMatch] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    unsupported_features: list[str] = Field(default_factory=list)
    source_analysis_rule_ids: list[str] = Field(default_factory=list)
    preflight_rule_ids: list[str] = Field(default_factory=list)
    validation: ValidationResult
    provenance: dict[str, Any] = Field(default_factory=dict)
