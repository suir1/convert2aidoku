from __future__ import annotations

import json
import re
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined
from tree_sitter_language_pack import get_parser

from .constants import (
    AIDOKU_RS_REPOSITORY,
    AIDOKU_RS_REV,
    DEPENDENCY_SPECS,
    MAX_GENERATED_FILE_CHARS,
)
from .errors import SecurityError
from .icons import create_aidoku_icon
from .ingest import ResolvedSource, copy_input_license, find_icon
from .models import Capability, GenerationManifest, SourceIR, validate_generated_path

_FORBIDDEN_GENERATED_TOKENS = (
    "unsafe",
    'extern "C"',
    "Command::",
    "generated_smoke",
    "#[cfg(test)]",
    "#[test]",
)

_SMOKE_MARKER = "#[cfg(test)]\nmod generated_smoke;"
_FORBIDDEN_GENERATED_MACRO = re.compile(
    r"\b(?:env|option_env|include|include_str|include_bytes)\s*!\s*\(",
)
_FORBIDDEN_GENERATED_RUST = re.compile(r"\bextern\s+crate\s+std\b")
_FORBIDDEN_RUST_MACROS = {
    "env",
    "include",
    "include_bytes",
    "include_str",
    "option_env",
}
_FORBIDDEN_RUST_ATTRIBUTES = {"cfg", "cfg_attr", "path", "test"}
_RUST_IDENTIFIER_NODES = {"identifier", "raw_identifier"}


def _rust_identifier(node: Any) -> str | None:
    if node is None or node.type not in _RUST_IDENTIFIER_NODES:
        return None
    return node.text.decode("utf-8", errors="replace").removeprefix("r#")


def _last_rust_identifier(node: Any) -> str | None:
    if node is None:
        return None
    found: str | None = None
    stack = [node]
    while stack:
        current = stack.pop()
        identifier = _rust_identifier(current)
        if identifier is not None:
            found = identifier
        stack.extend(reversed(current.children))
    return found


def _first_rust_identifier(node: Any) -> str | None:
    if node is None:
        return None
    stack = [node]
    while stack:
        current = stack.pop()
        identifier = _rust_identifier(current)
        if identifier is not None:
            return identifier
        stack.extend(reversed(current.children))
    return None


def _compact_rust_node(node: Any) -> str:
    text = node.text.decode("utf-8", errors="replace")
    text = re.sub(r"/\*[\s\S]*?\*/|//[^\r\n]*", "", text)
    return "".join(text.split())


def _validate_generated_rust_ast(path: str, content: str) -> None:
    """Reject compile-time I/O and ways to bypass the tool-owned smoke tests."""
    tree = get_parser("rust").parse(content.encode("utf-8"))
    stack = [tree.root_node]
    while stack:
        node = stack.pop()

        identifier = _rust_identifier(node)
        if identifier == "std" and not (
            node.parent is not None and _compact_rust_node(node.parent) == "aidoku::imports::std"
        ):
            raise SecurityError(f"generated Rust uses std, which is forbidden: {path}")
        if identifier == "generated_smoke":
            raise SecurityError(
                f"generated Rust references forbidden reserved module generated_smoke: {path}"
            )

        if node.type == "macro_invocation":
            name = _last_rust_identifier(node.child_by_field_name("macro"))
            if name in _FORBIDDEN_RUST_MACROS:
                raise SecurityError(
                    f"generated Rust uses forbidden environment/file macro {name}: {path}"
                )

        if node.type in {"attribute_item", "inner_attribute_item"}:
            name = _first_rust_identifier(node.named_child(0))
            if name in _FORBIDDEN_RUST_ATTRIBUTES:
                raise SecurityError(f"generated Rust uses forbidden attribute {name}: {path}")

        # This catches unsafe blocks as well as unsafe fn/trait/impl keywords,
        # including variants separated from adjacent tokens by comments.
        if node.type in {"unsafe", "unsafe_block"}:
            raise SecurityError(f"generated Rust uses forbidden unsafe code: {path}")

        stack.extend(reversed(node.children))


def validate_generated_content(path: str, content: str) -> None:
    if len(content) > MAX_GENERATED_FILE_CHARS:
        raise SecurityError(f"generated file is too large: {path}")
    if path.endswith(".rs"):
        _validate_generated_rust_ast(path, content)
        if _FORBIDDEN_GENERATED_MACRO.search(content):
            raise SecurityError(f"generated Rust uses a forbidden environment/file macro: {path}")
        if _FORBIDDEN_GENERATED_RUST.search(content):
            raise SecurityError(f"generated Rust uses std, which is not allowed: {path}")
        for token in _FORBIDDEN_GENERATED_TOKENS:
            if token in content:
                raise SecurityError(f"generated Rust uses forbidden construct {token}: {path}")


def _remove_reserved_smoke_marker(content: str) -> str:
    # A repair model may echo the tool-owned marker from its current-files
    # context. Strip exactly that marker; any other test module is rejected.
    return content.replace(f"\n{_SMOKE_MARKER}\n", "\n").replace(f"\n{_SMOKE_MARKER}", "\n")


def _alloc_macro_is_imported(content: str, name: str) -> bool:
    return any(
        re.search(rf"\b{re.escape(name)}\b", statement.group(0))
        for statement in re.finditer(r"\buse\s+aidoku::alloc\b[^;]*;", content)
    )


def _inject_no_std_macro_imports(content: str) -> str:
    missing = [
        name
        for name in ("format", "vec")
        if re.search(rf"(?<![:\w]){name}!\s*[\(\[\{{]", content)
        and not _alloc_macro_is_imported(content, name)
    ]
    if not missing:
        return content
    imports = "\n".join(f"use aidoku::alloc::{name};" for name in missing)
    crate_attributes = re.match(r"(?:\s*#!\[[^\n]*\]\s*\n)+", content)
    if crate_attributes is None:
        return imports + "\n\n" + content.lstrip()
    boundary = crate_attributes.end()
    return content[:boundary] + "\n" + imports + "\n" + content[boundary:].lstrip("\n")


def normalize_pinned_aidoku_rust(content: str) -> str:
    """Apply small type-safe compatibility rewrites for the pinned Aidoku/Rust APIs."""
    content = content.replace("aidoku::std::filters::SelectFilter", "aidoku::SelectFilter")
    content = content.replace("RequestError::new(", "aidoku::AidokuError::message(")
    if content.count("RequestError") == 1:
        content = re.sub(
            r"use\s+aidoku::imports::net::\{\s*Request\s*,\s*RequestError\s*\};",
            "use aidoku::imports::net::Request;",
            content,
        )
    if re.search(r"let\s+host\s*=\s*absolute\b", content):
        content = content.replace("Request::get(absolute)?", "Request::get(absolute.clone())?")
    content = re.sub(
        r'"(?:\\.|[^"\\])*"',
        lambda match: match.group(0).replace("}//chapters", "}/chapters"),
        content,
    )
    content = _inject_no_std_macro_imports(content)
    content = re.sub(
        r"(\bparse_(?:local_)?date\s*\([^;]{0,800}?\))\s*\.ok\(\)",
        r"\1",
        content,
    )
    content = re.sub(
        r"\b(?P<items>[A-Za-z_]\w*)\.sort_by\(\|(?P<left>[A-Za-z_]\w*),\s*"
        r"(?P<right>[A-Za-z_]\w*)\|\s*(?P=right)\.index\.cmp\(&(?P=left)\.index\)\);",
        lambda match: f"{match.group('items')}.sort_by_key(|item| core::cmp::Reverse(item.index));",
        content,
    )
    return content


def _environment() -> Environment:
    template_dir = resource_files("convert2aidoku").joinpath("resources", "templates")
    return Environment(
        loader=__import__("jinja2").FileSystemLoader(str(template_dir)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )


def _dependency_context(names: set[str]) -> dict[str, dict[str, Any]]:
    dependencies: dict[str, dict[str, Any]] = {}
    for name in sorted(names):
        if name not in DEPENDENCY_SPECS:
            raise SecurityError(f"generated source requested disallowed dependency: {name}")
        spec = DEPENDENCY_SPECS[name]
        dependencies[name] = {
            "version": str(spec["version"]),
            "features": list(spec.get("features", [])),
        }
    return dependencies


def _write_cargo(
    destination: Path,
    ir: SourceIR,
    dependency_names: set[str],
    *,
    invalidate_lock: bool = False,
) -> None:
    template = _environment().get_template("Cargo.toml.j2")
    package = re.sub(r"[^a-zA-Z0-9_-]+", "_", ir.metadata.package_name)
    cargo_path = destination / "Cargo.toml"
    rendered = template.render(
        package_name=package,
        aidoku_repository=AIDOKU_RS_REPOSITORY,
        aidoku_rev=AIDOKU_RS_REV,
        dependencies=_dependency_context(dependency_names),
    )
    previous = cargo_path.read_text(encoding="utf-8") if cargo_path.is_file() else None
    if invalidate_lock or (previous is not None and previous != rendered):
        (destination / "Cargo.lock").unlink(missing_ok=True)
    cargo_path.write_text(rendered, encoding="utf-8")


def _source_json(ir: SourceIR) -> dict[str, Any]:
    return {
        "info": {
            "id": ir.metadata.source_id,
            "name": ir.metadata.name,
            "version": ir.metadata.version,
            "url": ir.metadata.base_url,
            "contentRating": ir.metadata.content_rating.aidoku_value,
            "languages": [ir.metadata.language],
        }
    }


def _static_filter_cases(manifest: GenerationManifest) -> list[dict[str, Any]]:
    resource = next(
        (item for item in manifest.files if item.path == "res/filters.json"),
        None,
    )
    if resource is None:
        return []
    try:
        filters = json.loads(resource.content)
    except json.JSONDecodeError:
        return []
    if not isinstance(filters, list):
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
            if isinstance(default, str) and default in ids:
                value = next(
                    (candidate for candidate in ids if candidate and candidate != default),
                    default,
                )
            else:
                value = next(
                    (candidate for candidate in ids if candidate),
                    ids[0] if ids else None,
                )
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


def _update_min_app_version(destination: Path, manifest: GenerationManifest) -> None:
    """Keep tool-owned metadata compatible with host imports used by generated Rust."""
    rust = "\n".join(item.content for item in manifest.files if item.path.endswith(".rs"))
    source_path = destination / "res" / "source.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    info = source["info"]
    minimum_version: str | None = None
    if re.search(r"\bparse_(?:local_)?date(?:_with_options)?\b", rust):
        minimum_version = "0.7.1"
    if re.search(r"\b(?:timeout|set_timeout)\s*\(", rust):
        minimum_version = "0.8.3"
    if minimum_version is None:
        info.pop("minAppVersion", None)
    else:
        info["minAppVersion"] = minimum_version
    source_path.write_text(
        json.dumps(source, ensure_ascii=False, indent="\t") + "\n",
        encoding="utf-8",
    )


def create_scaffold(destination: Path, ir: SourceIR, resolved: ResolvedSource) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    (destination / ".cargo").mkdir()
    (destination / "src").mkdir()
    (destination / "res").mkdir()

    config = _environment().get_template("config.toml.j2").render()
    (destination / ".cargo" / "config.toml").write_text(config, encoding="utf-8")
    _write_cargo(destination, ir, set())
    (destination / "res" / "source.json").write_text(
        json.dumps(_source_json(ir), ensure_ascii=False, indent="\t") + "\n",
        encoding="utf-8",
    )
    create_aidoku_icon(
        find_icon(resolved.module_path),
        destination / "res" / "icon.png",
        initials=ir.metadata.name,
    )
    copied_license = copy_input_license(resolved, destination / "LICENSE.input")
    provenance = [
        "# Generated source provenance",
        "",
        f"- Input: `{ir.input_ref}`",
        f"- Input commit: `{ir.commit or 'unknown'}`",
        f"- Input license copied: `{copied_license or 'not found'}`",
        "- This output may be a derivative work. Verify redistribution rights before publishing.",
        "",
    ]
    (destination / "PROVENANCE.md").write_text("\n".join(provenance), encoding="utf-8")


def _safe_destination(root: Path, relative: str) -> Path:
    safe = validate_generated_path(relative)
    destination = root.joinpath(*safe.split("/"))
    root_resolved = root.resolve()
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    current = root
    for part in Path(safe).parent.parts:
        current = current / part
        if current.is_symlink():
            raise SecurityError(f"refusing to write through a symbolic link: {relative}")
    resolved_parent = parent.resolve()
    if root_resolved != resolved_parent and root_resolved not in resolved_parent.parents:
        raise SecurityError(f"generated path escapes staging directory: {relative}")
    return destination


def apply_generation_manifest(
    destination: Path,
    ir: SourceIR,
    manifest: GenerationManifest,
    *,
    query: str | None,
) -> list[str]:
    dependency_names = {item.name for item in manifest.dependencies}
    _write_cargo(destination, ir, dependency_names)

    manifest_paths = {item.path for item in manifest.files}
    for current in (destination / "src").rglob("*.rs"):
        relative = current.relative_to(destination).as_posix()
        if relative != "src/generated_smoke.rs" and relative not in manifest_paths:
            current.unlink()
    for optional in ("res/filters.json", "res/settings.json"):
        if optional not in manifest_paths:
            (destination / optional).unlink(missing_ok=True)

    generated_paths: list[str] = []
    for generated in manifest.files:
        content = generated.content
        if generated.path == "src/lib.rs":
            content = _remove_reserved_smoke_marker(content)
            if not re.match(r"\s*#!\[no_std\]", content):
                content = "#![no_std]\n\n" + content.lstrip()
            content = normalize_pinned_aidoku_rust(content)
        validate_generated_content(generated.path, content)
        target = _safe_destination(destination, generated.path)
        target.write_text(content.rstrip() + "\n", encoding="utf-8")
        generated_paths.append(generated.path)

    _update_min_app_version(destination, manifest)

    lib_path = destination / "src" / "lib.rs"
    lib = lib_path.read_text(encoding="utf-8")
    smoke_module = "#[cfg(test)]\nmod generated_smoke;"
    lib_path.write_text(lib.rstrip() + "\n\n" + smoke_module + "\n", encoding="utf-8")

    smoke = (
        _environment()
        .get_template("smoke.rs.j2")
        .render(
            source_struct=manifest.source_struct,
            image_request_provider="ImageRequestProvider" in manifest.implemented_traits,
            listing_provider="ListingProvider" in manifest.implemented_traits,
            dynamic_filters="DynamicFilters" in manifest.implemented_traits,
            deep_link_handler="DeepLinkHandler" in manifest.implemented_traits,
            popular_listing=Capability.POPULAR in ir.capabilities,
            latest_listing=Capability.LATEST in ir.capabilities,
            query_expression=(f"Some({json.dumps(query)}.into())" if query else "None"),
            static_filter_cases=_static_filter_cases(manifest),
        )
    )
    (destination / "src" / "generated_smoke.rs").write_text(smoke, encoding="utf-8")

    for optional_json in ("filters.json", "settings.json"):
        path = destination / "res" / optional_json
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise SecurityError(f"generated {optional_json} is not valid JSON: {exc}") from exc
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent="\t") + "\n",
                encoding="utf-8",
            )
    return sorted(generated_paths + ["src/generated_smoke.rs"])


def read_generated_files(destination: Path) -> list[dict[str, str]]:
    result = []
    for path in sorted((destination / "src").rglob("*.rs")):
        if path.name == "generated_smoke.rs":
            continue
        result.append(
            {
                "path": path.relative_to(destination).as_posix(),
                "content": _remove_reserved_smoke_marker(path.read_text(encoding="utf-8")),
            }
        )
    for name in ("filters.json", "settings.json"):
        path = destination / "res" / name
        if path.exists():
            result.append(
                {
                    "path": path.relative_to(destination).as_posix(),
                    "content": path.read_text(encoding="utf-8"),
                }
            )
    return result
