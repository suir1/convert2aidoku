from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from .generation_filter_projection import (
    _project_recovered_check_filter_mappings,
    _project_recovered_dynamic_filter_queries,
    _project_recovered_dynamic_filters,
    _prune_public_only_dynamic_filters,
    _prune_redundant_dynamic_settings,
    _synthesize_recovered_dynamic_filters,
)
from .generation_request_projection import (
    _prequeried_url_helpers,
    _project_recovered_chapter_image_resolution,
    _project_recovered_chapter_page_variants,
    _project_recovered_detail_api_envelope,
    _project_recovered_request_headers,
    _request_builder_helpers,
)
from .generation_response_projection import (
    _project_recovered_nested_dto_aliases,
    _project_recovered_nullable_dto_defaults,
    _project_recovered_rank_item_wrapper,
    _skip_unused_decompiled_dto_fields,
)
from .generation_setting_compatibility import (
    project_user_agent_setting as _project_user_agent_setting,
)
from .models import Capability, GeneratedFile, GeneratedResources, GenerationManifest, SourceIR
from .normalization_trace import NormalizationTrace
from .request_policy import RequestPolicy
from .rust_compatibility import normalize_pinned_aidoku_rust as _normalize_pinned_aidoku_rust
from .rust_inspection import RustInspection
from .rust_inspection import last_rust_identifier as _last_rust_identifier


@dataclass(frozen=True)
class _ProjectionContext:
    ir: SourceIR
    manifest: GenerationManifest
    setting_defaults: dict[str, str]
    setting_keys: set[str]
    setting_values: dict[str, tuple[str, ...]]
    prequeried_url_helpers: set[str]
    request_builder_helpers: set[str]
    request_policy: RequestPolicy
    preserve_cover_urls: bool
    trace: NormalizationTrace | None

    @classmethod
    def build(
        cls,
        ir: SourceIR,
        manifest: GenerationManifest,
        trace: NormalizationTrace | None,
    ) -> _ProjectionContext:
        resources = GeneratedResources(manifest)
        return cls(
            ir=ir,
            manifest=manifest,
            setting_defaults=resources.setting_defaults(),
            setting_keys=resources.setting_keys(),
            setting_values=resources.setting_values(),
            prequeried_url_helpers=_prequeried_url_helpers(manifest),
            request_builder_helpers=_request_builder_helpers(manifest),
            request_policy=RequestPolicy.from_source_ir(ir),
            preserve_cover_urls=bool(
                ir.image_url_policy and ir.image_url_policy.preserve_cover_urls
            ),
            trace=trace,
        )


@dataclass(frozen=True)
class _ProjectionState:
    files: list[GeneratedFile]
    implemented_traits: list[str]


_Projection = Callable[[_ProjectionContext, _ProjectionState], _ProjectionState]
_Applicability = Callable[[_ProjectionContext], bool]


def _always(_context: _ProjectionContext) -> bool:
    return True


def _decompiled_apk(context: _ProjectionContext) -> bool:
    return context.ir.source_format == "decompiled_apk"


@dataclass(frozen=True)
class _ProjectionPass:
    rule_id: str | None
    project: _Projection
    applies: _Applicability = _always

    def apply(
        self,
        context: _ProjectionContext,
        state: _ProjectionState,
    ) -> tuple[_ProjectionState, bool]:
        if not self.applies(context):
            return state, False
        projected = self.project(context, state)
        changed = projected != state
        if self.rule_id is not None and context.trace is not None:
            context.trace.hit(self.rule_id, changed=changed)
        return projected, changed


def _with_files(state: _ProjectionState, files: list[GeneratedFile]) -> _ProjectionState:
    return _ProjectionState(files=files, implemented_traits=state.implemented_traits)


def _project_generated_return_ownership(files: list[GeneratedFile]) -> list[GeneratedFile]:
    """Project call sites from unambiguous generated helper return types."""
    inspection = RustInspection(
        generated.content for generated in files if generated.path.endswith(".rs")
    )
    return_kinds: dict[str, set[str]] = {}
    for function in inspection.functions:
        return_type = function.node.child_by_field_name("return_type")
        compact = RustInspection.compact_node(return_type) if return_type is not None else ""
        if re.fullmatch(r"(?:aidoku::)?Result<.+>", compact):
            kind = "aidoku_result"
        elif re.fullmatch(r"Option<&Vec<.+>>", compact):
            kind = "borrowed_vec"
        else:
            kind = "other"
        return_kinds.setdefault(function.name, set()).add(kind)
    aidoku_results = {name for name, kinds in return_kinds.items() if kinds == {"aidoku_result"}}
    borrowed_vecs = {name for name, kinds in return_kinds.items() if kinds == {"borrowed_vec"}}
    if not aidoku_results and not borrowed_vecs:
        return files

    updated: list[GeneratedFile] = []
    for generated in files:
        if not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        edits: list[tuple[int, int, bytes]] = []
        for call in RustInspection.from_content(generated.content).nodes("call_expression"):
            callee = call.child_by_field_name("function")
            arguments = call.child_by_field_name("arguments")
            if callee is None or callee.type != "field_expression" or arguments is None:
                continue
            field = callee.child_by_field_name("field")
            receiver = callee.child_by_field_name("value")
            if field is None or receiver is None or receiver.type != "call_expression":
                continue
            receiver_callee = receiver.child_by_field_name("function")
            helper_node = (
                receiver_callee.child_by_field_name("field")
                if receiver_callee is not None and receiver_callee.type == "field_expression"
                else receiver_callee
            )
            helper = (
                helper_node.text.decode("utf-8", errors="replace").rsplit("::", 1)[-1]
                if helper_node is not None
                else None
            )
            if field.text == b"cloned" and not arguments.named_children:
                if helper not in borrowed_vecs:
                    continue
                receiver_text = receiver.text.decode("utf-8", errors="replace")
                replacement = f"{receiver_text}.map(|values| values.as_slice())"
                edits.append((call.start_byte, call.end_byte, replacement.encode()))
                continue
            if (
                field.text != b"map_err"
                or helper not in aidoku_results
                or len(arguments.named_children) != 1
            ):
                continue
            mapper = RustInspection.compact_node(arguments.named_children[0])
            if (
                re.fullmatch(
                    r"\|(?P<error>[A-Za-z_]\w*)\|(?:aidoku::)?AidokuError::message\("
                    r"(?P=error)\)",
                    mapper,
                )
                is not None
            ):
                edits.append((call.start_byte, call.end_byte, receiver.text))
        encoded = generated.content.encode("utf-8")
        for begin, end, replacement in sorted(edits, reverse=True):
            encoded = encoded[:begin] + replacement + encoded[end:]
        content = encoded.decode("utf-8")
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


def _project_recovered_kotlin_chapters(
    ir: SourceIR,
    files: list[GeneratedFile],
    *,
    setting_defaults: Mapping[str, str],
    setting_values: Mapping[str, tuple[str, ...]],
) -> list[GeneratedFile]:
    """Materialize a standard Kotlin ChapterDto mapping when every behavior fact is proven."""
    if ir.source_format != "kotlin_module" or Capability.CHAPTERS not in ir.capabilities:
        return files
    input_content = "\n".join(source.content for source in ir.files)
    if not all(
        marker in input_content
        for marker in (
            "fun toSChapter(",
            "date_upload",
            "scanlator = typeName",
            "chapter_number",
            ".toFloatOrNull()",
        )
    ):
        return files
    type_pairs = re.findall(
        r'"(?P<kind>[^"\\]+)"\s*->\s*Pair\('
        r'"(?P<suffix>[^"\\]*)"\s*,\s*"(?P<scanlator>[^"\\]*)"\)',
        input_content,
    )
    route_match = re.search(
        r'\burl\s*=\s*"\$[A-Za-z_]\w*(?P<route>/[^"$]*?)'
        r'\$\{[^}\n]*\.id\}"',
        input_content,
    )
    date_formats = set(re.findall(r'SimpleDateFormat\("([^"\\]+)"', input_content))
    if not type_pairs or route_match is None or len(date_formats) != 1:
        return files
    if "${this@ChapterDto.serial}$suffix" not in input_content or "size}P）" not in input_content:
        return files

    inspection = RustInspection(
        generated.content for generated in files if generated.path.endswith(".rs")
    )
    required = {"id", "serial", "type", "size", "dateCreated"}
    chapter_candidates = [
        struct
        for struct in inspection.structs
        if required <= {field.serialized_name for field in struct.fields}
    ]
    if len(chapter_candidates) != 1:
        return files
    chapter_dto = chapter_candidates[0]
    chapter_fields = {field.serialized_name: field.name for field in chapter_dto.fields}
    container_candidates = [
        (struct, field)
        for struct in inspection.structs
        for field in struct.fields
        if field.serialized_name == "chaptersByComicId" and chapter_dto.name in field.type_text
    ]
    if len(container_candidates) != 1:
        return files
    chapter_collection = container_candidates[0][1].name
    setting_candidates = [
        key
        for key, values in setting_values.items()
        if "all" in values and {kind for kind, _suffix, _scanlator in type_pairs} <= set(values)
    ]
    if len(setting_candidates) != 1:
        return files
    setting_key = setting_candidates[0]
    setting_default = setting_defaults.get(setting_key, "all")

    source_file = next(
        (
            generated
            for generated in files
            if generated.path.endswith(".rs")
            and "fn get_manga_update" in generated.content
            and f".{chapter_collection}" in generated.content
        ),
        None,
    )
    dto_file = next(
        (
            generated
            for generated in files
            if generated.path.endswith(".rs") and f"struct {chapter_dto.name}" in generated.content
        ),
        None,
    )
    if source_file is None or dto_file is None or "c2a_into_chapter" in source_file.content:
        return files

    source_content = source_file.content
    replacement = None
    for function in RustInspection.from_content(source_content).named("get_manga_update"):
        compact_function = RustInspection.compact_node(function.node)
        if "needs_chapters" not in function.text or ".chapters=" in compact_function:
            continue
        access = re.search(
            rf"\b(?P<data>[A-Za-z_]\w*)\.{re.escape(chapter_collection)}\b",
            function.text,
        )
        updated = re.search(
            r"\blet\s+mut\s+(?P<updated>[A-Za-z_]\w*)\s*=\s*[A-Za-z_]\w*\s*;",
            function.text,
        )
        if access is None or updated is None:
            continue
        for branch in RustInspection.from_content(function.text).nodes("if_expression"):
            condition = branch.child_by_field_name("condition")
            consequence = branch.child_by_field_name("consequence")
            if (
                condition is None
                or RustInspection.compact_node(condition) != "needs_chapters"
                or consequence is None
                or consequence.type != "block"
            ):
                continue
            data = access.group("data")
            target = updated.group("updated")
            block = (
                "{\n"
                "            let c2a_chapter_filter = "
                "aidoku::imports::defaults::defaults_get::<aidoku::alloc::String>("
                f"{json.dumps(setting_key)}).unwrap_or_else(|| "
                f"aidoku::alloc::String::from({json.dumps(setting_default)}));\n"
                f"            if let Some(chapters) = {data}.{chapter_collection} {{\n"
                f"                let manga_key = {target}.key.clone();\n"
                "                let mut chapters = chapters.into_iter()\n"
                '                    .filter(|chapter| c2a_chapter_filter == "all" '
                "|| chapter.c2a_matches_filter(&c2a_chapter_filter))\n"
                "                    .filter_map(|chapter| chapter.c2a_into_chapter(&manga_key))\n"
                "                    .collect::<aidoku::alloc::Vec<_>>();\n"
                "                chapters.sort_by(|left, right| right.chapter_number\n"
                "                    .partial_cmp(&left.chapter_number)\n"
                "                    .unwrap_or(core::cmp::Ordering::Equal));\n"
                f"                {target}.chapters = Some(chapters);\n"
                "            }\n"
                "        }"
            )
            replacement = function.text.replace(
                consequence.text.decode("utf-8", errors="replace"),
                block,
                1,
            )
            source_content = source_content.replace(function.text, replacement, 1)
            break
        if replacement is not None:
            break
    if replacement is None:
        return files

    type_arms = "\n".join(
        f"            {json.dumps(kind, ensure_ascii=False)} => "
        f"({json.dumps(suffix, ensure_ascii=False)}, "
        f"{json.dumps(scanlator, ensure_ascii=False)}),"
        for kind, suffix, scanlator in type_pairs
    )
    route = route_match.group("route")
    date_format = next(iter(date_formats))
    dto_helper = f"""
impl {chapter_dto.name} {{
    pub(crate) fn c2a_matches_filter(&self, filter: &str) -> bool {{
        self.{chapter_fields["type"]} == filter
    }}

    pub(crate) fn c2a_into_chapter(self, manga_key: &str) -> Option<aidoku::Chapter> {{
        let (suffix, scanlator) = match self.{chapter_fields["type"]}.as_str() {{
{type_arms}
            _ => return None,
        }};
        let key = aidoku::alloc::format!(
            "{{}}{{}}{{}}",
            manga_key.trim_end_matches('/'),
            {json.dumps(route)},
            self.{chapter_fields["id"]},
        );
        Some(aidoku::Chapter {{
            key: key.clone(),
            title: Some(aidoku::alloc::format!(
                "{{}}{{}}（{{}}P）",
                self.{chapter_fields["serial"]},
                suffix,
                self.{chapter_fields["size"]},
            )),
            chapter_number: self.{chapter_fields["serial"]}.parse::<f32>().ok(),
            date_uploaded: aidoku::imports::std::parse_date(
                &self.{chapter_fields["dateCreated"]},
                {json.dumps(date_format)},
            ),
            scanlators: Some(aidoku::alloc::vec![scanlator.into()]),
            url: Some(key),
            ..Default::default()
        }})
    }}
}}
""".strip()
    dto_content = dto_file.content.rstrip() + "\n\n" + dto_helper + "\n"
    return [
        generated.model_copy(update={"content": source_content})
        if generated.path == source_file.path
        else generated.model_copy(update={"content": dto_content})
        if generated.path == dto_file.path
        else generated
        for generated in files
    ]


def _inject_import(content: str, statement: str) -> str:
    crate_attributes = re.match(r"(?:\s*#!\[[^\n]*\]\s*\n)+", content)
    if crate_attributes is None:
        return statement + "\n" + content.lstrip()
    boundary = crate_attributes.end()
    return content[:boundary] + statement + "\n" + content[boundary:]


def _expose_generated_module_items(content: str) -> str:
    content = re.sub(
        r"(?m)^(?!pub\b)(?P<kind>fn|struct|enum|type|const|static)\s+",
        r"pub(crate) \g<kind> ",
        content,
    )
    edits: list[int] = []
    for implementation in RustInspection.from_content(content).nodes("impl_item"):
        if implementation.child_by_field_name("trait") is not None:
            continue
        body = implementation.child_by_field_name("body")
        if body is None:
            continue
        edits.extend(
            item.start_byte
            for item in body.named_children
            if item.type == "function_item"
            and re.match(
                r"pub(?:\s|\()",
                item.text.decode("utf-8", errors="replace").lstrip(),
            )
            is None
        )
    encoded = content.encode("utf-8")
    for position in sorted(edits, reverse=True):
        encoded = encoded[:position] + b"pub(crate) " + encoded[position:]
    return encoded.decode("utf-8")


def _normalize_generated_module_topology(files: list[GeneratedFile]) -> list[GeneratedFile]:
    modules = {
        PurePosixPath(generated.path).stem: generated.path
        for generated in files
        if generated.path.startswith("src/")
        and generated.path.endswith(".rs")
        and generated.path not in {"src/lib.rs", "src/generated_smoke.rs"}
        and len(PurePosixPath(generated.path).parts) == 2
    }
    definitions: dict[str, str] = {}
    for generated in files:
        module = PurePosixPath(generated.path).stem
        if module not in modules:
            continue
        for name in re.findall(
            r"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?"
            r"(?:fn|struct|enum|type|const|static)\s+([A-Za-z_]\w*)",
            generated.content,
        ):
            definitions.setdefault(name, module)

    contents = {generated.path: generated.content for generated in files}
    for path in list(contents):
        if not path.endswith(".rs") or path == "src/lib.rs":
            continue
        content = contents[path]
        content = re.sub(
            r"(?m)^(?P<indent>[ \t]*)pub\s+fn\s+",
            r"\g<indent>fn ",
            content,
        )
        inspection = RustInspection.from_content(content)
        register_edits = []
        for node in inspection.nodes("macro_invocation"):
            macro = node.child_by_field_name("macro")
            if _last_rust_identifier(macro) == "register_source":
                end = node.end_byte
                encoded = content.encode("utf-8")
                while end < len(encoded) and encoded[end : end + 1] in {b" ", b"\t"}:
                    end += 1
                if encoded[end : end + 1] == b";":
                    end += 1
                if encoded[end : end + 1] == b"\n":
                    end += 1
                register_edits.append((node.start_byte, end))
        if register_edits:
            encoded = content.encode("utf-8")
            for start, end in reversed(register_edits):
                encoded = encoded[:start] + encoded[end:]
            content = encoded.decode("utf-8")
        for module in modules:
            if module == PurePosixPath(path).stem:
                continue
            content = re.sub(
                rf"(?m)^\s*mod\s+{re.escape(module)}\s*;\s*\n?",
                "",
                content,
            )
            content = re.sub(
                rf"(?m)^(?P<indent>\s*)use\s+{re.escape(module)}::",
                rf"\g<indent>use crate::{module}::",
                content,
            )
        for module in re.findall(
            r"(?m)^\s*use\s+crate::([A-Za-z_]\w*)::\*\s*;",
            content,
        ):
            owner_path = modules.get(module)
            if owner_path is not None and owner_path != path:
                contents[owner_path] = _expose_generated_module_items(contents[owner_path])
        for symbol in re.findall(r"(?m)^\s*use\s+crate::([A-Za-z_]\w*)\s*;", content):
            owner = definitions.get(symbol)
            if owner is None or owner == PurePosixPath(path).stem:
                continue
            content = re.sub(
                rf"(?m)^(?P<indent>\s*)use\s+crate::{re.escape(symbol)}\s*;",
                rf"\g<indent>use crate::{owner}::{symbol};",
                content,
            )
            owner_path = modules[owner]
            owner_content = contents[owner_path]
            owner_content = re.sub(
                rf"(?m)^(?P<indent>\s*)(?!pub\b)(?P<kind>fn|struct|enum|type|const|static)"
                rf"\s+{re.escape(symbol)}\b",
                rf"\g<indent>pub(crate) \g<kind> {symbol}",
                owner_content,
                count=1,
            )
            contents[owner_path] = owner_content
        usage_without_imports = re.sub(r"(?m)^\s*use\s+[^;]+;", "", content)
        current_module = PurePosixPath(path).stem
        for symbol, owner in definitions.items():
            if (
                owner == current_module
                or not re.fullmatch(r"[A-Z][A-Z0-9_]*", symbol)
                or re.search(rf"\b{re.escape(symbol)}\b", usage_without_imports) is None
                or re.search(rf"\b(?:crate::)?{re.escape(owner)}::{re.escape(symbol)}\b", content)
                is not None
                or re.search(rf"use\s+crate::{re.escape(owner)}::{re.escape(symbol)}", content)
                is not None
            ):
                continue
            content = _inject_import(content, f"use crate::{owner}::{symbol};")
            owner_path = modules[owner]
            owner_content = contents[owner_path]
            owner_content = re.sub(
                rf"(?m)^(?P<indent>\s*)(?!pub\b)(?P<kind>const|static)\s+"
                rf"{re.escape(symbol)}\b",
                rf"\g<indent>pub(crate) \g<kind> {symbol}",
                owner_content,
                count=1,
            )
            contents[owner_path] = owner_content
        contents[path] = content

    return [
        generated.model_copy(update={"content": contents[generated.path]})
        if contents[generated.path] != generated.content
        else generated
        for generated in files
    ]


def _synthesize_dynamic_filters(
    context: _ProjectionContext,
    state: _ProjectionState,
) -> _ProjectionState:
    files, traits = _synthesize_recovered_dynamic_filters(
        context.ir,
        state.files,
        source_struct=context.manifest.source_struct,
        implemented_traits=state.implemented_traits,
    )
    return _ProjectionState(files=files, implemented_traits=traits)


def _prune_dynamic_settings(
    _context: _ProjectionContext,
    state: _ProjectionState,
) -> _ProjectionState:
    files, traits = _prune_redundant_dynamic_settings(
        state.files,
        state.implemented_traits,
    )
    return _ProjectionState(files=files, implemented_traits=traits)


def _prune_public_only_filters(
    context: _ProjectionContext,
    state: _ProjectionState,
) -> _ProjectionState:
    return _with_files(
        state,
        _prune_public_only_dynamic_filters(context.ir, state.files),
    )


def _project_rank_item_wrapper(
    context: _ProjectionContext,
    state: _ProjectionState,
) -> _ProjectionState:
    return _with_files(
        state,
        _project_recovered_rank_item_wrapper(context.ir, state.files),
    )


def _normalize_pinned_rust(
    context: _ProjectionContext,
    state: _ProjectionState,
) -> _ProjectionState:
    files: list[GeneratedFile] = []
    for generated in state.files:
        content = generated.content
        if generated.path.endswith(".rs"):
            content = _normalize_pinned_aidoku_rust(
                content,
                allow_dead_code=generated.path != "src/lib.rs",
                setting_defaults=context.setting_defaults,
                setting_keys=context.setting_keys,
                setting_values=context.setting_values,
                prequeried_url_helpers=context.prequeried_url_helpers,
                preserve_cover_urls=context.preserve_cover_urls,
                public_base_url=(
                    context.ir.metadata.base_url if context.ir.relative_url_keys else None
                ),
                chapter_key_templates=tuple(
                    route.chapter_key_template for route in context.ir.chapter_page_routes
                ),
                request_builder_helpers=context.request_builder_helpers,
                trace=context.trace,
            )
        files.append(generated.model_copy(update={"content": content}))
    return _with_files(state, files)


def _skip_unused_dto_fields(
    _context: _ProjectionContext,
    state: _ProjectionState,
) -> _ProjectionState:
    return _with_files(state, _skip_unused_decompiled_dto_fields(state.files))


def _project_nested_dto_aliases(
    context: _ProjectionContext,
    state: _ProjectionState,
) -> _ProjectionState:
    return _with_files(
        state,
        _project_recovered_nested_dto_aliases(context.ir, state.files),
    )


def _project_nullable_dto_defaults(
    context: _ProjectionContext,
    state: _ProjectionState,
) -> _ProjectionState:
    return _with_files(
        state,
        _project_recovered_nullable_dto_defaults(context.ir, state.files),
    )


def _project_kotlin_chapters(
    context: _ProjectionContext,
    state: _ProjectionState,
) -> _ProjectionState:
    return _with_files(
        state,
        _project_recovered_kotlin_chapters(
            context.ir,
            state.files,
            setting_defaults=context.setting_defaults,
            setting_values=context.setting_values,
        ),
    )


def _project_request_headers(
    context: _ProjectionContext,
    state: _ProjectionState,
) -> _ProjectionState:
    return _with_files(
        state,
        _project_recovered_request_headers(
            context.ir,
            state.files,
            setting_defaults=context.setting_defaults,
            setting_values=context.setting_values,
        ),
    )


def _project_request_policy(
    context: _ProjectionContext,
    state: _ProjectionState,
) -> _ProjectionState:
    return _with_files(state, context.request_policy.project(state.files))


def _project_user_agent(
    context: _ProjectionContext,
    state: _ProjectionState,
) -> _ProjectionState:
    return _with_files(
        state,
        _project_user_agent_setting(
            state.files,
            context.setting_defaults,
        ),
    )


def _project_detail_envelope(
    context: _ProjectionContext,
    state: _ProjectionState,
) -> _ProjectionState:
    return _with_files(
        state,
        _project_recovered_detail_api_envelope(context.ir, state.files),
    )


def _project_chapter_page_variants(
    context: _ProjectionContext,
    state: _ProjectionState,
) -> _ProjectionState:
    return _with_files(
        state,
        _project_recovered_chapter_page_variants(context.ir, state.files),
    )


def _project_chapter_image_resolution(
    context: _ProjectionContext,
    state: _ProjectionState,
) -> _ProjectionState:
    return _with_files(
        state,
        _project_recovered_chapter_image_resolution(
            context.ir,
            state.files,
            setting_defaults=context.setting_defaults,
            setting_values=context.setting_values,
        ),
    )


def _project_dynamic_filters(
    context: _ProjectionContext,
    state: _ProjectionState,
) -> _ProjectionState:
    return _with_files(
        state,
        _project_recovered_dynamic_filters(
            context.ir,
            state.files,
            implemented_traits=state.implemented_traits,
        ),
    )


def _project_dynamic_filter_queries(
    context: _ProjectionContext,
    state: _ProjectionState,
) -> _ProjectionState:
    return _with_files(
        state,
        _project_recovered_dynamic_filter_queries(context.ir, state.files),
    )


def _project_check_filter_mappings(
    context: _ProjectionContext,
    state: _ProjectionState,
) -> _ProjectionState:
    return _with_files(
        state,
        _project_recovered_check_filter_mappings(context.ir, state.files),
    )


def _project_return_ownership(
    _context: _ProjectionContext,
    state: _ProjectionState,
) -> _ProjectionState:
    return _with_files(state, _project_generated_return_ownership(state.files))


def _project_module_topology(
    _context: _ProjectionContext,
    state: _ProjectionState,
) -> _ProjectionState:
    return _with_files(state, _normalize_generated_module_topology(state.files))


_PROJECTION_PASSES = (
    _ProjectionPass("project_synthesize_recovered_dynamic_filters", _synthesize_dynamic_filters),
    _ProjectionPass("project_prune_redundant_dynamic_settings", _prune_dynamic_settings),
    _ProjectionPass("project_prune_public_only_dynamic_filters", _prune_public_only_filters),
    _ProjectionPass("project_recovered_rank_item_wrapper", _project_rank_item_wrapper),
    _ProjectionPass(None, _normalize_pinned_rust),
    _ProjectionPass(
        "project_skip_unused_decompiled_dto_fields",
        _skip_unused_dto_fields,
        _decompiled_apk,
    ),
    _ProjectionPass(
        "project_recovered_nested_dto_aliases",
        _project_nested_dto_aliases,
        _decompiled_apk,
    ),
    _ProjectionPass(
        "project_recovered_nullable_dto_defaults",
        _project_nullable_dto_defaults,
        _decompiled_apk,
    ),
    _ProjectionPass("project_recovered_kotlin_chapters", _project_kotlin_chapters),
    _ProjectionPass("project_recovered_request_headers", _project_request_headers),
    _ProjectionPass("project_request_policy", _project_request_policy),
    _ProjectionPass("project_user_agent_setting", _project_user_agent),
    _ProjectionPass("project_recovered_detail_api_envelope", _project_detail_envelope),
    _ProjectionPass(
        "project_recovered_chapter_page_variants",
        _project_chapter_page_variants,
    ),
    _ProjectionPass(
        "project_recovered_chapter_image_resolution",
        _project_chapter_image_resolution,
    ),
    _ProjectionPass("project_recovered_dynamic_filters", _project_dynamic_filters),
    _ProjectionPass(
        "project_recovered_dynamic_filter_queries",
        _project_dynamic_filter_queries,
    ),
    _ProjectionPass(
        "project_recovered_check_filter_mappings",
        _project_check_filter_mappings,
    ),
    _ProjectionPass("project_generated_return_ownership", _project_return_ownership),
    _ProjectionPass("project_generated_module_topology", _project_module_topology),
)

MANIFEST_PROJECTION_RULE_ORDER = tuple(
    projection.rule_id for projection in _PROJECTION_PASSES if projection.rule_id is not None
)
MANIFEST_PROJECTION_RULE_IDS = frozenset(MANIFEST_PROJECTION_RULE_ORDER)


def normalize_generation_manifest(
    ir: SourceIR,
    manifest: GenerationManifest,
    *,
    trace: NormalizationTrace | None = None,
) -> GenerationManifest:
    """Apply the ordered Generation Manifest Projection registry."""
    context = _ProjectionContext.build(ir, manifest, trace)
    state = _ProjectionState(
        files=list(manifest.files),
        implemented_traits=list(manifest.implemented_traits),
    )
    changed = False
    for projection in _PROJECTION_PASSES:
        state, projected = projection.apply(context, state)
        changed |= projected
    if not changed:
        return manifest
    return manifest.model_copy(
        update={
            "files": state.files,
            "implemented_traits": state.implemented_traits,
        }
    )
