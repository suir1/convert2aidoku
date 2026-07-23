import os
from pathlib import Path

import pytest

from convert2aidoku.models import DependencyRequest, GeneratedFile, GenerationManifest
from convert2aidoku.scaffold import apply_generation_manifest
from convert2aidoku.toolchain import find_tool
from convert2aidoku.validator import validate_project
from tests.scenarios import scaffold_project

from .test_converter import RUST_SOURCE

ENCRYPTED_RUST_SOURCE = RUST_SOURCE.replace(
    "use aidoku::{",
    "use aes::Aes128;\n"
    "use cbc::cipher::{block_padding::Pkcs7, BlockDecryptMut as _, KeyIvInit as _};\n"
    "use aidoku::{",
).replace(
    "fn new() -> Self { Self }",
    "fn new() -> Self {\n"
    "        let mut data = [0_u8; 16];\n"
    "        let key = [0_u8; 16];\n"
    "        let iv = [0_u8; 16];\n"
    "        let _ = cbc::Decryptor::<Aes128>::new(&key.into(), &iv.into())\n"
    "            .decrypt_padded_mut::<Pkcs7>(&mut data);\n"
    '        let _ = hex::decode("00");\n'
    "        Self\n"
    "    }",
)

TRIPLE_DES_RUST_SOURCE = RUST_SOURCE.replace(
    "use aidoku::{",
    "use base64::{Engine as _, engine::general_purpose::STANDARD};\n"
    "use cbc::cipher::{block_padding::Pkcs7, BlockEncryptMut as _, KeyIvInit as _};\n"
    "use des::TdesEde3;\n"
    "use aidoku::{",
).replace(
    "fn new() -> Self { Self }",
    "fn new() -> Self {\n"
    "        let mut data = [0_u8; 16];\n"
    "        let key = [0_u8; 24];\n"
    "        let iv = [0_u8; 8];\n"
    "        let encrypted = cbc::Encryptor::<TdesEde3>::new_from_slices(&key, &iv)\n"
    "            .unwrap()\n"
    "            .encrypt_padded_mut::<Pkcs7>(&mut data, 1)\n"
    "            .unwrap();\n"
    "        let _ = STANDARD.encode(encrypted);\n"
    "        Self\n"
    "    }",
)

pytestmark = pytest.mark.skipif(
    os.getenv("C2A_RUN_RUST_INTEGRATION") != "1",
    reason="set C2A_RUN_RUST_INTEGRATION=1 to run the pinned Aidoku build",
)


def test_generated_fixture_builds_packages_and_verifies(tmp_path: Path) -> None:
    assert find_tool("cargo")
    assert find_tool("aidoku")
    project, ir = scaffold_project(tmp_path)
    apply_generation_manifest(
        project,
        ir,
        GenerationManifest(
            source_struct="Simple",
            files=[GeneratedFile(path="src/lib.rs", content=RUST_SOURCE)],
        ),
        query=None,
    )

    validation = validate_project(project, live=False)
    assert validation.build_ok, validation.diagnostics
    assert validation.package_ok, validation.diagnostics
    assert (project / "package.aix").is_file()


def test_pinned_encrypted_json_dependencies_compile_for_wasm(tmp_path: Path) -> None:
    assert find_tool("cargo")
    project, ir = scaffold_project(tmp_path, name="encrypted-project")
    apply_generation_manifest(
        project,
        ir,
        GenerationManifest(
            source_struct="Simple",
            files=[GeneratedFile(path="src/lib.rs", content=ENCRYPTED_RUST_SOURCE)],
            dependencies=[
                DependencyRequest(name="aes"),
                DependencyRequest(name="cbc"),
                DependencyRequest(name="hex"),
            ],
        ),
        query=None,
    )

    validation = validate_project(project, live=False)
    assert validation.build_ok, validation.diagnostics
    assert validation.package_ok, validation.diagnostics


def test_pinned_triple_des_dependencies_compile_for_wasm(tmp_path: Path) -> None:
    assert find_tool("cargo")
    project, ir = scaffold_project(tmp_path, name="triple-des-project")
    apply_generation_manifest(
        project,
        ir,
        GenerationManifest(
            source_struct="Simple",
            files=[GeneratedFile(path="src/lib.rs", content=TRIPLE_DES_RUST_SOURCE)],
            dependencies=[
                DependencyRequest(name="des"),
                DependencyRequest(name="cbc"),
                DependencyRequest(name="base64"),
            ],
        ),
        query=None,
    )

    validation = validate_project(project, live=False)
    assert validation.build_ok, validation.diagnostics
    assert validation.package_ok, validation.diagnostics
