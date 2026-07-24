from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .models import Capability

AIDOKU_RS_REPOSITORY = "https://github.com/Aidoku/aidoku-rs.git"
AIDOKU_RS_REV = "1a6bb691dd67c7151fc76fc852fb5a364d325f72"

_PROMPT_MARKER = "{{DEPENDENCY_POLICY}}"


@dataclass(frozen=True)
class PinnedDependency:
    version: str
    default_features: bool = False
    features: tuple[str, ...] = ()


_PINNED_DEPENDENCIES: Mapping[str, PinnedDependency] = MappingProxyType(
    {
        "serde": PinnedDependency("1.0.219", features=("derive",)),
        "serde_json": PinnedDependency("1.0.140", features=("alloc",)),
        "regex": PinnedDependency("1.11.1", features=("unicode",)),
        "base64": PinnedDependency("0.22.1", features=("alloc",)),
        "aes": PinnedDependency("0.8.4"),
        "des": PinnedDependency("0.8.1"),
        "cbc": PinnedDependency("0.1.2", features=("block-padding",)),
        "hex": PinnedDependency("0.4.3", features=("alloc",)),
    }
)

_REQUIRED_DEPENDENCIES: Mapping[Capability, frozenset[str]] = MappingProxyType(
    {
        Capability.JSON_API: frozenset({"serde"}),
        Capability.ENCRYPTED_JSON: frozenset({"aes", "cbc", "serde", "serde_json"}),
        Capability.TRIPLE_DES_CBC: frozenset({"des", "cbc", "base64"}),
    }
)
_ONE_OF_DEPENDENCIES: Mapping[Capability, frozenset[str]] = MappingProxyType(
    {
        Capability.ENCRYPTED_JSON: frozenset({"base64", "hex"}),
    }
)


@dataclass(frozen=True)
class DependencyEvaluation:
    allowed: tuple[str, ...]
    requested: frozenset[str]
    disallowed: tuple[str, ...]
    cargo_dependencies: Mapping[str, PinnedDependency]
    diagnostics: tuple[str, ...]


def evaluate_dependency_policy(
    requested: Iterable[str],
    *,
    capabilities: Iterable[Capability] = (),
) -> DependencyEvaluation:
    requested_names = frozenset(requested)
    allowed = tuple(sorted(_PINNED_DEPENDENCIES))
    disallowed = tuple(sorted(requested_names - _PINNED_DEPENDENCIES.keys()))
    cargo_dependencies = MappingProxyType(
        {
            name: _PINNED_DEPENDENCIES[name]
            for name in sorted(requested_names & _PINNED_DEPENDENCIES.keys())
        }
    )
    capability_set = frozenset(capabilities)
    diagnostics: list[str] = []

    if Capability.JSON_API in capability_set and "serde" not in requested_names:
        diagnostics.append("JSON API source generated no pinned serde dependency")
    if Capability.ENCRYPTED_JSON in capability_set:
        missing = sorted(_REQUIRED_DEPENDENCIES[Capability.ENCRYPTED_JSON] - requested_names)
        if missing:
            diagnostics.append(
                "encrypted JSON source omitted required pinned dependencies: " + ", ".join(missing)
            )
        if not requested_names.intersection(_ONE_OF_DEPENDENCIES[Capability.ENCRYPTED_JSON]):
            diagnostics.append("encrypted JSON source requested neither hex nor base64 decoding")
    if Capability.TRIPLE_DES_CBC in capability_set:
        missing = sorted(_REQUIRED_DEPENDENCIES[Capability.TRIPLE_DES_CBC] - requested_names)
        if missing:
            diagnostics.append(
                "3DES-CBC request signing omitted required pinned dependencies: "
                + ", ".join(missing)
            )

    return DependencyEvaluation(
        allowed=allowed,
        requested=requested_names,
        disallowed=disallowed,
        cargo_dependencies=cargo_dependencies,
        diagnostics=tuple(diagnostics),
    )


def render_dependency_policy(contract: str) -> str:
    if contract.count(_PROMPT_MARKER) != 1:
        raise ValueError("Aidoku contract must contain exactly one dependency policy marker")
    evaluation = evaluate_dependency_policy(())
    allowlist = ", ".join(f"`{name}`" for name in evaluation.allowed)
    requirements = ["Dependency requests may only use these exact names: " + allowlist + "."]
    for capability, required in _REQUIRED_DEPENDENCIES.items():
        requirement = "request " + ", ".join(f"`{name}`" for name in sorted(required))
        alternatives = _ONE_OF_DEPENDENCIES.get(capability)
        if alternatives:
            requirement += " and at least one of " + ", ".join(
                f"`{name}`" for name in sorted(alternatives)
            )
        requirements.append(f"- `{capability.value}`: {requirement}.")
    return contract.replace(_PROMPT_MARKER, "\n".join(requirements))
