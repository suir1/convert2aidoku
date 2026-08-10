from __future__ import annotations

import os
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field, SecretStr

from .constants import (
    DEFAULT_AI_TIMEOUT_SECONDS,
    DEFAULT_GENERATION_MAX_TOKENS,
    DEFAULT_MAX_REPAIR_ROUNDS,
    DEFAULT_REPAIR_MAX_TOKENS,
    MAX_AI_MAX_TOKENS,
    MAX_REPAIR_ROUNDS,
    MIN_AI_MAX_TOKENS,
)
from .errors import ConfigurationError


class ReasoningEffort(StrEnum):
    AUTO = "auto"
    OFF = "off"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AISettings(BaseModel):
    base_url: str = ""
    model: str = ""
    api_key: SecretStr = SecretStr("")
    timeout_seconds: float = DEFAULT_AI_TIMEOUT_SECONDS
    max_repair_rounds: int = DEFAULT_MAX_REPAIR_ROUNDS
    generation_reasoning_effort: ReasoningEffort = ReasoningEffort.OFF
    repair_reasoning_effort: ReasoningEffort = ReasoningEffort.LOW
    generation_max_tokens: int = Field(
        default=DEFAULT_GENERATION_MAX_TOKENS,
        ge=MIN_AI_MAX_TOKENS,
        le=MAX_AI_MAX_TOKENS,
    )
    repair_max_tokens: int = Field(
        default=DEFAULT_REPAIR_MAX_TOKENS,
        ge=MIN_AI_MAX_TOKENS,
        le=MAX_AI_MAX_TOKENS,
    )

    @property
    def chat_completions_url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    @property
    def provider_configured(self) -> bool:
        return bool(self.base_url and self.model and self.api_key.get_secret_value())

    def require_provider(self) -> Self:
        missing = []
        if not self.base_url:
            missing.append("base URL (--base-url, C2A_BASE_URL, or c2a.toml)")
        if not self.model:
            missing.append("model (--model, C2A_MODEL, or c2a.toml)")
        if not self.api_key.get_secret_value():
            missing.append("C2A_API_KEY")
        if missing:
            raise ConfigurationError(
                "AI configuration is required for this source: " + ", ".join(missing)
            )
        return self


def _read_config(path: Path | None) -> dict[str, object]:
    config_path = path or Path("c2a.toml")
    if not config_path.exists():
        return {}
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    ai = data.get("ai", {})
    if not isinstance(ai, dict):
        raise ConfigurationError("[ai] in c2a.toml must be a table")
    if "api_key" in ai:
        raise ConfigurationError("API keys are not allowed in c2a.toml; use C2A_API_KEY")
    return ai


def ai_config_defaults(config_path: Path | None = None) -> dict[str, str]:
    """Return non-secret provider defaults for interactive clients such as the local Web UI."""
    config = _read_config(config_path)
    return {
        "base_url": str(os.getenv("C2A_BASE_URL") or config.get("base_url") or ""),
        "model": str(os.getenv("C2A_MODEL") or config.get("model") or ""),
    }


def load_ai_settings(
    *,
    base_url: str | None = None,
    model: str | None = None,
    config_path: Path | None = None,
    max_repair_rounds: int | None = None,
    generation_reasoning_effort: ReasoningEffort | str | None = None,
    repair_reasoning_effort: ReasoningEffort | str | None = None,
    generation_max_tokens: int | None = None,
    repair_max_tokens: int | None = None,
    require_provider: bool = True,
) -> AISettings:
    config = _read_config(config_path)
    resolved_base_url = base_url or os.getenv("C2A_BASE_URL") or config.get("base_url")
    resolved_model = model or os.getenv("C2A_MODEL") or config.get("model")
    api_key = os.getenv("C2A_API_KEY")

    configured_rounds = config.get("max_repair_rounds", DEFAULT_MAX_REPAIR_ROUNDS)
    env_rounds = os.getenv("C2A_MAX_REPAIR_ROUNDS")
    rounds = (
        max_repair_rounds
        if max_repair_rounds is not None
        else int(env_rounds)
        if env_rounds is not None
        else int(configured_rounds)
    )
    if not 0 <= rounds <= MAX_REPAIR_ROUNDS:
        raise ConfigurationError(f"max repair rounds must be between 0 and {MAX_REPAIR_ROUNDS}")

    timeout = float(
        os.getenv("C2A_TIMEOUT_SECONDS", config.get("timeout_seconds", DEFAULT_AI_TIMEOUT_SECONDS))
    )

    def reasoning_effort(
        explicit: ReasoningEffort | str | None,
        env_name: str,
        config_name: str,
        default: ReasoningEffort,
    ) -> ReasoningEffort:
        raw = explicit or os.getenv(env_name) or config.get(config_name, default)
        try:
            return ReasoningEffort(str(raw))
        except ValueError as exc:
            values = ", ".join(item.value for item in ReasoningEffort)
            raise ConfigurationError(f"{config_name} must be one of: {values}") from exc

    def max_tokens(
        explicit: int | None,
        env_name: str,
        config_name: str,
        default: int,
    ) -> int:
        raw = (
            explicit
            if explicit is not None
            else os.getenv(env_name) or config.get(config_name, default)
        )
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"{config_name} must be an integer") from exc
        if not MIN_AI_MAX_TOKENS <= value <= MAX_AI_MAX_TOKENS:
            raise ConfigurationError(
                f"{config_name} must be between {MIN_AI_MAX_TOKENS} and {MAX_AI_MAX_TOKENS}"
            )
        return value

    settings = AISettings(
        base_url=str(resolved_base_url or ""),
        model=str(resolved_model or ""),
        api_key=SecretStr(api_key or ""),
        timeout_seconds=timeout,
        max_repair_rounds=rounds,
        generation_reasoning_effort=reasoning_effort(
            generation_reasoning_effort,
            "C2A_GENERATION_REASONING_EFFORT",
            "generation_reasoning_effort",
            ReasoningEffort.OFF,
        ),
        repair_reasoning_effort=reasoning_effort(
            repair_reasoning_effort,
            "C2A_REPAIR_REASONING_EFFORT",
            "repair_reasoning_effort",
            ReasoningEffort.LOW,
        ),
        generation_max_tokens=max_tokens(
            generation_max_tokens,
            "C2A_GENERATION_MAX_TOKENS",
            "generation_max_tokens",
            DEFAULT_GENERATION_MAX_TOKENS,
        ),
        repair_max_tokens=max_tokens(
            repair_max_tokens,
            "C2A_REPAIR_MAX_TOKENS",
            "repair_max_tokens",
            DEFAULT_REPAIR_MAX_TOKENS,
        ),
    )
    return settings.require_provider() if require_provider else settings
