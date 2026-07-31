from __future__ import annotations

DEFAULT_MAX_REPAIR_ROUNDS = 1
MAX_REPAIR_ROUNDS = 8
DEFAULT_AI_TIMEOUT_SECONDS = 300.0
DEFAULT_MAX_INPUT_CHARS = 160_000
DEFAULT_MAX_DECOMPILED_INPUT_CHARS = 335_000
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/114.0"
)
MAX_AI_DIAGNOSTIC_CHARS = 24_000
MAX_AI_RESPONSE_BYTES = 2_000_000
MAX_GENERATED_FILES = 64
MAX_GENERATED_FILE_CHARS = 500_000
MAX_GENERATED_TOTAL_CHARS = 2_000_000

# Aidoku's networking runtime negotiates and decodes response compression. Forwarding this
# source-client header can leave get_html/get_json_owned with raw compressed bytes.
AIDOKU_RUNTIME_MANAGED_REQUEST_HEADERS = frozenset({"accept-encoding"})
