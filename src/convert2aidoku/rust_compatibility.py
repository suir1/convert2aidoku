from __future__ import annotations

from collections.abc import Mapping

from .aidoku_import_compatibility import (
    finalize_aidoku_imports,
    normalize_aidoku_api_paths,
    normalize_aidoku_registration_imports,
)
from .aidoku_model_compatibility import (
    normalize_aidoku_model_construction,
    normalize_aidoku_model_string_literals,
    normalize_aidoku_models,
    normalize_aidoku_struct_defaults,
)
from .aidoku_page_context_compatibility import (
    normalize_image_request_compatibility,
    normalize_image_url_compatibility,
    normalize_page_context_compatibility,
)
from .chapter_date_compatibility import (
    finalize_chapter_date_compatibility,
    normalize_chapter_date_compatibility,
)
from .dynamic_filter_compatibility import (
    normalize_filter_predicate_compatibility,
    normalize_legacy_dynamic_filters,
    normalize_select_filter_path_compatibility,
    normalize_select_filter_struct_compatibility,
)
from .generated_rust_safety import _remove_reserved_smoke_marker as _remove_reserved_smoke_marker
from .generated_rust_safety import validate_generated_content as validate_generated_content
from .generation_setting_compatibility import (
    normalize_generation_settings,
    normalize_runtime_setting_access,
    normalize_typed_domain_setting,
)
from .graphql_compatibility import (
    normalize_graphql_fragment_compatibility,
    normalize_graphql_projection_compatibility,
    normalize_graphql_request_compatibility,
)
from .normalization_trace import NormalizationTrace
from .request_response_compatibility import (
    normalize_generic_response_models,
    normalize_html_response_values,
    normalize_legacy_request_compatibility,
    normalize_pagination_response_models,
    normalize_request_header_values,
    normalize_request_response_compatibility,
    normalize_request_result_tails,
    normalize_request_retry_compatibility,
)
from .rust_control_flow_compatibility import (
    normalize_early_control_flow,
    normalize_indexing_control_flow,
    normalize_iteration_control_flow,
    normalize_late_control_flow,
)
from .rust_ownership_compatibility import normalize_detail_ownership, normalize_pagination_ownership
from .source_runtime_compatibility import (
    normalize_source_bootstrap_compatibility,
    normalize_source_lifecycle_compatibility,
    normalize_source_runtime_attributes,
    normalize_source_runtime_prelude,
)
from .source_url_compatibility import (
    normalize_deep_link_compatibility,
    normalize_ir_source_urls,
    normalize_literal_url_compatibility,
    normalize_preserved_source_urls,
    normalize_source_path_helpers,
)


def normalize_pinned_aidoku_rust(
    content: str,
    *,
    allow_dead_code: bool = False,
    setting_defaults: Mapping[str, str] | None = None,
    setting_keys: tuple[str, ...] | None = None,
    setting_values: Mapping[str, tuple[str, ...]] | None = None,
    prequeried_url_helpers: set[str] | None = None,
    preserve_cover_urls: bool = False,
    public_base_url: str | None = None,
    chapter_key_templates: tuple[str, ...] | None = None,
    request_builder_helpers: set[str] | None = None,
    remove_extern_std: bool = False,
    trace: NormalizationTrace | None = None,
) -> str:
    """Apply small type-safe compatibility rewrites for the pinned Aidoku/Rust APIs."""
    active_trace = trace or NormalizationTrace()

    content = normalize_early_control_flow(content, trace=active_trace)
    content = normalize_source_runtime_prelude(
        content,
        remove_extern_std=remove_extern_std,
        trace=active_trace,
    )
    content = normalize_graphql_request_compatibility(content, trace=active_trace)
    content = normalize_aidoku_api_paths(content, trace=active_trace)
    content = normalize_generic_response_models(content, trace=active_trace)
    content = normalize_graphql_fragment_compatibility(content, trace=active_trace)
    content = normalize_html_response_values(content, trace=active_trace)
    content = normalize_indexing_control_flow(content, trace=active_trace)
    content = normalize_graphql_projection_compatibility(content, trace=active_trace)
    content = normalize_image_request_compatibility(content, trace=active_trace)
    content = normalize_request_result_tails(content, trace=active_trace)
    content = normalize_detail_ownership(content, trace=active_trace)
    content = normalize_legacy_request_compatibility(content, trace=active_trace)
    content = normalize_runtime_setting_access(content, trace=active_trace)
    content = normalize_source_bootstrap_compatibility(content, trace=active_trace)
    content = normalize_request_response_compatibility(
        content,
        request_builder_helpers=request_builder_helpers,
        trace=active_trace,
    )
    content = normalize_source_lifecycle_compatibility(content, trace=active_trace)
    content = normalize_aidoku_model_construction(content, trace=active_trace)
    content = normalize_legacy_dynamic_filters(content, trace=active_trace)
    content = normalize_page_context_compatibility(content, trace=active_trace)
    content = normalize_deep_link_compatibility(content, trace=active_trace)
    content = normalize_chapter_date_compatibility(content, trace=active_trace)
    content = normalize_source_runtime_attributes(
        content,
        allow_dead_code=allow_dead_code,
        trace=active_trace,
    )
    content = normalize_select_filter_path_compatibility(content, trace=active_trace)
    content = normalize_aidoku_registration_imports(content, trace=active_trace)
    content = normalize_literal_url_compatibility(content, trace=active_trace)
    content = normalize_request_retry_compatibility(content, trace=active_trace)
    content = normalize_aidoku_models(content, trace=active_trace)
    content = normalize_source_path_helpers(content, trace=active_trace)
    content = normalize_aidoku_struct_defaults(content, trace=active_trace)
    content = normalize_pagination_response_models(content, trace=active_trace)
    content = normalize_pagination_ownership(content, trace=active_trace)
    content = normalize_select_filter_struct_compatibility(content, trace=active_trace)
    content = normalize_image_url_compatibility(content, trace=active_trace)
    content = normalize_iteration_control_flow(content, trace=active_trace)
    content = normalize_filter_predicate_compatibility(content, trace=active_trace)
    content = normalize_ir_source_urls(
        content,
        prequeried_url_helpers=prequeried_url_helpers,
        public_base_url=public_base_url,
        chapter_key_templates=chapter_key_templates,
        trace=active_trace,
    )
    content = normalize_late_control_flow(content, trace=active_trace)
    content = normalize_preserved_source_urls(
        content,
        preserve_cover_urls=preserve_cover_urls,
        trace=active_trace,
    )
    content = normalize_typed_domain_setting(content, trace=active_trace)
    content = normalize_aidoku_model_string_literals(content, trace=active_trace)
    content = normalize_request_header_values(content, trace=active_trace)
    content = normalize_generation_settings(
        content,
        setting_defaults=setting_defaults,
        setting_keys=setting_keys,
        setting_values=setting_values,
        trace=active_trace,
    )
    content = finalize_aidoku_imports(content, trace=active_trace)
    content = finalize_chapter_date_compatibility(content, trace=active_trace)
    return content
