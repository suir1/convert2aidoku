from __future__ import annotations

AIDOKU_RS_REPOSITORY = "https://github.com/Aidoku/aidoku-rs.git"
AIDOKU_RS_REV = "1a6bb691dd67c7151fc76fc852fb5a364d325f72"

DEFAULT_MAX_REPAIR_ROUNDS = 3
MAX_REPAIR_ROUNDS = 8
DEFAULT_AI_TIMEOUT_SECONDS = 300.0
DEFAULT_MAX_INPUT_CHARS = 160_000
DEFAULT_MAX_DECOMPILED_INPUT_CHARS = 335_000
MAX_AI_DIAGNOSTIC_CHARS = 24_000
MAX_AI_RESPONSE_BYTES = 2_000_000
MAX_GENERATED_FILES = 64
MAX_GENERATED_FILE_CHARS = 500_000
MAX_GENERATED_TOTAL_CHARS = 2_000_000

ALLOWED_GENERATED_FILES = (
    "src/**/*.rs",
    "res/filters.json",
    "res/settings.json",
)

DEPENDENCY_SPECS: dict[str, dict[str, object]] = {
    "serde": {
        "version": "1.0.219",
        "default-features": False,
        "features": ["derive"],
    },
    "serde_json": {
        "version": "1.0.140",
        "default-features": False,
        "features": ["alloc"],
    },
    "regex": {
        "version": "1.11.1",
        "default-features": False,
        "features": ["unicode"],
    },
    "base64": {
        "version": "0.22.1",
        "default-features": False,
        "features": ["alloc"],
    },
    "aes": {
        "version": "0.8.4",
        "default-features": False,
        "features": [],
    },
    "cbc": {
        "version": "0.1.2",
        "default-features": False,
        "features": ["block-padding"],
    },
    "hex": {
        "version": "0.4.3",
        "default-features": False,
        "features": ["alloc"],
    },
}

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
