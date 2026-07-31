import os
from pathlib import Path

import pytest

from convert2aidoku.listing_renderer import render_search_listing
from convert2aidoku.models import (
    Capability,
    DependencyRequest,
    GeneratedFile,
    GenerationManifest,
    RequestHeaderProfile,
)
from convert2aidoku.scaffold import apply_generation_manifest
from convert2aidoku.toolchain import find_tool
from convert2aidoku.validator import validate_project
from tests.scenarios import minimal_source_ir, scaffold_project
from tests.test_implementation_ir import _copymanga_listing_files, _serializable_listing_files

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

RSA_MD5_RUST_SOURCE = RUST_SOURCE.replace(
    "use aidoku::{",
    "use base64::{Engine as _, engine::general_purpose::STANDARD};\n"
    "use md5::{Digest as _, Md5};\n"
    "use rand_chacha::ChaCha20Rng;\n"
    "use rand_core::SeedableRng as _;\n"
    "use rsa::{Pkcs1v15Encrypt, RsaPublicKey, pkcs8::DecodePublicKey as _};\n"
    "use aidoku::{",
).replace(
    "fn new() -> Self { Self }",
    "fn new() -> Self {\n"
    "        let public_key = concat!(\n"
    '            "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAmFCg289dTws27v8G",\n'
    '            "tqIffkP4zgFR+MYIuUIeVO5AGiBV0rfpRh5gg7i8RrT12E9j6XwKoe3x",\n'
    '            "Jz1khDnPc65P5f7CJcNJ9A8bj7Al5K4jYGxz+4Q+n0YzSllXPit/Vz/i",\n'
    '            "W5jFdlP6CTIgUVwvIoGEL2sS4cqqqSpCDKHSeiXh9CtMsktc6YyrSN+8",\n'
    '            "mQbBvoSSew18r/vC07iQiaYkClcs7jIPq9tuilL//2uR9kWn5jsp8zHK",\n'
    '            "VjmXuLtHDhM9lObZGCVJwdlN2KDKTh276u/pzQ1s5u8z/ARtK26N8e5w",\n'
    '            "8mNlGcHcHfwyhjfEQurvrnkqYH37+12U3jGk5YNHGyOPcwIDAQAB",\n'
    "        );\n"
    "        let der = STANDARD.decode(public_key).unwrap();\n"
    "        let key = RsaPublicKey::from_public_key_der(&der).unwrap();\n"
    "        let mut rng = ChaCha20Rng::from_seed([7_u8; 32]);\n"
    '        let _ = key.encrypt(&mut rng, Pkcs1v15Encrypt, b"device").unwrap();\n'
    '        let _ = Md5::digest(b"payload");\n'
    "        Self\n"
    "    }",
)

LISTING_RUST_SOURCE = """#![no_std]

use aidoku::{Chapter, FilterValue, Manga, MangaPageResult, Page, Result, Source};
use aidoku::{prelude::register_source, alloc::{String, Vec}};

mod c2a_listing;

pub struct Example;

impl Source for Example {
    fn new() -> Self { Self }

    fn get_search_manga_list(
        &self,
        query: Option<String>,
        page: i32,
        filters: Vec<FilterValue>,
    ) -> Result<MangaPageResult> {
        c2a_listing::get_search_manga_list(query, page, filters)
    }

    fn get_manga_update(
        &self,
        manga: Manga,
        _needs_details: bool,
        _needs_chapters: bool,
    ) -> Result<Manga> {
        Ok(manga)
    }

    fn get_page_list(&self, _manga: Manga, _chapter: Chapter) -> Result<Vec<Page>> {
        Ok(Vec::new())
    }
}

register_source!(Example);
"""

LISTING_PROVIDER_RUST_SOURCE = LISTING_RUST_SOURCE.replace(
    "\nregister_source!(Example);",
    """
impl aidoku::ListingProvider for Example {
    fn get_manga_list(
        &self,
        listing: aidoku::Listing,
        page: i32,
    ) -> aidoku::Result<MangaPageResult> {
        c2a_listing::get_manga_list(listing, page)
    }
}

register_source!(Example, ListingProvider);""",
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


def test_pinned_rsa_and_md5_dependencies_compile_for_wasm(tmp_path: Path) -> None:
    assert find_tool("cargo")
    project, ir = scaffold_project(tmp_path, name="rsa-md5-project")
    apply_generation_manifest(
        project,
        ir,
        GenerationManifest(
            source_struct="Simple",
            files=[GeneratedFile(path="src/lib.rs", content=RSA_MD5_RUST_SOURCE)],
            dependencies=[
                DependencyRequest(name="base64"),
                DependencyRequest(name="md-5"),
                DependencyRequest(name="rand_chacha"),
                DependencyRequest(name="rand_core"),
                DependencyRequest(name="rsa"),
            ],
        ),
        query=None,
    )

    validation = validate_project(project, live=False)
    assert validation.build_ok, validation.diagnostics
    assert validation.package_ok, validation.diagnostics


def test_deterministic_listing_module_and_provider_compile_for_wasm(tmp_path: Path) -> None:
    assert find_tool("cargo")
    listing_ir = minimal_source_ir(
        source_id="zh.copymanga",
        language="zh",
        source_format="decompiled_apk",
        main_class="CopyManga",
        capabilities=[Capability.POPULAR, Capability.LATEST],
        files=_copymanga_listing_files(),
        request_header_profiles=[
            RequestHeaderProfile(
                name="API",
                domains=["api.copy3000.com", "api.manga2025.com"],
                headers={"Accept": "application/json"},
            )
        ],
    )
    rendered = render_search_listing(listing_ir)
    project, scaffold_ir = scaffold_project(tmp_path, name="listing-project")
    apply_generation_manifest(
        project,
        scaffold_ir,
        GenerationManifest(
            source_struct="Example",
            implemented_traits=["ListingProvider"],
            files=[
                GeneratedFile(path="src/lib.rs", content=LISTING_PROVIDER_RUST_SOURCE),
                rendered,
            ],
            dependencies=[DependencyRequest(name="serde", features=["derive"])],
        ),
        query=None,
    )

    validation = validate_project(project, live=False)
    assert validation.build_ok, validation.diagnostics
    assert validation.package_ok, validation.diagnostics


def test_serializable_string_mappings_compile_for_wasm(tmp_path: Path) -> None:
    assert find_tool("cargo")
    listing_ir = minimal_source_ir(
        source_format="decompiled_apk",
        main_class="Example",
        files=_serializable_listing_files(),
    )
    rendered = render_search_listing(listing_ir)
    project, scaffold_ir = scaffold_project(tmp_path, name="serializable-listing-project")
    apply_generation_manifest(
        project,
        scaffold_ir,
        GenerationManifest(
            source_struct="Example",
            files=[
                GeneratedFile(path="src/lib.rs", content=LISTING_RUST_SOURCE),
                rendered,
            ],
            dependencies=[DependencyRequest(name="serde", features=["derive"])],
        ),
        query=None,
    )

    validation = validate_project(project, live=False)
    assert validation.build_ok, validation.diagnostics
    assert validation.package_ok, validation.diagnostics
