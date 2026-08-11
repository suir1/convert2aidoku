from __future__ import annotations

import json
import re

from .normalization_trace import NormalizationTrace
from .rust_inspection import RustInspection


def _normalize_base_url_provider(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        if function.name != "get_base_url":
            continue
        normalized = re.sub(r"->\s*String\s*\{", "-> Result<String> {", function.text, count=1)
        if normalized == function.text:
            continue
        tail = re.search(
            r"\n(?P<indent>\s*)(?P<expression>[^;\n]+)\s*\n\s*\}$",
            normalized,
        )
        if tail is not None and not tail.group("expression").lstrip().startswith("Ok("):
            normalized = (
                normalized[: tail.start()]
                + f"\n{tail.group('indent')}Ok({tail.group('expression').strip()})\n}}"
            )
        replacements.append((function.text, normalized))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    return content


def _normalize_comic_path_helper(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).functions:
        if function.name not in {"url2comic_path", "extract_comic_path"}:
            continue
        if '.split("/comic/")' not in function.text or "unwrap_or" not in function.text:
            continue
        opening = function.text.find("{")
        argument = re.search(
            r"\(\s*(?P<name>[A-Za-z_]\w*)\s*:\s*&str\b",
            function.text[:opening],
        )
        if opening < 0 or argument is None:
            continue
        name = argument.group("name")
        replacement = (
            function.text[:opening].rstrip()
            + " {\n"
            + f'    if let Some((_, path)) = {name}.split_once("/comic/") {{\n'
            + "        path.to_string()\n"
            + f'    }} else if let Some((_, path)) = {name}.split_once("/comic2/") {{\n'
            + "        path.to_string()\n"
            + "    } else {\n"
            + f"        {name}.trim_start_matches('/').to_string()\n"
            + "    }\n"
            + "}"
        )
        replacements.append((function.text, replacement))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    return re.sub(
        r"(?P<root>\b[A-Za-z_]\w*)\s*\.\s*(?P<field>[A-Za-z_]\w*)"
        r"\s*\.strip_prefix\(\s*(?P<first>\"(?:\\.|[^\"\\])*\")\s*\)"
        r"\s*\.unwrap_or\(\s*&(?P=root)\s*\.\s*(?P=field)\s*\)"
        r"\s*\.strip_prefix\(\s*(?P<second>\"(?:\\.|[^\"\\])*\")\s*\)"
        r"\s*\.unwrap_or\(\s*&(?P=root)\s*\.\s*(?P=field)\s*\)",
        lambda match: (
            f"{match.group('root')}.{match.group('field')}"
            f".strip_prefix({match.group('first')})"
            f".or_else(|| {match.group('root')}.{match.group('field')}"
            f".strip_prefix({match.group('second')}))"
            f".unwrap_or(&{match.group('root')}.{match.group('field')})"
        ),
        content,
    )


def _normalize_deep_link_defaults(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for node in RustInspection.from_content(content).nodes("struct_expression"):
        name = node.child_by_field_name("name")
        if (
            name is None
            or re.search(
                r"(?:^|::)DeepLinkResult::",
                name.text.decode("utf-8", errors="replace"),
            )
            is None
        ):
            continue
        original = node.text.decode("utf-8", errors="replace")
        normalized = re.sub(r"(?m)^\s*\.\.Default::default\(\)\s*,?\s*\n?", "", original)
        if normalized != original:
            replacements.append((original, normalized))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    return content


def _normalize_absolute_deep_link_paths(content: str) -> str:
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).named("handle_deep_link"):
        if '"/comic/"' not in function.text:
            continue
        normalized = function.text
        normalized = re.sub(
            r"(?m)^(?P<indent>[ \t]*)let\s+(?P<path>[A-Za-z_]\w*)\s*=\s*"
            r"(?P<url>[A-Za-z_]\w*)\.as_str\(\);",
            lambda match: (
                f"{match.group('indent')}let {match.group('path')} = {match.group('url')}\n"
                f'{match.group("indent")}    .split_once("://")\n'
                f"{match.group('indent')}    .and_then(|(_, value)| "
                "value.find('/').map(|index| &value[index..]))\n"
                f"{match.group('indent')}    .unwrap_or({match.group('url')}.as_str());"
            ),
            normalized,
            count=1,
        )
        if "then_some(url.as_str())" not in normalized:
            normalized = re.sub(
                r"(?P<url>[A-Za-z_]\w*)\.strip_prefix\("
                r'(?P<base>"https?://(?:\\.|[^"\\])*")\)',
                r"\g<url>.strip_prefix(\g<base>).or_else(|| "
                r"\g<url>.starts_with('/').then_some(\g<url>.as_str()))",
                normalized,
                count=1,
            )
        normalized = re.sub(
            r"(?P<value>[A-Za-z_]\w*)\.strip_prefix\(\"/comic/\"\)",
            r'\g<value>.split_once("/comic/").map(|(_, rest)| rest)',
            normalized,
        )
        if "DeepLinkResult::Chapter" not in normalized:
            normalized = re.sub(
                r"(?m)^(?P<indent>[ \t]*)if\s+(?P<path>[A-Za-z_]\w*)"
                r'\.starts_with\("/comic/"\)\s*\{',
                lambda match: (
                    f"{match.group('indent')}if let Some((manga_id, chapter_id)) = "
                    f'{match.group("path")}.split_once("/comic/")\n'
                    f"{match.group('indent')}    .map(|(_, rest)| rest)\n"
                    f"{match.group('indent')}    .and_then(|rest| "
                    'rest.split_once("/chapter/"))\n'
                    f"{match.group('indent')}{{\n"
                    f"{match.group('indent')}    let manga_key = "
                    'format!("/comic/{}", manga_id);\n'
                    f"{match.group('indent')}    let key = "
                    'format!("{}/chapter/{}", manga_key, chapter_id);\n'
                    f"{match.group('indent')}    return Ok(Some(DeepLinkResult::Chapter "
                    "{ manga_key, key }));\n"
                    f"{match.group('indent')}}}\n"
                    f'{match.group("indent")}if {match.group("path")}.starts_with("/comic/") {{'
                ),
                normalized,
                count=1,
            )
        normalized = re.sub(
            r'\bparts\.len\(\)\s*>=\s*2\s*&&\s*parts\[1\]\s*==\s*"chapter"',
            'parts.len() >= 3 && parts[1] == "chapter"',
            normalized,
        )
        route_nodes = []
        for node in RustInspection.from_content(normalized).nodes("if_expression"):
            condition = node.child_by_field_name("condition")
            if condition is None:
                continue
            condition_text = RustInspection.compact_node(condition)
            node_text = node.text.decode("utf-8", errors="replace")
            if (
                condition_text == 'path.starts_with("/comic/")'
                and "DeepLinkResult::Manga" in node_text
            ):
                route_nodes.append(("manga", node))
            elif (
                condition_text == 'path.starts_with("/comic/chapter/")'
                and "DeepLinkResult::Chapter" in node_text
            ):
                route_nodes.append(("chapter", node))
        manga = next((node for kind, node in route_nodes if kind == "manga"), None)
        chapter = next((node for kind, node in route_nodes if kind == "chapter"), None)
        if (
            manga is not None
            and chapter is not None
            and manga.start_byte < chapter.start_byte
            and manga.parent is not None
            and chapter.parent is not None
            and manga.parent.parent == chapter.parent.parent
        ):
            encoded = normalized.encode("utf-8")
            between = encoded[manga.end_byte : chapter.start_byte]
            normalized = (
                encoded[: manga.start_byte]
                + encoded[chapter.start_byte : chapter.end_byte]
                + between
                + encoded[manga.start_byte : manga.end_byte]
                + encoded[chapter.end_byte :]
            ).decode("utf-8")
        if normalized != function.text:
            replacements.append((function.text, normalized))
    for original, normalized in replacements:
        content = content.replace(original, normalized, 1)
    return content


def _normalize_prequeried_url_helpers(content: str, helpers: set[str] | None) -> str:
    for helper in helpers or set():
        function = rf"(?:[A-Za-z_][A-Za-z0-9_]*::)*{re.escape(helper)}\s*\("
        content = re.sub(
            rf'(?P<head>"\{{\}}(?:\\.|[^"\\])*?)\?'
            rf'(?P<tail>(?:\\.|[^"\\])*"\s*,\s*{function})',
            r"\g<head>&\g<tail>",
            content,
        )
    return content


def _normalize_public_absolute_url(content: str, public_base_url: str | None) -> str:
    if not public_base_url:
        return content
    replacements: list[tuple[str, str]] = []
    for function in RustInspection.from_content(content).named("absolute_url"):
        opening = function.text.find("{")
        if opening < 0:
            continue
        argument = re.search(
            r"\(\s*(?:mut\s+)?(?P<name>[A-Za-z_]\w*)\s*:\s*&str\b",
            function.text[:opening],
        )
        if argument is None:
            continue
        base = public_base_url.rstrip("/")
        replacement = (
            function.text[:opening].rstrip()
            + " {\n"
            + f'    if {argument.group("name")}.starts_with("http://") '
            + f'|| {argument.group("name")}.starts_with("https://") {{\n'
            + f"        aidoku::alloc::String::from({argument.group('name')})\n"
            + "    } else {\n"
            + f'        format!("{{}}/{{}}", {json.dumps(base)}, '
            + f"{argument.group('name')}.trim_start_matches('/'))\n"
            + "    }\n"
            + "}"
        )
        replacements.append((function.text, replacement))
    for original, replacement in replacements:
        content = content.replace(original, replacement, 1)
    base = public_base_url.rstrip("/")
    has_relative_model = False
    for node in RustInspection.from_content(content).nodes("struct_expression"):
        name = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name is None or body is None:
            continue
        type_name = name.text.decode("utf-8", errors="replace")
        fields = set()
        for child in body.named_children:
            field = child.child_by_field_name("field")
            if field is not None:
                fields.add(field.text.decode("utf-8", errors="replace"))
            elif child.type == "shorthand_field_initializer":
                fields.add(child.text.decode("utf-8", errors="replace"))
        if type_name in {"Manga", "Chapter", "aidoku::Manga", "aidoku::Chapter"} and (
            "key" in fields and "url" not in fields
        ):
            has_relative_model = True
            break
    has_absolute_helper = re.search(r"\bfn\s+absolute_url\s*\(", content) is not None
    if ("impl Source for" in content or has_relative_model) and not has_absolute_helper:
        content = (
            content.rstrip()
            + "\n\nfn absolute_url(relative: &str) -> String {\n"
            + '    if relative.starts_with("http://") || relative.starts_with("https://") {\n'
            + "        aidoku::alloc::String::from(relative)\n"
            + "    } else {\n"
            + f'        format!("{{}}/{{}}", {json.dumps(base)}, '
            + "relative.trim_start_matches('/'))\n"
            + "    }\n}\n"
        )
        has_absolute_helper = True
    if has_absolute_helper:
        content = re.sub(
            r"(?m)^(?P<indent>[ \t]*)(?P<target>manga|chapter)\.key\s*=\s*"
            r"(?P<value>[^;\n]+);(?!\s*\n[ \t]*(?P=target)\.url\s*=)",
            lambda match: (
                match.group(0)
                + f"\n{match.group('indent')}{match.group('target')}.url = "
                + f"Some(absolute_url(&{match.group('target')}.key));"
            ),
            content,
        )
        struct_edits: list[tuple[int, int, bytes]] = []
        for node in RustInspection.from_content(content).nodes("struct_expression"):
            name = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if name is None or body is None:
                continue
            type_name = name.text.decode("utf-8", errors="replace")
            if type_name not in {"Manga", "Chapter", "aidoku::Manga", "aidoku::Chapter"}:
                continue
            fields = {}
            for child in body.named_children:
                field_name = child.child_by_field_name("field")
                if field_name is not None:
                    fields[field_name.text.decode("utf-8", errors="replace")] = child
                elif child.type == "shorthand_field_initializer":
                    fields[child.text.decode("utf-8", errors="replace")] = child
            if "url" in fields or "key" not in fields:
                continue
            key = fields["key"]
            value = key.child_by_field_name("value")
            expression = (
                value.text.decode("utf-8", errors="replace")
                if value is not None
                else key.text.decode("utf-8", errors="replace")
            )
            key_value = (
                f"{expression}.clone()" if re.fullmatch(r"[A-Za-z_]\w*", expression) else expression
            )
            replacement = f"key: {key_value}, url: Some(absolute_url(&({expression})))"
            struct_edits.append((key.start_byte, key.end_byte, replacement.encode("utf-8")))
        encoded = content.encode("utf-8")
        for start, end, replacement in reversed(struct_edits):
            encoded = encoded[:start] + replacement + encoded[end:]
        content = encoded.decode("utf-8")
    return content


def _normalize_chapter_key_templates(
    content: str,
    chapter_key_templates: tuple[str, ...] | None,
) -> str:
    for template in chapter_key_templates or ():
        expected = template.replace("{comic_path}", "{}").replace("{chapter_id}", "{}")
        first_placeholder = expected.find("{}")
        if first_placeholder <= 0:
            continue
        expected_literal = json.dumps(expected)
        key_prefix = expected[:first_placeholder]
        shortened = expected[first_placeholder:]
        candidates = {shortened, "/" + shortened.lstrip("/")}
        shortened_literal = json.dumps(shortened)
        restored: list[tuple[str, str]] = []
        for node in RustInspection.from_content(content).nodes("if_expression"):
            condition = node.child_by_field_name("condition")
            consequence = node.child_by_field_name("consequence")
            alternative = node.child_by_field_name("alternative")
            if condition is None or consequence is None or alternative is None:
                continue
            alternative_blocks = [
                child for child in alternative.named_children if child.type == "block"
            ]
            condition_text = condition.text.decode("utf-8", errors="replace")
            if (
                consequence.type != "block"
                or len(alternative_blocks) != 1
                or re.search(
                    rf"\.starts_with\(\s*{re.escape(json.dumps(key_prefix))}\s*\)",
                    condition_text,
                )
                is None
                or RustInspection.compact_node(consequence)
                != RustInspection.compact_node(alternative_blocks[0])
            ):
                continue
            consequence_text = consequence.text.decode("utf-8", errors="replace")
            if expected_literal not in consequence_text:
                continue
            original = node.text.decode("utf-8", errors="replace")
            restored.append(
                (
                    original,
                    original.replace(
                        consequence_text,
                        consequence_text.replace(expected_literal, shortened_literal, 1),
                        1,
                    ),
                )
            )
        for original, replacement in restored:
            content = content.replace(original, replacement, 1)

        def replace_unguarded(
            match: re.Match[str],
            current_content: str = content,
            prefix_literal: str = key_prefix,
            replacement_literal: str = expected_literal,
        ) -> str:
            window = current_content[max(0, match.start() - 500) : match.start()]
            guard = re.search(
                rf"if\s+(?P<value>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)"
                rf"\.starts_with\(\s*{re.escape(json.dumps(prefix_literal))}\s*\)"
                r"\s*\{[^{}]*$",
                window,
            )
            arguments = current_content[match.end() : match.end() + 300]
            if guard is not None and re.match(
                rf"\s*,\s*{re.escape(guard.group('value'))}\s*,",
                arguments,
            ):
                return match.group(0)
            return match.group("prefix") + replacement_literal

        for candidate in candidates:
            if candidate == expected:
                continue
            content = re.sub(
                rf"(?P<prefix>\bformat!\(\s*){re.escape(json.dumps(candidate))}(?=\s*,)",
                replace_unguarded,
                content,
            )
    return content


def _normalize_preserved_cover_urls(content: str, preserve_cover_urls: bool) -> str:
    if not preserve_cover_urls:
        return content
    content = re.sub(
        r"(?P<receiver>\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
        r"\s*\.cover\s*\.as_deref\(\)\s*\.map\(\|\s*(?P<value>[A-Za-z_][A-Za-z0-9_]*)"
        r"\s*\|\s*(?:[A-Za-z_][A-Za-z0-9_]*::)*"
        r"[A-Za-z_][A-Za-z0-9_]*resolution[A-Za-z0-9_]*\(\s*(?P=value)\s*,[^)]*\)"
        r"\)\s*\.unwrap_or_default\(\)",
        r"\g<receiver>.cover.clone().unwrap_or_default()",
        content,
    )
    content = re.sub(
        r"(?:[A-Za-z_][A-Za-z0-9_]*::)*[A-Za-z_][A-Za-z0-9_]*resolution"
        r"[A-Za-z0-9_]*\(\s*&(?P<receiver>[A-Za-z_][A-Za-z0-9_]*(?:\."
        r"[A-Za-z_][A-Za-z0-9_]*)*)\.cover\s*,[^)]*\)",
        r"\g<receiver>.cover.clone()",
        content,
    )
    return re.sub(
        r"(?:[A-Za-z_][A-Za-z0-9_]*::)*[A-Za-z_][A-Za-z0-9_]*"
        r"(?:resolution|image_url)[A-Za-z0-9_]*\(\s*&(?P<receiver>"
        r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\.cover)"
        r"\s*(?:,[^()]*)?\)",
        r"\g<receiver>.clone()",
        content,
    )


def _normalize_clone_absolute_request_url(content: str) -> str:
    if re.search(r"let\s+host\s*=\s*absolute\b", content):
        return content.replace("Request::get(absolute)?", "Request::get(absolute.clone())?")
    return content


def _normalize_chapter_route_double_slash(content: str) -> str:
    return re.sub(
        r'"(?:\\.|[^"\\])*"',
        lambda match: match.group(0).replace("}//", "}/"),
        content,
    )


def _normalize_named_base_literal_url_joins(content: str) -> str:
    pattern = re.compile(
        r'(?P<head>\bformat!\s*\(\s*")\{\}(?P<path>[A-Za-z0-9])'
        r'(?P<literal>(?:\\.|[^"\\])*)"(?P<separator>\s*,\s*)'
        r"(?P<provider>(?:self\s*\.\s*)?[A-Za-z_]\w*(?:\s*\(\s*\))?)"
        r"(?P<end>\s*(?=,|\)))"
    )

    def replace(match: re.Match[str]) -> str:
        identifiers = re.findall(r"[A-Za-z_]\w*", match.group("provider"))
        provider_name = identifiers[-1] if identifiers else ""
        if not {"base", "domain", "origin"}.intersection(provider_name.split("_")):
            return match.group(0)
        return (
            match.group("head")
            + "{}/"
            + match.group("path")
            + match.group("literal")
            + '"'
            + match.group("separator")
            + match.group("provider")
            + match.group("end")
        )

    return pattern.sub(replace, content)


def normalize_deep_link_compatibility(content: str, *, trace: NormalizationTrace) -> str:
    for rewrite in (
        _normalize_deep_link_defaults,
        _normalize_absolute_deep_link_paths,
    ):
        content = trace.apply(rewrite.__name__.removeprefix("_"), content, rewrite)
    return content


def normalize_literal_url_compatibility(content: str, *, trace: NormalizationTrace) -> str:
    content = trace.apply(
        "clone_absolute_request_url", content, _normalize_clone_absolute_request_url
    )
    content = trace.apply(
        "named_base_literal_url_joins",
        content,
        _normalize_named_base_literal_url_joins,
    )
    return trace.apply("chapter_route_double_slash", content, _normalize_chapter_route_double_slash)


def normalize_source_path_helpers(content: str, *, trace: NormalizationTrace) -> str:
    for rewrite in (_normalize_base_url_provider, _normalize_comic_path_helper):
        content = trace.apply(rewrite.__name__.removeprefix("_"), content, rewrite)
    return content


def normalize_ir_source_urls(
    content: str,
    *,
    prequeried_url_helpers: set[str] | None,
    public_base_url: str | None,
    chapter_key_templates: tuple[str, ...] | None,
    trace: NormalizationTrace,
) -> str:
    content = trace.apply(
        "normalize_prequeried_url_helpers",
        content,
        lambda value: _normalize_prequeried_url_helpers(value, prequeried_url_helpers),
    )
    content = trace.apply(
        "normalize_public_absolute_url",
        content,
        lambda value: _normalize_public_absolute_url(value, public_base_url),
    )
    return trace.apply(
        "normalize_chapter_key_templates",
        content,
        lambda value: _normalize_chapter_key_templates(value, chapter_key_templates),
    )


def normalize_preserved_source_urls(
    content: str,
    *,
    preserve_cover_urls: bool,
    trace: NormalizationTrace,
) -> str:
    return trace.apply(
        "normalize_preserved_cover_urls",
        content,
        lambda value: _normalize_preserved_cover_urls(value, preserve_cover_urls),
    )
