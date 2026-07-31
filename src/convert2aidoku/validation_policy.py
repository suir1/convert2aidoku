from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from .models import ValidationBlocker

BLOCKED_HTTP_STATUSES = frozenset({403, 429, 503, 521, 522, 523, 524, 567})

_CHALLENGE_BODY_MARKERS = (
    "/cdn-cgi/challenge-platform/",
    "cf-chl-",
    "cf-turnstile",
    "checking your browser",
    "just a moment...",
    "attention required! | cloudflare",
    "verify you are human",
    "g-recaptcha",
    "hcaptcha",
)
_REMOTE_RESPONSE_MARKERS = (
    "cloudflare",
    "captcha",
    "cf-chl",
    "site unavailable",
)
_NETWORK_CANDIDATE_MARKERS = (
    "networkerror",
    "request failed",
    "connection error",
    "connection refused",
    "connection reset",
    "dns error",
    "timed out",
    "timeout",
)
_EMPTY_LISTING_MARKERS = (
    "search/list returned no manga",
    "popular listing returned no manga",
    "latest listing returned no manga",
)
_JSON_RESPONSE_MARKER = 'jsonparseerror(error("expected value", line: 1, column: 1))'
_HTTP_STATUS = re.compile(
    r"(?:\bhttp\s+|\bstatus\s+code:\s*)"
    + r"(?:"
    + "|".join(str(status) for status in sorted(BLOCKED_HTTP_STATUSES))
    + r")\b"
)


class RunnerFailureKind(StrEnum):
    ANONYMOUS_INITIALIZATION = "anonymous_initialization"
    JSON_RESPONSE = "json_response"
    TRANSPORT = "transport"
    REMOTE_RESPONSE = "remote_response"
    EMPTY_LISTING = "empty_listing"
    NETWORK_CANDIDATE = "network_candidate"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RunnerFailureAssessment:
    kind: RunnerFailureKind

    @property
    def direct_blocker(self) -> ValidationBlocker | None:
        if self.kind is RunnerFailureKind.ANONYMOUS_INITIALIZATION:
            return ValidationBlocker.ANONYMOUS_INITIALIZATION
        return None

    @property
    def requires_probe(self) -> bool:
        return self.kind not in {
            RunnerFailureKind.ANONYMOUS_INITIALIZATION,
            RunnerFailureKind.UNKNOWN,
        }


@dataclass(frozen=True)
class BlockerEvidence:
    blocker: ValidationBlocker
    diagnostic: str


def assess_runner_failure(output: str) -> RunnerFailureAssessment:
    """Classify failed live output without declaring an external blocker by text alone."""
    lowered = output.casefold()
    if "初始化失败" in output:
        kind = RunnerFailureKind.ANONYMOUS_INITIALIZATION
    elif _JSON_RESPONSE_MARKER in lowered:
        kind = RunnerFailureKind.JSON_RESPONSE
    elif "requesterror" in lowered:
        kind = RunnerFailureKind.TRANSPORT
    elif _HTTP_STATUS.search(lowered) or any(
        marker in lowered for marker in _REMOTE_RESPONSE_MARKERS
    ):
        kind = RunnerFailureKind.REMOTE_RESPONSE
    elif any(marker in lowered for marker in _EMPTY_LISTING_MARKERS):
        kind = RunnerFailureKind.EMPTY_LISTING
    elif any(marker in lowered for marker in _NETWORK_CANDIDATE_MARKERS):
        kind = RunnerFailureKind.NETWORK_CANDIDATE
    else:
        kind = RunnerFailureKind.UNKNOWN
    return RunnerFailureAssessment(kind)


def is_browser_challenge(body: str) -> bool:
    lowered = body.casefold()
    return any(marker in lowered for marker in _CHALLENGE_BODY_MARKERS)


def is_blocked_http_status(status_code: int) -> bool:
    return status_code in BLOCKED_HTTP_STATUSES
