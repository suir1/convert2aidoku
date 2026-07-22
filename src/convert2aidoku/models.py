from __future__ import annotations

import json
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .constants import MAX_GENERATED_FILE_CHARS, MAX_GENERATED_FILES, MAX_GENERATED_TOTAL_CHARS
from .errors import SecurityError


class Capability(StrEnum):
    SEARCH = "search"
    POPULAR = "popular"
    LATEST = "latest"
    DETAILS = "details"
    CHAPTERS = "chapters"
    PAGES = "pages"
    FILTERS = "filters"
    DYNAMIC_FILTERS = "dynamic_filters"
    SETTINGS = "settings"
    IMAGE_HEADERS = "image_headers"
    DEEP_LINKS = "deep_links"
    JSON_API = "json_api"
    ENCRYPTED_JSON = "encrypted_json"
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
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str
    kind: Literal["select", "sort"]
    options: list[SourceFilterOption] = Field(min_length=1)
    default_index: int = Field(default=0, ge=0)
    default_ascending: bool | None = None

    @model_validator(mode="after")
    def default_is_valid(self) -> SourceFilterSpec:
        if self.default_index >= len(self.options):
            raise ValueError("filter default index is outside its options")
        if self.kind == "select" and self.default_ascending is not None:
            raise ValueError("select filters cannot declare an ascending default")
        return self


class SourceIR(BaseModel):
    schema_version: Literal[1, 2] = 2
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
    relative_url_keys: bool = False
    chapter_page_routes: list[ChapterPageRoute] = Field(default_factory=list)
    image_url_policy: ImageUrlPolicy | None = None
    filter_specs: list[SourceFilterSpec] = Field(default_factory=list)
    files: list[SourceFile] = Field(default_factory=list)
    license_name: str | None = None
    license_text: str | None = None
    warnings: list[str] = Field(default_factory=list)
    unsupported_features: list[str] = Field(default_factory=list)


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
        item["items"] = [
            _normalize_setting_item(child, location=f"{location}.items[{index}]")
            for index, child in enumerate(children)
        ]
    return item


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
        data = [
            _normalize_setting_item(item, location=f"res/settings.json[{index}]")
            for index, item in enumerate(data)
        ]
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

    @field_validator("implemented_traits")
    @classmethod
    def optional_traits_are_known(cls, values: list[str]) -> list[str]:
        allowed = {
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
        }
        invalid = sorted(set(values) - allowed)
        if invalid:
            raise ValueError("unknown or non-registerable optional traits: " + ", ".join(invalid))
        return values

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


class StageKind(StrEnum):
    TOOLCHAIN = "toolchain"
    FORMAT = "format"
    CHECK = "check"
    CLIPPY = "clippy"
    LIVE_TEST = "live_test"
    PACKAGE = "package"
    VERIFY = "verify"


class ValidationStage(BaseModel):
    name: str
    kind: StageKind
    ok: bool
    command: list[str] = Field(default_factory=list)
    output: str = ""
    duration_seconds: float = 0.0
    skipped: bool = False
    blocked: bool = False


class ValidationResult(BaseModel):
    stages: list[ValidationStage] = Field(default_factory=list)
    build_ok: bool = False
    package_ok: bool = False
    live_ok: bool = False
    blocked: bool = False
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


class AIRound(BaseModel):
    round: int
    purpose: Literal["generate", "repair"]
    structured_output: bool
    usage: AIUsage | None = None
    warnings: list[str] = Field(default_factory=list)


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
    generated_files: list[str] = Field(default_factory=list)
    capability_gaps: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    manifest_warnings: list[str] = Field(default_factory=list)
    unsupported_features: list[str] = Field(default_factory=list)
    validation: ValidationResult | None = None


class ConversionReport(BaseModel):
    schema_version: Literal[1] = 1
    status: ConversionStatus
    input_ref: str
    source_id: str
    provider_base_url: str | None = None
    model: str | None = None
    ai_rounds: list[AIRound] = Field(default_factory=list)
    generated_files: list[str] = Field(default_factory=list)
    template_matches: list[TemplateMatch] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    unsupported_features: list[str] = Field(default_factory=list)
    validation: ValidationResult
    provenance: dict[str, Any] = Field(default_factory=dict)
