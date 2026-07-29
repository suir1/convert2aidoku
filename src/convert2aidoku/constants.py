from __future__ import annotations

DEFAULT_MAX_REPAIR_ROUNDS = 1
MAX_REPAIR_ROUNDS = 8
DEFAULT_AI_TIMEOUT_SECONDS = 300.0
DEFAULT_MAX_INPUT_CHARS = 160_000
DEFAULT_MAX_DECOMPILED_INPUT_CHARS = 335_000
MAX_AI_DIAGNOSTIC_CHARS = 24_000
MAX_AI_RESPONSE_BYTES = 2_000_000
MAX_GENERATED_FILES = 64
MAX_GENERATED_FILE_CHARS = 500_000
MAX_GENERATED_TOTAL_CHARS = 2_000_000

BLOCKED_OUTPUT_MARKERS = (
    "http 403",
    "status code: 403",
    "cloudflare",
    "captcha",
    "cf-chl",
    "connection refused",
    "connection reset",
    "dns error",
    "timed out",
    "timeout",
    "site unavailable",
)
