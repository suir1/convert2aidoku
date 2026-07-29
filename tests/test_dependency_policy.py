from __future__ import annotations

import pytest

from convert2aidoku.ai import _contract_text
from convert2aidoku.dependency_policy import (
    AIDOKU_RS_REPOSITORY,
    AIDOKU_RS_REV,
    evaluate_dependency_policy,
    render_dependency_policy,
)
from convert2aidoku.models import Capability


def test_policy_owns_complete_sorted_allowlist_and_cargo_projection() -> None:
    evaluation = evaluate_dependency_policy(["serde_json", "des", "serde_json", "reqwest"])

    assert evaluation.allowed == (
        "aes",
        "base64",
        "cbc",
        "des",
        "hex",
        "md-5",
        "rand_chacha",
        "rand_core",
        "regex",
        "rsa",
        "serde",
        "serde_json",
    )
    assert evaluation.requested == frozenset({"des", "reqwest", "serde_json"})
    assert evaluation.disallowed == ("reqwest",)
    assert tuple(evaluation.cargo_dependencies) == ("des", "serde_json")
    assert evaluation.cargo_dependencies["des"].version == "0.8.1"
    assert not evaluation.cargo_dependencies["des"].default_features
    assert evaluation.cargo_dependencies["serde_json"].features == ("alloc",)

    all_pins = evaluate_dependency_policy(evaluation.allowed).cargo_dependencies
    assert {
        name: (spec.version, spec.default_features, spec.features)
        for name, spec in all_pins.items()
    } == {
        "aes": ("0.8.4", False, ()),
        "base64": ("0.22.1", False, ("alloc",)),
        "cbc": ("0.1.2", False, ("block-padding",)),
        "des": ("0.8.1", False, ()),
        "hex": ("0.4.3", False, ("alloc",)),
        "md-5": ("0.10.6", False, ()),
        "rand_chacha": ("0.3.1", False, ()),
        "rand_core": ("0.6.4", False, ()),
        "regex": ("1.11.1", False, ("unicode",)),
        "rsa": ("0.9.8", False, ()),
        "serde": ("1.0.219", False, ("derive",)),
        "serde_json": ("1.0.140", False, ("alloc",)),
    }


def test_policy_reports_capability_requirements_from_the_same_rules() -> None:
    incomplete = evaluate_dependency_policy(
        [],
        capabilities=[
            Capability.JSON_API,
            Capability.ENCRYPTED_JSON,
            Capability.TRIPLE_DES_CBC,
            Capability.RSA_PKCS1_V15,
            Capability.MD5_REQUEST_SIGNING,
        ],
    )
    encrypted = evaluate_dependency_policy(
        ["aes", "cbc", "hex", "serde", "serde_json"],
        capabilities=[Capability.JSON_API, Capability.ENCRYPTED_JSON],
    )
    triple_des = evaluate_dependency_policy(
        ["base64", "cbc", "des"],
        capabilities=[Capability.TRIPLE_DES_CBC],
    )

    assert incomplete.diagnostics == (
        "JSON API source generated no pinned serde dependency",
        "encrypted JSON source omitted required pinned dependencies: aes, cbc, serde, serde_json",
        "encrypted JSON source requested neither hex nor base64 decoding",
        "3DES-CBC request signing omitted required pinned dependencies: base64, cbc, des",
        "RSA PKCS#1 v1.5 device bootstrap omitted required pinned dependencies: "
        "base64, rand_chacha, rand_core, rsa, serde_json",
        "MD5 request signing omitted required pinned dependencies: md-5",
    )
    assert not encrypted.diagnostics
    assert not triple_des.diagnostics


def test_ai_contract_is_rendered_from_policy_and_includes_des() -> None:
    contract = _contract_text()

    assert "{{DEPENDENCY_POLICY}}" not in contract
    assert "`des`" in contract
    assert "`triple_des_cbc`: request `base64`, `cbc`, `des`" in contract
    assert "`encrypted_json`: request `aes`, `cbc`, `serde`, `serde_json`" in contract
    assert (
        "`rsa_pkcs1_v15`: request `base64`, `rand_chacha`, `rand_core`, `rsa`, `serde_json`"
        in contract
    )
    assert "`md5_request_signing`: request `md-5`" in contract


@pytest.mark.parametrize(
    "contract",
    ["no marker", "{{DEPENDENCY_POLICY}} and {{DEPENDENCY_POLICY}}"],
)
def test_contract_rendering_requires_one_policy_marker(contract: str) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        render_dependency_policy(contract)


def test_policy_owns_pinned_aidoku_revision() -> None:
    assert AIDOKU_RS_REPOSITORY == "https://github.com/Aidoku/aidoku-rs.git"
    assert AIDOKU_RS_REV == "1a6bb691dd67c7151fc76fc852fb5a364d325f72"
