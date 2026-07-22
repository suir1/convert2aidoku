from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, SecretStr

from .constants import DEFAULT_AI_TIMEOUT_SECONDS, DEFAULT_MAX_REPAIR_ROUNDS, MAX_REPAIR_ROUNDS
from .errors import ConfigurationError


class AISettings(BaseModel):
    base_url: str
    model: str
    api_key: SecretStr
    timeout_seconds: float = DEFAULT_AI_TIMEOUT_SECONDS
    max_repair_rounds: int = DEFAULT_MAX_REPAIR_ROUNDS

    @property
    def chat_completions_url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"


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


def load_ai_settings(
    *,
    base_url: str | None = None,
    model: str | None = None,
    config_path: Path | None = None,
    max_repair_rounds: int | None = None,
) -> AISettings:
    config = _read_config(config_path)
    resolved_base_url = base_url or os.getenv("C2A_BASE_URL") or config.get("base_url")
    resolved_model = model or os.getenv("C2A_MODEL") or config.get("model")
    api_key = os.getenv("C2A_API_KEY")

    missing = []
    if not resolved_base_url:
        missing.append("base URL (--base-url, C2A_BASE_URL, or c2a.toml)")
    if not resolved_model:
        missing.append("model (--model, C2A_MODEL, or c2a.toml)")
    if not api_key:
        missing.append("C2A_API_KEY")
    if missing:
        raise ConfigurationError("missing AI configuration: " + ", ".join(missing))

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
    return AISettings(
        base_url=str(resolved_base_url),
        model=str(resolved_model),
        api_key=SecretStr(api_key),
        timeout_seconds=timeout,
        max_repair_rounds=rounds,
    )
