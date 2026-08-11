from __future__ import annotations

import json
import re
from collections.abc import Mapping
from importlib.resources import files as resource_files
from pathlib import Path, PurePosixPath

from jinja2 import Environment, StrictUndefined

from .dependency_policy import (
    AIDOKU_RS_REPOSITORY,
    AIDOKU_RS_REV,
    PinnedDependency,
    evaluate_dependency_policy,
)
from .errors import SecurityError
from .generated_source_metadata import GeneratedSourceMetadata
from .icons import create_aidoku_icon
from .ingest import ResolvedSource, copy_input_license, find_icon
from .models import (
    Capability,
    GeneratedResources,
    GenerationManifest,
    SourceIR,
    validate_generated_path,
)
from .normalization_trace import NormalizationTrace
from .rust_compatibility import (
    _remove_reserved_smoke_marker,
    validate_generated_content,
)
from .rust_compatibility import (
    normalize_pinned_aidoku_rust as normalize_pinned_aidoku_rust,
)


def render_generated_lib_rs(
    source_struct: str,
    implemented_traits: list[str],
    generated_paths: set[str],
) -> str:
    """Own the crate entry point once AI has separated its source implementation."""
    modules = set()
    for path in generated_paths:
        parts = PurePosixPath(path).parts
        if len(parts) < 2 or parts[0] != "src":
            continue
        module = parts[1] if len(parts) > 2 else PurePosixPath(parts[1]).stem
        if module not in {"lib", "generated_smoke"}:
            modules.add(module)
    declarations = "\n".join(f"mod {module};" for module in sorted(modules))
    trait_arguments = "".join(f",\n    {trait}" for trait in implemented_traits)
    return (
        "#![no_std]\n\n"
        "use aidoku::{Source, prelude::register_source};\n\n"
        f"{declarations}\n\n"
        f"pub use source::{source_struct};\n\n"
        f"register_source!(\n    {source_struct}{trait_arguments}\n);\n"
    )


def normalize_generation_manifest(
    ir: SourceIR,
    manifest: GenerationManifest,
    *,
    trace: NormalizationTrace | None = None,
) -> GenerationManifest:
    """Compatibility facade for the Generation Manifest Projection Module."""
    from .generation_projection import normalize_generation_manifest as project_manifest

    return project_manifest(ir, manifest, trace=trace)


def _environment() -> Environment:
    template_dir = resource_files("convert2aidoku").joinpath("resources", "templates")
    return Environment(
        loader=__import__("jinja2").FileSystemLoader(str(template_dir)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )


def _dependency_context(names: set[str]) -> Mapping[str, PinnedDependency]:
    evaluation = evaluate_dependency_policy(names)
    if evaluation.disallowed:
        noun = "dependency" if len(evaluation.disallowed) == 1 else "dependencies"
        raise SecurityError(
            f"generated source requested disallowed {noun}: " + ", ".join(evaluation.disallowed)
        )
    return evaluation.cargo_dependencies


def _live_api_domain_setting(resources: GeneratedResources) -> dict[str, object] | None:
    defaults = resources.setting_defaults()
    for key, values in resources.setting_values().items():
        if key.rsplit(".", 1)[-1] != "api_domain":
            continue
        default = defaults.get(key)
        candidates = [
            value
            for value in dict.fromkeys([default, *values])
            if value and value.casefold() != "custom"
        ]
        if len(candidates) > 1:
            return {"key": key, "candidates": tuple(candidates)}
    return None


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


def create_scaffold(destination: Path, ir: SourceIR, resolved: ResolvedSource) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    (destination / ".cargo").mkdir()
    (destination / "src").mkdir()
    (destination / "res").mkdir()

    config = _environment().get_template("config.toml.j2").render()
    (destination / ".cargo" / "config.toml").write_text(config, encoding="utf-8")
    _write_cargo(destination, ir, set())
    GeneratedSourceMetadata.from_source_ir(ir).write(destination)
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
    manifest = normalize_generation_manifest(ir, manifest)
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
        validate_generated_content(generated.path, content)
        target = _safe_destination(destination, generated.path)
        target.write_text(content.rstrip() + "\n", encoding="utf-8")
        generated_paths.append(generated.path)

    GeneratedSourceMetadata.load(destination).with_manifest_requirements(manifest).write(
        destination
    )

    lib_path = destination / "src" / "lib.rs"
    lib = lib_path.read_text(encoding="utf-8")
    smoke_module = "#[cfg(test)]\nmod generated_smoke;"
    lib_path.write_text(lib.rstrip() + "\n\n" + smoke_module + "\n", encoding="utf-8")

    live_resources = GeneratedResources(manifest)
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
            query_expression=(
                f"Some({json.dumps(query, ensure_ascii=False)}.into())" if query else "None"
            ),
            static_filter_cases=live_resources.static_filter_cases(),
            api_domain_setting=_live_api_domain_setting(live_resources),
        )
    )
    (destination / "src" / "generated_smoke.rs").write_text(smoke, encoding="utf-8")
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
