from __future__ import annotations

import json
import re
from typing import Any

from .models import Capability, GeneratedFile, GeneratedResources, GenerationManifest, SourceIR

_STRING = r'"(?:\\.|[^"\\])*"'
_PREFERENCE = re.compile(
    r"\b(?P<kind>ListPreference|MultiSelectListPreference|SwitchPreferenceCompat|EditTextPreference)"
    r"\s*\([^)]*\)\s*\.apply\s*\{"
)


def _string(literal: str) -> str | None:
    try:
        value = json.loads(literal)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


def _constants(ir: SourceIR) -> dict[str, str]:
    constants: dict[str, str] = {}
    pattern = re.compile(rf"\b(?:const\s+)?val\s+(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<value>{_STRING})")
    for source in ir.files:
        for match in pattern.finditer(source.content):
            if (value := _string(match.group("value"))) is not None:
                constants[match.group("name")] = value
    return constants


def _set_constants(ir: SourceIR) -> dict[str, list[str]]:
    constants: dict[str, list[str]] = {}
    pattern = re.compile(
        r"\b(?:const\s+)?val\s+(?P<name>[A-Za-z_]\w*)\s*=\s*"
        r"setOf\((?P<values>[\s\S]*?)\)"
    )
    for source in ir.files:
        for match in pattern.finditer(source.content):
            values = [
                value
                for literal in re.findall(_STRING, match.group("values"))
                if (value := _string(literal)) is not None
            ]
            if values:
                constants[match.group("name")] = values
    return constants


def _block(content: str, opening: int) -> str | None:
    depth = 0
    quoted = False
    escaped = False
    for index in range(opening, len(content)):
        character = content[index]
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return content[opening + 1 : index]
    return None


def _value(block: str, field: str, constants: dict[str, str]) -> str | None:
    match = re.search(
        rf"\b{re.escape(field)}\s*=\s*(?P<value>{_STRING}|[A-Za-z_]\w*)",
        block,
    )
    if match is None:
        return None
    expression = match.group("value")
    return _string(expression) if expression.startswith('"') else constants.get(expression)


def _array(block: str, field: str) -> list[str]:
    match = re.search(
        rf"\b{re.escape(field)}\s*=\s*arrayOf\((?P<values>[\s\S]*?)\)",
        block,
    )
    if match is None:
        return []
    values = []
    for literal in re.findall(_STRING, match.group("values")):
        if (value := _string(literal)) is not None:
            values.append(value)
    return values


def _default(
    block: str,
    constants: dict[str, str],
    set_constants: dict[str, list[str]],
) -> str | bool | list[str] | None:
    match = re.search(
        rf"\bsetDefaultValue\(\s*(?P<value>{_STRING}|true|false|[A-Za-z_]\w*)\s*\)",
        block,
    )
    if match is None:
        return None
    expression = match.group("value")
    if expression in {"true", "false"}:
        return expression == "true"
    if expression.startswith('"'):
        return _string(expression)
    return constants.get(expression) or set_constants.get(expression)


def _item(
    kind: str,
    block: str,
    constants: dict[str, str],
    set_constants: dict[str, list[str]],
) -> dict[str, Any] | None:
    key = _value(block, "key", constants)
    if not key:
        return None
    item: dict[str, Any] = {
        "type": {
            "ListPreference": "select",
            "MultiSelectListPreference": "multi-select",
            "SwitchPreferenceCompat": "switch",
            "EditTextPreference": "text",
        }[kind],
        "key": key,
        "title": _value(block, "title", constants) or key,
    }
    if summary := _value(block, "summary", constants):
        item["summary"] = summary
    default = _default(block, constants, set_constants)
    if kind in {"ListPreference", "MultiSelectListPreference"}:
        titles = _array(block, "entries")
        values = _array(block, "entryValues")
        if not titles or len(titles) != len(values):
            return None
        if kind == "MultiSelectListPreference":
            selected = (
                [value for value in default if value in values] if isinstance(default, list) else []
            )
        else:
            selected = default if isinstance(default, str) and default in values else values[0]
        item.update(
            {
                "titles": titles,
                "values": values,
                "default": selected,
            }
        )
    elif kind == "SwitchPreferenceCompat":
        item["default"] = default if isinstance(default, bool) else False
    else:
        item["default"] = default if isinstance(default, str) else ""
    return item


def _kotlin_items(ir: SourceIR) -> list[dict[str, Any]]:
    constants = _constants(ir)
    set_constants = _set_constants(ir)
    items = []
    for source in ir.files:
        for match in _PREFERENCE.finditer(source.content):
            block = _block(source.content, match.end() - 1)
            if block is None:
                continue
            if item := _item(match.group("kind"), block, constants, set_constants):
                items.append(item)
    return items


def with_kotlin_settings(ir: SourceIR, manifest: GenerationManifest) -> GenerationManifest:
    if ir.source_format != "kotlin_module" or Capability.SETTINGS not in ir.capabilities:
        return manifest
    resources = GeneratedResources(manifest)
    if resources.has_nonempty_setting_items():
        return manifest
    items = _kotlin_items(ir)
    if not items:
        return manifest
    generated = GeneratedFile(
        path=GeneratedResources.SETTINGS,
        content=json.dumps(
            [{"type": "group", "title": ir.metadata.name, "items": items}],
            ensure_ascii=False,
        ),
    )
    files = [item for item in manifest.files if item.path != GeneratedResources.SETTINGS]
    return manifest.model_copy(update={"files": [*files, generated]})
