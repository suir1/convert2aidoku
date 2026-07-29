import json

from convert2aidoku.live_validation_evidence import live_validation_evidence
from convert2aidoku.models import Capability, GeneratedResources
from tests.scenarios import generation_manifest, minimal_source_ir


def test_unknown_source_has_no_live_validation_evidence() -> None:
    evidence = live_validation_evidence(minimal_source_ir())

    assert evidence.repair_context == ""
    assert evidence.setting_overrides is None


def test_known_sources_expose_their_benchmark_context() -> None:
    mycomic = live_validation_evidence(minimal_source_ir(source_id="zh.mycomic"))
    copymanga = live_validation_evidence(minimal_source_ir(source_id="zh.copymanga"))

    assert "/chapters/794527" in mycomic.repair_context
    assert "does not share the browser" in mycomic.repair_context
    assert "Version: 2025.11.21" in copymanga.repair_context
    assert "&theme=<selected path_word>" in copymanga.repair_context


def test_setting_defaults_require_matching_source_capability_and_are_fresh() -> None:
    without_capability = minimal_source_ir(source_id="zh.copymanga")
    with_capability = minimal_source_ir(
        source_id="zh.copymanga",
        capabilities=[Capability.DYNAMIC_BASE_URLS],
    )

    assert live_validation_evidence(without_capability).setting_overrides is None
    first = live_validation_evidence(with_capability).setting_overrides
    second = live_validation_evidence(with_capability).setting_overrides
    assert first == {
        "v2.pref.api_domain": "api.manga2025.com",
        "api_domain": "api.manga2025.com",
    }
    assert first is not second


def test_live_validated_setting_default_stays_inside_generated_allowlist() -> None:
    def manifest(values: list[str], *, key: str = "v2.pref.api_domain"):
        settings = [
            {
                "type": "group",
                "items": [
                    {
                        "type": "select",
                        "key": key,
                        "titles": values,
                        "values": values,
                        "default": values[0],
                    }
                ],
            }
        ]
        return generation_manifest(
            "fn source() {}",
            resources={"res/settings.json": json.dumps(settings)},
        )

    ir = minimal_source_ir(
        source_id="zh.copymanga",
        capabilities=[Capability.DYNAMIC_BASE_URLS],
    )
    overrides = live_validation_evidence(ir).setting_overrides
    allowed = GeneratedResources(
        manifest(["api.mangacopy.com", "api.manga2025.com"])
    ).with_defaults(setting_overrides=overrides)
    rejected = GeneratedResources(manifest(["api.mangacopy.com"])).with_defaults(
        setting_overrides=overrides
    )
    normalized_key = GeneratedResources(
        manifest(["api.mangacopy.com", "api.manga2025.com"], key="api_domain")
    ).with_defaults(setting_overrides=overrides)

    allowed_settings = json.loads(next(x.content for x in allowed.files if x.path.endswith("json")))
    rejected_settings = json.loads(
        next(x.content for x in rejected.files if x.path.endswith("json"))
    )
    normalized_key_settings = json.loads(
        next(x.content for x in normalized_key.files if x.path.endswith("json"))
    )
    assert allowed_settings[0]["items"][0]["default"] == "api.manga2025.com"
    assert rejected_settings[0]["items"][0]["default"] == "api.mangacopy.com"
    assert normalized_key_settings[0]["items"][0]["default"] == "api.manga2025.com"
