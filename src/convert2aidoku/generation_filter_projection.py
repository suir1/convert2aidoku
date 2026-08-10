from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from .decompiled_input import (
    decompiled_dto_shapes,
    decompiled_dynamic_filter_endpoint,
)
from .models import Capability, GeneratedFile, SourceFilterSpec, SourceIR
from .public_only_scope import public_only_filter_exclusion
from .rust_inspection import RustInspection
from .rust_inspection import last_rust_identifier as _last_rust_identifier


def _remove_trait_implementations(
    files: list[GeneratedFile],
    trait_name: str,
) -> tuple[list[GeneratedFile], bool]:
    updated: list[GeneratedFile] = []
    removed = False
    for generated in files:
        content = generated.content
        if not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        edits = [
            (implementation.start_byte, implementation.end_byte)
            for implementation in RustInspection.from_content(content).nodes("impl_item")
            if _last_rust_identifier(implementation.child_by_field_name("trait")) == trait_name
        ]
        if edits:
            encoded = content.encode("utf-8")
            for start, end in reversed(edits):
                encoded = encoded[:start] + encoded[end:]
            content = encoded.decode("utf-8")
            removed = True
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated, removed


def _prune_redundant_dynamic_settings(
    files: list[GeneratedFile],
    implemented_traits: list[str],
) -> tuple[list[GeneratedFile], list[str]]:
    """Make the generated static settings resource the single settings implementation."""
    if "DynamicSettings" not in implemented_traits:
        return files, implemented_traits
    settings = next(
        (generated for generated in files if generated.path == "res/settings.json"),
        None,
    )
    if settings is None:
        return files, implemented_traits
    try:
        if not json.loads(settings.content):
            return files, implemented_traits
    except json.JSONDecodeError:
        return files, implemented_traits

    updated, removed = _remove_trait_implementations(files, "DynamicSettings")
    if not removed:
        return files, implemented_traits
    cleaned: list[GeneratedFile] = []
    for generated in updated:
        content = generated.content
        if generated.path == "src/lib.rs":
            content = re.sub(r"\bDynamicSettings\s*,\s*", "", content)
            content = re.sub(r",\s*DynamicSettings\b", "", content)
        cleaned.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return cleaned, [trait for trait in implemented_traits if trait != "DynamicSettings"]


def _rust_filter_expression(spec: SourceFilterSpec) -> str:
    base = (
        f"id: {json.dumps(spec.id)}.into(), "
        f"title: Some({json.dumps(spec.title, ensure_ascii=False)}.into()), "
    )
    if spec.kind == "check":
        return (
            "aidoku::CheckFilter { " + base + "default: Some(false), ..Default::default() }.into()"
        )
    if spec.kind == "text":
        return "aidoku::TextFilter { " + base + "..Default::default() }.into()"
    options = ", ".join(
        f"{json.dumps(item.title, ensure_ascii=False)}.into()" for item in spec.options
    )
    ids = ", ".join(f"{json.dumps(item.value, ensure_ascii=False)}.into()" for item in spec.options)
    common = base + f"options: aidoku::alloc::vec![{options}], "
    if spec.kind == "sort":
        return (
            "aidoku::SortFilter { "
            + common
            + "default: Some(aidoku::SortFilterDefault { "
            + f"index: {spec.default_index}, ascending: "
            + str(bool(spec.default_ascending)).lower()
            + " }), ..Default::default() }.into()"
        )
    return (
        "aidoku::SelectFilter { "
        + common
        + f"ids: Some(aidoku::alloc::vec![{ids}]), "
        + f"default: Some({json.dumps(spec.options[spec.default_index].value)}.into()), "
        + "..Default::default() }.into()"
    )


def _recovered_dynamic_filter_projection(ir: SourceIR) -> dict[str, str] | None:
    endpoint = decompiled_dynamic_filter_endpoint(ir.files)
    if endpoint is None:
        return None
    behavior = [
        source.content
        for source in ir.files
        if "tagList()" in source.content
        and "Reflection.typeOf" in source.content
        and all(getter in source.content for getter in ("getName()", "getCount()", "getPathWord()"))
    ]
    if len(behavior) != 1:
        return None
    shapes = decompiled_dto_shapes(ir.files)
    by_name = {shape.name: shape for shape in shapes}
    candidates: list[tuple[Any, Any, Any]] = []
    for response in shapes:
        for list_field in response.fields:
            matched = re.fullmatch(r"(?:java\.util\.)?List<\s*([^<>]+?)\s*>", list_field.java_type)
            if matched is None:
                continue
            item = by_name.get(matched.group(1).strip())
            if item is None:
                continue
            fields = {field.name: field for field in item.fields}
            if (
                fields.get("name") is not None
                and fields["name"].java_type == "String"
                and fields.get("count") is not None
                and fields["count"].java_type in {"int", "Integer"}
                and fields.get("pathWord") is not None
                and fields["pathWord"].java_type == "String"
                and f"Reflection.typeOf({response.name}.class)" in behavior[0]
            ):
                candidates.append((response, list_field, item))
    generic_fields = [field for shape in shapes for field in shape.fields if field.java_type == "T"]
    titles: set[str] = set()
    try:
        for source in ir.files:
            if "FilterKt.getThemeFilter()" not in source.content:
                continue
            titles.update(
                json.loads(found.group(1))
                for found in re.finditer(
                    r"\bsuper\(\s*(\"(?:\\.|[^\"\\])*\")",
                    source.content,
                )
            )
    except json.JSONDecodeError:
        return None
    if len(candidates) != 1 or len(generic_fields) != 1 or len(titles) != 1:
        return None
    response, list_field, item = candidates[0]
    fields = {field.name: field for field in item.fields}
    return {
        "endpoint": endpoint,
        "envelope_path": generic_fields[0].serialized_name,
        "response_path": list_field.serialized_name,
        "name_path": fields["name"].serialized_name,
        "count_path": fields["count"].serialized_name,
        "key_path": fields["pathWord"].serialized_name,
        "title": next(iter(titles)),
    }


def deterministic_dynamic_filters_available(ir: SourceIR) -> bool:
    return (
        Capability.DYNAMIC_FILTERS in ir.capabilities
        and _recovered_dynamic_filter_projection(ir) is not None
    )


def _owned_dynamic_filter_implementation(
    ir: SourceIR,
    projection: Mapping[str, str],
    *,
    source_struct: str,
    endpoint: str,
) -> str:
    static_filters = "\n".join(
        f"        filters.push({_rust_filter_expression(spec)});" for spec in ir.filter_specs
    )
    return f"""
#[derive(aidoku::serde::Deserialize)]
struct C2aDynamicFilterEnvelope {{
    #[serde(rename = {json.dumps(projection["envelope_path"])})]
    value: C2aDynamicFilterResult,
}}

#[derive(aidoku::serde::Deserialize)]
struct C2aDynamicFilterResult {{
    #[serde(default, rename = {json.dumps(projection["response_path"])})]
    themes: aidoku::alloc::Vec<C2aDynamicFilterTheme>,
}}

#[derive(aidoku::serde::Deserialize)]
struct C2aDynamicFilterTheme {{
    #[serde(default, rename = {json.dumps(projection["name_path"])})]
    name: aidoku::alloc::String,
    #[serde(default, rename = {json.dumps(projection["count_path"])})]
    count: i32,
    #[serde(default, rename = {json.dumps(projection["key_path"])})]
    key: aidoku::alloc::String,
}}

impl aidoku::DynamicFilters for {source_struct} {{
    fn get_dynamic_filters(
        &self,
    ) -> aidoku::Result<aidoku::alloc::Vec<aidoku::Filter>> {{
        let response: C2aDynamicFilterEnvelope = crate::c2a_listing::get_json(
            crate::c2a_listing::api_url({json.dumps(endpoint)}),
        )?;
        let total: i32 = response.value.themes.iter().map(|theme| theme.count).sum();
        let mut options = aidoku::alloc::vec![aidoku::alloc::format!("全部 ({{total}})").into()];
        let mut ids = aidoku::alloc::vec!["".into()];
        for theme in response.value.themes {{
            if !theme.key.is_empty() {{
                options.push(aidoku::alloc::format!("{{}} ({{}})", theme.name, theme.count).into());
                ids.push(theme.key.into());
            }}
        }}
        let mut filters = aidoku::alloc::vec![aidoku::SelectFilter {{
            id: "theme".into(),
            title: Some({json.dumps(projection["title"], ensure_ascii=False)}.into()),
            options,
            ids: Some(ids),
            default: Some("".into()),
            ..Default::default()
        }}.into()];
{static_filters}
        Ok(filters)
    }}
}}
""".strip()


def _synthesize_recovered_dynamic_filters(
    ir: SourceIR,
    files: list[GeneratedFile],
    *,
    source_struct: str,
    implemented_traits: list[str],
) -> tuple[list[GeneratedFile], list[str]]:
    endpoint = decompiled_dynamic_filter_endpoint(ir.files)
    listing_owned = any(generated.path == "src/c2a_listing.rs" for generated in files)
    projection = _recovered_dynamic_filter_projection(ir) if listing_owned else None
    if (
        Capability.DYNAMIC_FILTERS in ir.capabilities
        and endpoint is not None
        and projection is not None
    ):
        marker = "struct C2aDynamicFilterEnvelope"
        if any(marker in generated.content for generated in files):
            traits = list(dict.fromkeys([*implemented_traits, "DynamicFilters"]))
            return files, traits
        if not endpoint.startswith("/api/v3/"):
            endpoint = "/api/v3" + endpoint
        source = next((item for item in files if item.path == "src/source.rs"), None)
        if source is None:
            return files, implemented_traits
        cleaned, _removed = _remove_trait_implementations(files, "DynamicFilters")
        implementation = _owned_dynamic_filter_implementation(
            ir,
            projection,
            source_struct=source_struct,
            endpoint=endpoint,
        )
        updated = [
            generated.model_copy(
                update={"content": generated.content.rstrip() + "\n\n" + implementation + "\n"}
            )
            if generated.path == "src/source.rs"
            else generated
            for generated in cleaned
        ]
        traits = [trait for trait in implemented_traits if trait != "DynamicFilters"]
        return updated, [*traits, "DynamicFilters"]
    inspection = RustInspection(
        generated.content for generated in files if generated.path.endswith(".rs")
    )
    if (
        Capability.DYNAMIC_FILTERS not in ir.capabilities
        or "DynamicFilters" in implemented_traits
        or endpoint is None
        or inspection.has_function("get_dynamic_filters")
    ):
        return files, implemented_traits
    envelope = next(
        (
            item.name
            for item in inspection.structs
            if inspection.struct_field_type(item.name, "results") == "T"
        ),
        None,
    )
    json_helper = next(
        (
            function.name
            for function in inspection.functions
            if ".get_json_owned()" in function.text
            and re.search(r"Result\s*<\s*T\s*>", function.text.split("{", 1)[0])
        ),
        None,
    )
    api_helper = next(
        (
            function.name
            for function in inspection.functions
            if function.name == "api_url" or function.name.endswith("_api_url")
        ),
        None,
    )
    if envelope is None or json_helper is None or api_helper is None:
        return files, implemented_traits
    routes = {route for function in inspection.functions for route in function.route_literals}
    if any(route.startswith("/api/v3/") for route in routes) and not endpoint.startswith(
        "/api/v3/"
    ):
        endpoint = "/api/v3" + endpoint
    implementation = f"""
#[derive(aidoku::serde::Deserialize)]
struct C2aDynamicFilterTheme {{
    #[serde(default)]
    name: aidoku::alloc::String,
    #[serde(default, rename = "path_word")]
    path_word: aidoku::alloc::String,
}}

#[derive(aidoku::serde::Deserialize)]
struct C2aDynamicFilterResult {{
    #[serde(default, rename = "list")]
    themes: aidoku::alloc::Vec<C2aDynamicFilterTheme>,
}}

impl DynamicFilters for {source_struct} {{
    fn get_dynamic_filters(&self) -> Result<aidoku::alloc::Vec<Filter>> {{
        let response: {envelope}<C2aDynamicFilterResult> =
            self.{json_helper}(Self::{api_helper}({json.dumps(endpoint)}))?;
        let mut options = aidoku::alloc::vec!["全部".into()];
        let mut ids = aidoku::alloc::vec!["".into()];
        for theme in response.results.themes {{
            if !theme.path_word.is_empty() {{
                options.push(theme.name.into());
                ids.push(theme.path_word.into());
            }}
        }}
        Ok(aidoku::alloc::vec![aidoku::SelectFilter {{
            id: "theme".into(),
            title: Some("題材".into()),
            options,
            ids: Some(ids),
            default: Some("".into()),
            ..Default::default()
        }}.into()])
    }}
}}
""".strip()

    updated: list[GeneratedFile] = []
    projected = False
    owner_pattern = re.compile(rf"\bimpl\s+{re.escape(source_struct)}\s*\{{")
    for generated in files:
        content = generated.content
        if not projected and generated.path.endswith(".rs") and owner_pattern.search(content):
            content = content.rstrip() + "\n\n" + implementation + "\n"
            projected = True
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    if not projected:
        return files, implemented_traits
    return updated, [*implemented_traits, "DynamicFilters"]


def _project_recovered_dynamic_filters(
    ir: SourceIR,
    files: list[GeneratedFile],
    *,
    implemented_traits: list[str],
) -> list[GeneratedFile]:
    if not ir.filter_specs or "DynamicFilters" not in implemented_traits:
        return files
    updated: list[GeneratedFile] = []
    projected = False
    for generated in files:
        content = generated.content
        if projected or not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        for function in RustInspection.from_content(content).functions:
            if function.name != "get_dynamic_filters":
                continue
            missing = [
                spec
                for spec in ir.filter_specs
                if re.search(rf"\bid\s*:\s*{re.escape(json.dumps(spec.id))}", function.text) is None
            ]
            if not missing:
                projected = True
                break
            for call in RustInspection.from_content(function.text).nodes("call_expression"):
                callee = call.child_by_field_name("function")
                arguments = call.child_by_field_name("arguments")
                if (
                    callee is None
                    or callee.text.decode("utf-8", errors="replace") != "Ok"
                    or arguments is None
                    or len(arguments.named_children) != 1
                    or call.parent is None
                    or call.parent.type != "block"
                ):
                    continue
                original = call.text.decode("utf-8", errors="replace")
                current = arguments.named_children[0].text.decode("utf-8", errors="replace")
                indent = " " * call.start_point.column
                inner = indent + "    "
                pushes = "\n".join(
                    f"{inner}c2a_filters.push({_rust_filter_expression(spec)});" for spec in missing
                )
                replacement = (
                    "{\n"
                    f"{inner}let mut c2a_filters = {current};\n"
                    f"{pushes}\n"
                    f"{inner}Ok(c2a_filters)\n"
                    f"{indent}}}"
                )
                content = content.replace(original, replacement, 1)
                projected = True
                break
            break
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


def _prune_public_only_dynamic_filters(
    ir: SourceIR,
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    if ir.feature_scope != "public_only":
        return files
    updated: list[GeneratedFile] = []
    for generated in files:
        content = generated.content
        if not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        replacements: list[str] = []
        for function in RustInspection.from_content(content).named("get_dynamic_filters"):
            for call in RustInspection.from_content(function.text).nodes("call_expression"):
                callee = call.child_by_field_name("function")
                arguments = call.child_by_field_name("arguments")
                if (
                    callee is None
                    or arguments is None
                    or not RustInspection.compact_node(callee).endswith(".push")
                ):
                    continue
                argument = arguments.text.decode("utf-8", errors="replace")
                filter_id = re.search(r'\bid\s*:\s*(?P<value>"(?:\\.|[^"\\])*")', argument)
                title = re.search(
                    r'\btitle\s*:\s*Some\(\s*(?P<value>"(?:\\.|[^"\\])*")',
                    argument,
                )
                if filter_id is None or title is None:
                    continue
                identifier = json.loads(filter_id.group("value"))
                label = json.loads(title.group("value"))
                if public_only_filter_exclusion(f"{identifier}Filter", label) is None:
                    continue
                statement = call.parent
                if statement is None or statement.type != "expression_statement":
                    continue
                replacements.append(statement.text.decode("utf-8", errors="replace"))
        for original in replacements:
            content = content.replace(original, "", 1)
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


def _project_recovered_dynamic_filter_queries(
    ir: SourceIR,
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    endpoint = decompiled_dynamic_filter_endpoint(ir.files)
    if endpoint is None or "theme" not in endpoint:
        return files
    updated: list[GeneratedFile] = []
    for generated in files:
        content = generated.content
        if not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        replacements: list[tuple[str, str]] = []
        for function in RustInspection.from_content(content).named("get_search_manga_list"):
            if '"theme"' in function.text:
                continue
            selector = re.search(
                r"(?P<selector>(?:Self::|self\.)?[A-Za-z_]\w*)"
                r"\(\s*&filters\s*,\s*\"[^\"]+\"\s*\)"
                r"\.unwrap_or\(\s*\"\"\s*\)",
                function.text,
            )
            update = re.search(
                r"(?m)^(?P<indent>[ \t]*)(?P<url>[A-Za-z_]\w*)\.push_str\("
                r'"&_update=true"\);',
                function.text,
            )
            opening = function.text.find("{")
            if selector is None or update is None or opening < 0:
                continue
            indent = update.group("indent")
            selection = (
                f"\n{indent}let c2a_theme = {selector.group('selector')}"
                '(&filters, "theme").unwrap_or("");'
            )
            normalized = function.text[: opening + 1] + selection + function.text[opening + 1 :]
            update_statement = update.group(0)
            query = (
                f"{indent}if !c2a_theme.is_empty() {{\n"
                f"{indent}    {update.group('url')}.push_str(&aidoku::alloc::format!("
                '"&theme={}", c2a_theme));\n'
                f"{indent}}}\n"
                f"{update_statement}"
            )
            normalized = normalized.replace(update_statement, query, 1)
            replacements.append((function.text, normalized))
        for original, normalized in replacements:
            content = content.replace(original, normalized, 1)
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


_CHECK_FILTER_HELPER = """
fn c2a_check_value(filters: &[aidoku::FilterValue], id: &str) -> bool {
    filters.iter().any(|filter| {
        matches!(
            filter,
            aidoku::FilterValue::Check { id: filter_id, value }
                if filter_id == id && *value > 0
        )
    })
}
""".strip()


def _project_recovered_check_filter_mappings(
    ir: SourceIR,
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    check_ids = [spec.id for spec in ir.filter_specs if spec.kind == "check"]
    if not check_ids:
        return files
    updated: list[GeneratedFile] = []
    for generated in files:
        content = generated.content
        if not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        normalized = content
        for filter_id in check_ids:
            literal = re.escape(json.dumps(filter_id))
            normalized = re.sub(
                rf"(?:Self::|self\.)?[A-Za-z_]\w*\(\s*"
                rf"(?P<filters>&?[A-Za-z_]\w*)\s*,\s*{literal}\s*\)\.is_some\(\)",
                lambda match, filter_id=filter_id: (
                    f"c2a_check_value({match.group('filters')}, {json.dumps(filter_id)})"
                ),
                normalized,
            )
        if normalized != content and "fn c2a_check_value(" not in normalized:
            normalized = normalized.rstrip() + "\n\n" + _CHECK_FILTER_HELPER + "\n"
        updated.append(
            generated.model_copy(update={"content": normalized})
            if normalized != content
            else generated
        )
    return updated
