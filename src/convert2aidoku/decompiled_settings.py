from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .models import Capability, GeneratedFile, GeneratedResources, GenerationManifest, SourceIR
from .public_only_scope import (
    public_only_setting_exclusion,
    public_only_setting_reference_exclusion,
)

_STRING_LITERAL = r'"(?:\\.|[^"\\])*"'
_OPTION_VARIANT = re.compile(
    r"(?m)^\s*(?P<name>[A-Z][A-Z0-9_]*)\s*\((?P<arguments>[^\n]*)\)\s*[,;]"
)
_PREFERENCE_DECLARATION = re.compile(
    r"\b(?:final\s+)?(?:Preference|ListPreference|EditTextPreference|"
    r"SwitchPreferenceCompat)\s+(?P<name>[A-Za-z_]\w*)\s*=\s*new\s+"
    r"(?P<kind>ListPreference|EditTextPreference|SwitchPreferenceCompat)\s*\("
)


@dataclass(frozen=True)
class _OptionFacts:
    key: str
    titles: tuple[str, ...]
    values: tuple[str, ...]
    default: str | None
    variants: dict[str, str]


def _decode_string(literal: str) -> str | None:
    try:
        value = json.loads(literal)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


def _balanced_content(
    text: str,
    opening: int,
    *,
    open_character: str,
    close_character: str,
) -> str | None:
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(text)):
        character = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == open_character:
            depth += 1
        elif character == close_character:
            depth -= 1
            if depth == 0:
                return text[opening + 1 : index]
    return None


def _arguments(content: str) -> list[str]:
    arguments: list[str] = []
    start = 0
    depths = {"(": 0, "[": 0, "{": 0}
    closings = {")": "(", "]": "[", "}": "{"}
    quote: str | None = None
    escaped = False
    for index, character in enumerate(content):
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
        elif character in depths:
            depths[character] += 1
        elif character in closings:
            depths[closings[character]] -= 1
        elif character == "," and not any(depths.values()):
            arguments.append(content[start:index].strip())
            start = index + 1
    arguments.append(content[start:].strip())
    return arguments


def _method_body(content: str, name: str) -> str | None:
    declaration = re.search(rf"\b{re.escape(name)}\s*\(", content)
    if declaration is None:
        return None
    parameters_open = content.find("(", declaration.start())
    parameters = _balanced_content(
        content,
        parameters_open,
        open_character="(",
        close_character=")",
    )
    if parameters is None:
        return None
    parameters_close = parameters_open + len(parameters) + 1
    body_open = content.find("{", parameters_close + 1)
    if body_open < 0:
        return None
    return _balanced_content(
        content,
        body_open,
        open_character="{",
        close_character="}",
    )


def _constants(ir: SourceIR) -> dict[str, str]:
    constants: dict[str, str] = {}
    declaration = re.compile(
        rf"\b(?:public|private|protected)\s+static\s+final\s+String\s+"
        rf"(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<value>{_STRING_LITERAL})\s*;"
    )
    for source in ir.files:
        class_name = source.path.rsplit("/", 1)[-1].removesuffix(".java")
        for found in declaration.finditer(source.content):
            value = _decode_string(found.group("value"))
            if value is not None:
                constants[f"{class_name}.{found.group('name')}"] = value
    return constants


def _option_facts(ir: SourceIR) -> dict[str, _OptionFacts]:
    facts: dict[str, _OptionFacts] = {}
    key_pattern = re.compile(
        rf"\bpublic\s+static\s+final\s+String\s+KEY\s*=\s*(?P<key>{_STRING_LITERAL})"
    )
    for source in ir.files:
        class_name = source.path.rsplit("/", 1)[-1].removesuffix(".java")
        key_match = key_pattern.search(source.content)
        if key_match is None:
            continue
        key = _decode_string(key_match.group("key"))
        if key is None:
            continue
        variants: dict[str, str] = {}
        titles: list[str] = []
        values: list[str] = []
        decorated_titles = ".entry + '(' +" in source.content and ".description" in source.content
        for found in _OPTION_VARIANT.finditer(source.content):
            arguments = _arguments(found.group("arguments"))
            decoded = [_decode_string(argument) for argument in arguments[:3]]
            if len(decoded) < 2 or decoded[0] is None or decoded[1] is None:
                continue
            title = decoded[0]
            if decorated_titles and len(decoded) >= 3 and decoded[2] is not None:
                title += f"({decoded[2]})"
            titles.append(title)
            values.append(decoded[1])
            variants[found.group("name")] = decoded[1]
        if not variants:
            continue
        default: str | None = None
        constructed = re.search(
            rf"\bDEFAULT(?:_KEY)?\s*=\s*new\s+{re.escape(class_name)}\s*"
            rf"\((?P<arguments>[^;]+?)\)\.entryKey\s*;",
            source.content,
        )
        if constructed is not None:
            arguments = _arguments(constructed.group("arguments"))
            if len(arguments) >= 2:
                default = _decode_string(arguments[1])
        if default is None:
            selected = re.search(
                r"\bDEFAULT(?:_KEY)?\s*=\s*(?P<variant>[A-Z][A-Z0-9_]*)\.entryKey\s*;",
                source.content,
            )
            if selected is not None:
                default = variants.get(selected.group("variant"))
        facts[class_name] = _OptionFacts(
            key=key,
            titles=tuple(titles),
            values=tuple(values),
            default=default,
            variants=variants,
        )
    return facts


def _call_argument(content: str, variable: str, method: str) -> str | None:
    call = re.search(rf"\b{re.escape(variable)}\.{re.escape(method)}\s*\(", content)
    if call is None:
        return None
    opening = content.find("(", call.start())
    return _balanced_content(
        content,
        opening,
        open_character="(",
        close_character=")",
    )


def _resolve(
    expression: str | None,
    *,
    constants: dict[str, str],
    options: dict[str, _OptionFacts],
) -> str | bool | None:
    if expression is None:
        return None
    expression = expression.strip()
    if re.fullmatch(_STRING_LITERAL, expression):
        return _decode_string(expression)
    if expression in {"true", "false"}:
        return expression == "true"
    if expression in constants:
        return constants[expression]
    default = re.fullmatch(
        r"(?P<class>[A-Za-z_]\w*)\.INSTANCE\.getDEFAULT(?:_KEY)?\(\)",
        expression,
    )
    if default is not None and (option := options.get(default.group("class"))) is not None:
        return option.default
    variant = re.fullmatch(
        r"(?P<class>[A-Za-z_]\w*)\.(?P<variant>[A-Z][A-Z0-9_]*)\.getEntryKey\(\)",
        expression,
    )
    if variant is not None and (option := options.get(variant.group("class"))) is not None:
        return option.variants.get(variant.group("variant"))
    return None


def _select_option(
    body: str,
    variable: str,
    options: dict[str, _OptionFacts],
) -> _OptionFacts | None:
    entries = _call_argument(body, variable, "setEntries")
    values = _call_argument(body, variable, "setEntryValues")
    if entries is None or values is None:
        return None
    entries_class = re.fullmatch(
        r"(?P<class>[A-Za-z_]\w*)\.INSTANCE\.getENTRIES\(\)", entries.strip()
    )
    values_class = re.fullmatch(
        r"(?P<class>[A-Za-z_]\w*)\.INSTANCE\.getENTRY_KEYS\(\)", values.strip()
    )
    if (
        entries_class is None
        or values_class is None
        or entries_class.group("class") != values_class.group("class")
    ):
        return None
    return options.get(entries_class.group("class"))


def deterministic_decompiled_settings(ir: SourceIR) -> GeneratedFile | None:
    """Recover a complete public settings resource or return None for AI fallback."""
    if ir.source_format != "decompiled_apk" or Capability.SETTINGS not in ir.capabilities:
        return None
    preferences = next(
        (
            source.content
            for source in ir.files
            if source.path.endswith("PreferencesKt.java")
            and re.search(r"\binitPreferences\s*\(", source.content)
        ),
        None,
    )
    body = _method_body(preferences, "initPreferences") if preferences is not None else None
    if body is None:
        return None
    declarations = {
        found.group("name"): found.group("kind") for found in _PREFERENCE_DECLARATION.finditer(body)
    }
    returned = re.search(r"\breturn\s+(?P<array>[A-Za-z_]\w*)\s*;", body)
    if returned is None:
        return None
    array_name = returned.group("array")
    size_match = re.search(
        rf"\b{re.escape(array_name)}\s*=\s*new\s+Preference\s*\[\s*(?P<size>\d+)\s*\]",
        body,
    )
    assignments = {
        int(found.group("index")): found.group("variable")
        for found in re.finditer(
            rf"\b{re.escape(array_name)}\s*\[\s*(?P<index>\d+)\s*\]\s*=\s*"
            r"(?:\(Preference\)\s*)?(?P<variable>[A-Za-z_]\w*)\s*;",
            body,
        )
    }
    if (
        size_match is None
        or int(size_match.group("size")) != len(assignments)
        or set(assignments) != set(range(len(assignments)))
    ):
        return None
    constants = _constants(ir)
    options = _option_facts(ir)
    items: list[dict[str, object]] = []
    keys: set[str] = set()
    for index in range(len(assignments)):
        variable = assignments[index]
        kind = declarations.get(variable)
        key_expression = _call_argument(body, variable, "setKey")
        key = _resolve(
            key_expression,
            constants=constants,
            options=options,
        )
        if kind is None:
            return None
        if not isinstance(key, str):
            if (
                ir.feature_scope == "public_only"
                and key_expression is not None
                and public_only_setting_reference_exclusion(key_expression) is not None
            ):
                continue
            return None
        if ir.feature_scope == "public_only" and public_only_setting_exclusion(key) is not None:
            continue
        title = _resolve(
            _call_argument(body, variable, "setTitle"),
            constants=constants,
            options=options,
        )
        default = _resolve(
            _call_argument(body, variable, "setDefaultValue"),
            constants=constants,
            options=options,
        )
        if not isinstance(title, str) or key in keys:
            return None
        item: dict[str, object] = {"key": key, "title": title}
        if kind == "ListPreference":
            option = _select_option(body, variable, options)
            if option is None or not isinstance(default, str) or default not in option.values:
                return None
            item.update(
                {
                    "type": "select",
                    "titles": list(option.titles),
                    "values": list(option.values),
                    "default": default,
                }
            )
        elif kind == "EditTextPreference":
            if not isinstance(default, str):
                return None
            item.update({"type": "text", "default": default})
        elif kind == "SwitchPreferenceCompat":
            if not isinstance(default, bool):
                return None
            item.update({"type": "switch", "default": default})
        else:
            return None
        subtitle = _resolve(
            _call_argument(body, variable, "setSummary"),
            constants=constants,
            options=options,
        )
        if isinstance(subtitle, str):
            item["subtitle"] = subtitle
        items.append(item)
        keys.add(key)
    if not items:
        return None
    return GeneratedFile(
        path=GeneratedResources.SETTINGS,
        content=json.dumps(
            [{"type": "group", "title": ir.metadata.name, "items": items}],
            ensure_ascii=False,
        ),
    )


def with_deterministic_decompiled_settings(
    ir: SourceIR,
    manifest: GenerationManifest,
) -> GenerationManifest:
    if GeneratedResources(manifest).has_nonempty_setting_items():
        return manifest
    generated = deterministic_decompiled_settings(ir)
    if generated is None:
        return manifest
    files = [item for item in manifest.files if item.path != generated.path]
    return manifest.model_copy(update={"files": [*files, generated]})
