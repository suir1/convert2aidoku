from convert2aidoku.live_remediation import remediate_failed_image_cdn
from convert2aidoku.models import (
    GeneratedFile,
    GeneratedResources,
    GenerationManifest,
    ValidationResult,
    ValidationStage,
)


def _manifest(default: str = "default") -> GenerationManifest:
    return GenerationManifest(
        source_struct="Example",
        files=[
            GeneratedFile(path="src/lib.rs", content="#![no_std]\n"),
            GeneratedFile(
                path="res/settings.json",
                content=f'''[
                    {{"type":"group","title":"Images","items":[
                        {{"type":"select","key":"v1.key.image_cdn_option",
                         "title":"CDN","default":"{default}",
                         "values":["default","random","static.example.com",
                                   "hk.images-cdn.net","us.images-cdn.net"],
                         "titles":["Default","Random","Static","HK","US"]}}
                    ]}}
                ]''',
            ),
        ],
    )


def _cover_failure(status: int = 403) -> ValidationResult:
    return ValidationResult(
        build_ok=True,
        package_ok=True,
        stages=[
            ValidationStage(
                name="core-live-smoke",
                kind="live_test",
                ok=False,
                output=f"cover image returned HTTP {status}",
            )
        ],
    )


def test_failed_cover_uses_next_recovered_external_image_cdn() -> None:
    first = remediate_failed_image_cdn(
        _manifest(),
        _cover_failure(),
        public_base_url="https://www.example.com",
    )

    assert first is not None
    assert GeneratedResources(first.manifest).setting_defaults() == {
        "v1.key.image_cdn_option": "hk.images-cdn.net"
    }
    assert first.warning.endswith("hk.images-cdn.net")

    second = remediate_failed_image_cdn(
        first.manifest,
        _cover_failure(),
        public_base_url="https://www.example.com",
    )

    assert second is not None
    assert GeneratedResources(second.manifest).setting_defaults() == {
        "v1.key.image_cdn_option": "us.images-cdn.net"
    }


def test_image_cdn_remediation_is_narrow_and_does_not_cycle() -> None:
    assert (
        remediate_failed_image_cdn(
            _manifest(),
            _cover_failure(500),
            public_base_url="https://www.example.com",
        )
        is None
    )
    assert (
        remediate_failed_image_cdn(
            _manifest("us.images-cdn.net"),
            _cover_failure(),
            public_base_url="https://www.example.com",
        )
        is None
    )
