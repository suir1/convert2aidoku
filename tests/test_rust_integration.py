import os
from pathlib import Path

import pytest

from convert2aidoku.analyzer import analyze_source
from convert2aidoku.ingest import resolve_source
from convert2aidoku.models import DependencyRequest, GeneratedFile, GenerationManifest
from convert2aidoku.scaffold import apply_generation_manifest, create_scaffold
from convert2aidoku.toolchain import find_tool
from convert2aidoku.validator import validate_project

from .test_converter import FIXTURE, RUST_SOURCE

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

pytestmark = pytest.mark.skipif(
    os.getenv("C2A_RUN_RUST_INTEGRATION") != "1",
    reason="set C2A_RUN_RUST_INTEGRATION=1 to run the pinned Aidoku build",
)


def test_generated_fixture_builds_packages_and_verifies(tmp_path: Path) -> None:
    assert find_tool("cargo")
    assert find_tool("aidoku")
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved)
        project = tmp_path / "project"
        create_scaffold(project, ir, resolved)
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
    with resolve_source(str(FIXTURE)) as resolved:
        ir = analyze_source(resolved)
        project = tmp_path / "encrypted-project"
        create_scaffold(project, ir, resolved)
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
