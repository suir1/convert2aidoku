from __future__ import annotations

import re

from .constants import AIDOKU_ROOT_NAMES as _AIDOKU_ROOT_NAMES
from .decompiled_input import (
    decompiled_dto_shapes,
    decompiled_nullable_dto_fields,
    decompiled_rank_list_wraps_comic,
)
from .models import GeneratedFile, SourceIR
from .rust_inspection import RustInspection


def _skip_unused_decompiled_dto_fields(
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    rust_content = "\n".join(
        generated.content for generated in files if generated.path.endswith(".rs")
    )
    declared_types = set(
        re.findall(
            r"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|type|trait)\s+"
            r"([A-Za-z_]\w*)",
            rust_content,
        )
    )
    known_types = (
        declared_types
        | _AIDOKU_ROOT_NAMES
        | {
            "BTreeMap",
            "Option",
            "Result",
            "String",
            "Value",
            "Vec",
        }
    )
    updated = []
    for generated in files:
        if not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        raw = bytearray(generated.content.encode())
        edits: list[tuple[int, int, bytes]] = []
        for struct in RustInspection.from_content(generated.content).structs:
            sibling = struct.node.prev_named_sibling
            attributes = []
            while sibling is not None and sibling.type == "attribute_item":
                attributes.append(sibling.text.decode("utf-8", errors="replace"))
                sibling = sibling.prev_named_sibling
            if not any("Deserialize" in attribute for attribute in attributes):
                continue
            for field in struct.fields:
                skip_attributes = [
                    attribute
                    for attribute in field.attributes
                    if "skip_deserializing" in attribute.text.decode("utf-8", errors="replace")
                ]
                if re.search(
                    rf"\.\s*{re.escape(field.name)}\b(?!\s*\()",
                    rust_content,
                ):
                    edits.extend(
                        (attribute.start_byte, attribute.end_byte, b"")
                        for attribute in skip_attributes
                    )
                    continue
                has_skip = bool(skip_attributes)
                type_node = field.node.child_by_field_name("type")
                type_names = {
                    name for name in re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", field.type_text)
                }
                unresolved = type_names - known_types
                if type_node is not None:
                    if unresolved:
                        replacement = b"Option<serde_json::Value>"
                        edits.append((type_node.start_byte, type_node.end_byte, replacement))
                    elif not field.type_text.startswith("Option<"):
                        replacement = f"Option<{field.type_text}>".encode()
                        edits.append((type_node.start_byte, type_node.end_byte, replacement))
                if not has_skip:
                    line_start = generated.content.rfind("\n", 0, field.node.start_byte) + 1
                    prefix = generated.content[line_start : field.node.start_byte]
                    indent = prefix if not prefix.strip() else "    "
                    attribute = f"#[serde(skip_deserializing)]\n{indent}".encode()
                    edits.append((field.node.start_byte, field.node.start_byte, attribute))
        for start, end, replacement in sorted(edits, reverse=True):
            raw[start:end] = replacement
        content = raw.decode()
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


def _project_recovered_nested_dto_aliases(
    ir: SourceIR,
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    source_shapes = {shape.name: shape for shape in decompiled_dto_shapes(ir.files)}
    if not source_shapes:
        return files
    rust = RustInspection(
        generated.content for generated in files if generated.path.endswith(".rs")
    )

    def nested_source_type(java_type: str) -> str | None:
        candidates = re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", java_type)
        matches = [candidate for candidate in candidates if candidate in source_shapes]
        return matches[0] if len(matches) == 1 else None

    def nested_rust_type(rust_type: str) -> str | None:
        candidates = re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", rust_type)
        matches = [candidate for candidate in candidates if rust.struct_named(candidate)]
        return matches[0] if len(matches) == 1 else None

    rust_primitives = {
        "String": "String",
        "int": "i32",
        "Integer": "i32",
        "long": "i64",
        "Long": "i64",
        "boolean": "bool",
        "Boolean": "bool",
    }
    aliases: set[tuple[str, str, str]] = set()
    for source_owner in source_shapes.values():
        rust_owner = rust.struct_named(source_owner.name)
        if rust_owner is None:
            continue
        for source_field in source_owner.fields:
            expected_type = nested_source_type(source_field.java_type)
            rust_field = next(
                (
                    field
                    for field in rust_owner.fields
                    if field.name == source_field.name
                    or field.serialized_name == source_field.serialized_name
                ),
                None,
            )
            if expected_type is None or rust_field is None:
                continue
            actual_type = nested_rust_type(rust_field.type_text)
            if actual_type is None or actual_type == expected_type:
                continue
            expected_shape = source_shapes[expected_type]
            actual_shape = rust.struct_named(actual_type)
            assert actual_shape is not None
            if {field.serialized_name for field in expected_shape.fields} & {
                field.serialized_name for field in actual_shape.fields
            }:
                continue
            compatible = [
                (expected, actual)
                for expected in expected_shape.fields
                for actual in actual_shape.fields
                if rust_primitives.get(expected.java_type) == actual.type_text
                and expected.serialized_name != actual.serialized_name
            ]
            if len(compatible) == 1:
                expected, actual = compatible[0]
                aliases.add((actual_type, actual.name, expected.serialized_name))
    if not aliases:
        return files

    updated = []
    for generated in files:
        if not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        raw = bytearray(generated.content.encode())
        edits: list[tuple[int, bytes]] = []
        inspection = RustInspection.from_content(generated.content)
        for struct_name, field_name, alias in aliases:
            field = inspection.struct_field(struct_name, field_name)
            if field is None or any(
                f'alias = "{alias}"' in attribute.text.decode("utf-8", errors="replace")
                for attribute in field.attributes
            ):
                continue
            line_start = generated.content.rfind("\n", 0, field.node.start_byte) + 1
            prefix = generated.content[line_start : field.node.start_byte]
            indent = prefix if not prefix.strip() else "    "
            edits.append(
                (
                    field.node.start_byte,
                    f'#[serde(alias = "{alias}")]\n{indent}'.encode(),
                )
            )
        for position, replacement in sorted(edits, reverse=True):
            raw[position:position] = replacement
        content = raw.decode()
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


def _project_recovered_nullable_dto_defaults(
    ir: SourceIR,
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    nullable = decompiled_nullable_dto_fields(ir.files)
    if not nullable:
        return files
    updated = []
    for generated in files:
        if not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        encoded = generated.content.encode()
        raw = bytearray(encoded)
        edits: list[tuple[int, bytes]] = []
        inspection = RustInspection.from_content(generated.content)
        for struct_name, serialized_name in nullable:
            struct = inspection.struct_named(struct_name)
            if struct is None:
                continue
            field = next(
                (
                    candidate
                    for candidate in struct.fields
                    if candidate.name == serialized_name
                    or candidate.serialized_name == serialized_name
                ),
                None,
            )
            if field is None or any(
                re.search(r"\bserde\s*\([^)]*\bdefault\b", attribute.text.decode())
                for attribute in field.attributes
            ):
                continue
            line_start = encoded.rfind(b"\n", 0, field.node.start_byte) + 1
            prefix = encoded[line_start : field.node.start_byte].decode()
            indent = prefix if not prefix.strip() else "    "
            edits.append((field.node.start_byte, f"#[serde(default)]\n{indent}".encode()))
        for position, replacement in sorted(edits, reverse=True):
            raw[position:position] = replacement
        content = raw.decode()
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated


def _project_recovered_rank_item_wrapper(
    ir: SourceIR,
    files: list[GeneratedFile],
) -> list[GeneratedFile]:
    rust_content = "\n".join(
        generated.content for generated in files if generated.path.endswith(".rs")
    )
    if not decompiled_rank_list_wraps_comic(ir.files) or "struct C2aRankItem" in rust_content:
        return files

    inspection = RustInspection(
        generated.content for generated in files if generated.path.endswith(".rs")
    )
    updated: list[GeneratedFile] = []
    projected = False
    for generated in files:
        content = generated.content
        if projected or not generated.path.endswith(".rs"):
            updated.append(generated)
            continue
        for function in RustInspection.from_content(content).named("get_search_manga_list"):
            if "/ranks" not in function.text:
                continue
            response = re.search(
                r"(?m)^(?P<indent>[ \t]*)let\s+(?P<response>[A-Za-z_]\w*)\s*:\s*"
                r"(?P<envelope>[A-Za-z_]\w*)\s*<\s*"
                r"(?P<inner>[A-Za-z_]\w*(?:\s*<\s*[A-Za-z_]\w*\s*>)?)\s*>\s*=\s*"
                r"(?P<fetch>self\.[A-Za-z_]\w*\(\s*(?P<url>[A-Za-z_]\w*)\s*\)\?)\s*;",
                function.text,
            )
            if response is None:
                continue
            generic = re.fullmatch(
                r"[A-Za-z_]\w*\s*<\s*(?P<comic>[A-Za-z_]\w*)\s*>",
                response.group("inner"),
            )
            comic = generic.group("comic") if generic is not None else None
            if comic is None:
                list_type = inspection.struct_field_type(response.group("inner"), "list")
                list_item = (
                    re.fullmatch(r"Vec\s*<\s*(?P<comic>[A-Za-z_]\w*)\s*>", list_type)
                    if list_type is not None
                    else None
                )
                comic = list_item.group("comic") if list_item is not None else None
            if comic is None:
                continue
            direct_mapper = re.search(
                rf"\b{re.escape(response.group('response'))}\.results\.list\s*"
                r"\.into_iter\(\)\s*\.map\(\s*"
                r"(?P<mapper>[A-Za-z_]\w*::[A-Za-z_]\w*)\s*\)",
                function.text,
            )
            mapper = direct_mapper.group("mapper") if direct_mapper is not None else None
            if mapper is None:
                converter = next(
                    (
                        candidate
                        for candidate in inspection.functions
                        if re.search(
                            rf"\(\s*(?:(?P<self>&self)\s*,\s*)?[A-Za-z_]\w*\s*:\s*"
                            rf"{re.escape(comic)}\s*\)\s*->\s*Manga\b",
                            candidate.text.split("{", 1)[0],
                        )
                    ),
                    None,
                )
                if converter is not None:
                    signature = converter.text.split("{", 1)[0]
                    mapper = (
                        f"self.{converter.name}"
                        if "&self" in signature
                        else f"Self::{converter.name}"
                    )
            if mapper is None:
                continue
            indent = response.group("indent")
            inner = indent + "    "
            result = response.group("response")
            branch = (
                f'{indent}if {response.group("url")}.contains("/ranks") {{\n'
                f"{inner}let {result}: {response.group('envelope')}<C2aRankResult> = "
                f"{response.group('fetch')};\n"
                f"{inner}let has_next_page = {result}.results.total >= "
                f"{result}.results.offset + {result}.results.limit;\n"
                f"{inner}return Ok(MangaPageResult {{\n"
                f"{inner}    entries: {result}.results.list.into_iter()\n"
                f"{inner}        .map(|item| {mapper}(item.comic))\n"
                f"{inner}        .collect(),\n"
                f"{inner}    has_next_page,\n"
                f"{inner}}});\n"
                f"{indent}}}\n"
            )
            begin = function.node.start_byte + response.start()
            item = (
                "#[derive(aidoku::serde::Deserialize)]\n"
                f"struct C2aRankItem {{ comic: {comic} }}\n\n"
                "#[derive(aidoku::serde::Deserialize)]\n"
                "struct C2aRankResult {\n"
                "    #[serde(default)]\n"
                "    list: aidoku::alloc::Vec<C2aRankItem>,\n"
                "    #[serde(default)]\n"
                "    limit: i32,\n"
                "    #[serde(default)]\n"
                "    offset: i32,\n"
                "    #[serde(default)]\n"
                "    total: i32,\n"
                "}"
            )
            content = content[:begin] + branch + content[begin:]
            content = content.rstrip() + "\n\n" + item + "\n"
            projected = True
            break
        updated.append(
            generated.model_copy(update={"content": content})
            if content != generated.content
            else generated
        )
    return updated
