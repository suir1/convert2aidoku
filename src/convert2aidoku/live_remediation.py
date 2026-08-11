from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .models import GeneratedResources, GenerationManifest, ValidationResult

_IMAGE_CDN_FAILURE = re.compile(r"\b(?:cover image|first image) returned HTTP 403\b")
_HOST = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LiveRemediation:
    manifest: GenerationManifest
    warning: str


def _site_suffix(host: str) -> tuple[str, ...]:
    labels = tuple(part for part in host.casefold().strip(".").split(".") if part)
    return labels[-2:]


def _image_cdn_key(key: str) -> bool:
    terms = set(filter(None, re.split(r"[^a-z0-9]+", key.casefold())))
    return {"image", "cdn"} <= terms


def remediate_failed_image_cdn(
    manifest: GenerationManifest,
    validation: ValidationResult,
    *,
    public_base_url: str,
) -> LiveRemediation | None:
    if not _IMAGE_CDN_FAILURE.search(validation.diagnostics):
        return None
    public_host = urlsplit(public_base_url).hostname
    if public_host is None:
        return None
    public_suffix = _site_suffix(public_host)
    resources = GeneratedResources(manifest)
    defaults = resources.setting_defaults()
    for key, values in resources.setting_values().items():
        if not _image_cdn_key(key):
            continue
        external_hosts = tuple(
            value
            for value in dict.fromkeys(values)
            if _HOST.fullmatch(value) and _site_suffix(value) != public_suffix
        )
        if not external_hosts:
            continue
        current = defaults.get(key, "")
        if current in external_hosts:
            index = external_hosts.index(current) + 1
            if index >= len(external_hosts):
                continue
            candidate = external_hosts[index]
        else:
            candidate = external_hosts[0]
        updated = resources.with_setting_default(key, candidate)
        if updated == manifest:
            continue
        return LiveRemediation(
            manifest=updated,
            warning="live image CDN remediation selected recovered fallback host " + candidate,
        )
    return None
