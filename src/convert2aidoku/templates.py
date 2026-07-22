from __future__ import annotations

from .constants import AIDOKU_RS_REV
from .models import Capability, SourceIR, TemplateMatch, TemplateSlot, TemplateSpec

_PROVENANCE = (
    "Abstracted from the current Aidoku Rust contract and pinned aidoku-rs revision; "
    "no source-specific implementation is embedded."
)
_LICENSE_NOTE = (
    "This is a structural pattern, not copied source code. Preserve the input source license "
    "and verify redistribution rights for any generated derivative."
)


def _slot(
    name: str,
    kind: str,
    description: str,
    *,
    required: bool = True,
) -> TemplateSlot:
    return TemplateSlot(name=name, kind=kind, description=description, required=required)


def builtin_templates() -> tuple[TemplateSpec, ...]:
    """Return versioned, source-agnostic patterns supported by this checkout."""
    return (
        TemplateSpec(
            template_id="html-http-source",
            aidoku_revision=AIDOKU_RS_REV,
            description=(
                "Current no_std Source implementation for an HTML HttpSource with list, "
                "details, chapter, and page operations."
            ),
            required_capabilities=[
                Capability.SEARCH,
                Capability.DETAILS,
                Capability.CHAPTERS,
                Capability.PAGES,
            ],
            slots=[
                _slot("base_url", "url", "The source base URL."),
                _slot("list_selector", "selector", "A standards-compatible manga-card selector."),
                _slot("details_selector", "selector", "Selectors for title, cover, and metadata."),
                _slot("chapter_selector", "selector", "A selector for chapter groups or rows."),
                _slot("page_selector", "selector", "A selector for page image elements."),
                _slot("key_strategy", "rust", "Whether keys store IDs, paths, or stable URLs."),
                _slot("query_strategy", "rust", "Search, page, and empty-query behavior."),
            ],
            provenance=_PROVENANCE,
            license_note=_LICENSE_NOTE,
        ),
        TemplateSpec(
            template_id="filter-query-builder",
            aidoku_revision=AIDOKU_RS_REV,
            description="Aidoku filters.json plus typed FilterValue-to-query mapping.",
            required_capabilities=[Capability.FILTERS],
            optional_capabilities=[Capability.DYNAMIC_FILTERS],
            slots=[
                _slot("filter_schema", "json", "Aidoku filter definitions with options and ids."),
                _slot(
                    "filter_query_mapping",
                    "rust",
                    "Mapping from filter IDs to site parameters.",
                ),
            ],
            provenance=_PROVENANCE,
            license_note=_LICENSE_NOTE,
        ),
        TemplateSpec(
            template_id="listing-provider",
            aidoku_revision=AIDOKU_RS_REV,
            description="Optional ListingProvider implementation for popular/latest pages.",
            required_capabilities=[Capability.POPULAR],
            optional_capabilities=[Capability.LATEST],
            slots=[
                _slot("popular_endpoint", "url", "Popular listing endpoint."),
                _slot("latest_endpoint", "url", "Latest listing endpoint.", required=False),
                _slot("listing_parser", "rust", "Parser shared by listing pages."),
            ],
            provenance=_PROVENANCE,
            license_note=_LICENSE_NOTE,
        ),
        TemplateSpec(
            template_id="image-request-provider",
            aidoku_revision=AIDOKU_RS_REV,
            description="ImageRequestProvider for Referer and other image headers.",
            required_capabilities=[Capability.IMAGE_HEADERS],
            slots=[
                _slot("image_headers", "header", "Headers required by image requests."),
            ],
            provenance=_PROVENANCE,
            license_note=_LICENSE_NOTE,
        ),
        TemplateSpec(
            template_id="settings-resource",
            aidoku_revision=AIDOKU_RS_REV,
            description="settings.json plus defaults access through the public Aidoku API.",
            required_capabilities=[Capability.SETTINGS],
            slots=[
                _slot("settings_schema", "json", "Aidoku settings resource."),
                _slot("defaults_mapping", "rust", "Public defaults_get mapping."),
            ],
            provenance=_PROVENANCE,
            license_note=_LICENSE_NOTE,
        ),
        TemplateSpec(
            template_id="deep-link-handler",
            aidoku_revision=AIDOKU_RS_REV,
            description="Optional DeepLinkHandler for stable manga and chapter URLs.",
            required_capabilities=[Capability.DEEP_LINKS],
            slots=[
                _slot("deep_link_parser", "rust", "Stable path-to-key mapping."),
            ],
            provenance=_PROVENANCE,
            license_note=_LICENSE_NOTE,
        ),
        TemplateSpec(
            template_id="json-api-source",
            aidoku_revision=AIDOKU_RS_REV,
            description=(
                "Current no_std JSON API source using Aidoku requests and typed serde models."
            ),
            required_capabilities=[Capability.JSON_API],
            optional_capabilities=[
                Capability.SEARCH,
                Capability.DETAILS,
                Capability.CHAPTERS,
                Capability.PAGES,
            ],
            slots=[
                _slot("api_base_url", "url", "The public JSON API base URL."),
                _slot("request_headers", "header", "Headers required by API requests."),
                _slot("response_models", "rust", "Typed serde response envelopes and DTOs."),
                _slot("pagination", "rust", "Page-to-offset or cursor conversion."),
            ],
            provenance=_PROVENANCE,
            license_note=_LICENSE_NOTE,
        ),
        TemplateSpec(
            template_id="aes-cbc-json",
            aidoku_revision=AIDOKU_RS_REV,
            description="AES-CBC response decoding followed by typed JSON deserialization.",
            required_capabilities=[Capability.ENCRYPTED_JSON],
            slots=[
                _slot("key_derivation", "rust", "Exact source AES key derivation."),
                _slot("iv_extraction", "rust", "Exact IV location and encoding."),
                _slot("ciphertext_encoding", "rust", "Hex or base64 ciphertext decoding."),
                _slot("padding", "rust", "PKCS#5/PKCS#7 padding behavior."),
            ],
            provenance=_PROVENANCE,
            license_note=_LICENSE_NOTE,
        ),
        TemplateSpec(
            template_id="dynamic-base-url",
            aidoku_revision=AIDOKU_RS_REV,
            description="A settings-backed allowlisted base URL selector.",
            required_capabilities=[Capability.DYNAMIC_BASE_URLS],
            slots=[
                _slot("allowed_base_urls", "url", "Finite allowlist of source base URLs."),
                _slot("base_url_setting", "json", "User-visible base URL selection setting."),
                _slot("base_url_resolver", "rust", "Validated defaults-to-base URL mapping."),
            ],
            provenance=_PROVENANCE,
            license_note=_LICENSE_NOTE,
        ),
    )


def match_templates(ir: SourceIR) -> list[TemplateMatch]:
    """Match only templates for the pinned Aidoku revision.

    A match describes readiness and missing capabilities; it never copies an existing source
    implementation or sends one to the AI provider.
    """
    available = set(ir.capabilities)
    matches: list[TemplateMatch] = []
    for template in builtin_templates():
        required = set(template.required_capabilities)
        optional = set(template.optional_capabilities)
        matched = sorted(
            (required | optional) & available,
            key=lambda capability: capability.value,
        )
        missing = sorted(required - available, key=lambda capability: capability.value)
        denominator = max(1, len(required | optional))
        matches.append(
            TemplateMatch(
                template_id=template.template_id,
                aidoku_revision=template.aidoku_revision,
                ready=not missing,
                score=len(matched) / denominator,
                matched_capabilities=matched,
                missing_capabilities=missing,
                slots=template.slots,
                provenance=template.provenance,
                license_note=template.license_note,
            )
        )
    return sorted(matches, key=lambda match: (-match.ready, -match.score, match.template_id))
