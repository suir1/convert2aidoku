from pathlib import Path

import pytest

from convert2aidoku.config import ReasoningEffort, ai_config_defaults, load_ai_settings
from convert2aidoku.errors import ConfigurationError


def test_loads_env_key_without_exposing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("C2A_API_KEY", "secret-value")
    settings = load_ai_settings(base_url="http://localhost/v1", model="model")
    assert settings.api_key.get_secret_value() == "secret-value"
    assert "secret-value" not in repr(settings)
    assert settings.generation_reasoning_effort == ReasoningEffort.AUTO
    assert settings.repair_reasoning_effort == ReasoningEffort.LOW
    assert settings.max_repair_rounds == 1


def test_non_secret_defaults_are_available_to_interactive_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "c2a.toml"
    config.write_text('[ai]\nbase_url="http://config/v1"\nmodel="config-model"\n')
    monkeypatch.setenv("C2A_MODEL", "environment-model")
    monkeypatch.setenv("C2A_API_KEY", "must-not-be-returned")

    defaults = ai_config_defaults(config)

    assert defaults == {
        "base_url": "http://config/v1",
        "model": "environment-model",
    }
    assert "must-not-be-returned" not in repr(defaults)


def test_rejects_key_in_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "c2a.toml"
    config.write_text('[ai]\nbase_url="http://x/v1"\nmodel="m"\napi_key="bad"\n')
    monkeypatch.setenv("C2A_API_KEY", "from-env")
    with pytest.raises(ConfigurationError, match="not allowed"):
        load_ai_settings(config_path=config)


def test_repair_and_timeout_environment_overrides_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("C2A_API_KEY", "from-env")
    monkeypatch.setenv("C2A_MAX_REPAIR_ROUNDS", "1")
    monkeypatch.setenv("C2A_TIMEOUT_SECONDS", "12")
    settings = load_ai_settings(
        base_url="http://localhost/v1",
        model="model",
        config_path=Path("/does/not/exist"),
    )
    assert settings.max_repair_rounds == 1
    assert settings.timeout_seconds == 12


def test_rejects_more_than_eight_cumulative_repairs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("C2A_API_KEY", "from-env")
    with pytest.raises(ConfigurationError, match="between 0 and 8"):
        load_ai_settings(
            base_url="http://localhost/v1",
            model="model",
            max_repair_rounds=9,
        )


def test_reasoning_effort_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = tmp_path / "c2a.toml"
    config.write_text(
        '[ai]\nbase_url="http://x/v1"\nmodel="m"\n'
        'generation_reasoning_effort="high"\nrepair_reasoning_effort="medium"\n'
    )
    monkeypatch.setenv("C2A_API_KEY", "from-env")
    monkeypatch.setenv("C2A_GENERATION_REASONING_EFFORT", "off")

    settings = load_ai_settings(
        config_path=config,
        repair_reasoning_effort=ReasoningEffort.HIGH,
    )

    assert settings.generation_reasoning_effort == ReasoningEffort.OFF
    assert settings.repair_reasoning_effort == ReasoningEffort.HIGH


def test_rejects_invalid_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("C2A_API_KEY", "from-env")
    monkeypatch.setenv("C2A_REPAIR_REASONING_EFFORT", "extreme")

    with pytest.raises(ConfigurationError, match="repair_reasoning_effort must be one of"):
        load_ai_settings(base_url="http://localhost/v1", model="model")
