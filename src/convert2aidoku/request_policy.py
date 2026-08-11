from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .models import (
    GeneratedFile,
    GeneratedResources,
    GenerationManifest,
    SourceIR,
    ValidationResult,
)
from .rust_inspection import RustInspection

_STRING = r'"(?:\\.|[^"\\])*"'
_IMAGE_CDN_FAILURE = re.compile(r"\b(?:cover image|first image) returned HTTP 403\b")
_HOST = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?",
    re.I,
)


@dataclass(frozen=True)
class _PageBypass:
    marker: str
    path_prefix: str
    hosts: tuple[str, ...]
    headers: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RequestPolicyRemediation:
    manifest: GenerationManifest
    warning: str


def _decode(literal: str) -> str | None:
    try:
        value = json.loads(literal)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


def _balanced_block(content: str, opening: int) -> str | None:
    depth = 0
    quote = None
    escaped = False
    for index in range(opening, len(content)):
        character = content[index]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
        elif character in {'"', "'"}:
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return content[opening + 1 : index]
    return None


def _literal_pairs(content: str) -> tuple[tuple[str, str], ...]:
    pairs = []
    for match in re.finditer(
        rf"\.header\(\s*(?P<name>{_STRING})\s*,\s*(?P<value>{_STRING})\s*\)",
        content,
    ):
        name, value = _decode(match.group("name")), _decode(match.group("value"))
        if name is not None and value is not None:
            pairs.append((name, value))
    return tuple(dict.fromkeys(pairs))


def _recover_page_bypass(ir: SourceIR) -> _PageBypass | None:
    java = "\n".join(source.content for source in ir.files)
    marker_match = re.search(rf"\blast[A-Za-z0-9_]*Mark\s*=\s*(?P<value>{_STRING})\s*;", java, re.I)
    hosts_match = re.search(
        r"\b[A-Za-z_]*bypass[A-Za-z_]*hosts?\s*=\s*"
        r"CollectionsKt\.listOf\(new\s+String\[\]\s*\{(?P<value>[^}]+)\}\)",
        java,
        re.I,
    )
    if marker_match is None or hosts_match is None:
        return None
    marker = _decode(marker_match.group("value"))
    hosts = tuple(
        value
        for literal in re.findall(_STRING, hosts_match.group("value"))
        if (value := _decode(literal)) is not None
    )
    if not marker or not hosts:
        return None
    for source in ir.files:
        content = source.content
        if not all(
            token in content
            for token in ("implements Interceptor", "isPageListLink", "removeSuffix", "encodedPath")
        ):
            continue
        opening = content.find("{", content.find("isPageListLink"))
        block = _balanced_block(content, opening) if opening >= 0 else None
        if block is None:
            continue
        prefix_match = re.search(
            rf"\bString\s+[A-Za-z_]\w*\s*=\s*(?P<value>{_STRING})\s*\+\s*"
            r"StringsKt\.removeSuffix",
            block,
        )
        prefix = _decode(prefix_match.group("value")) if prefix_match else None
        headers = _literal_pairs(block)
        if prefix and prefix.startswith("/") and headers:
            return _PageBypass(marker, prefix.rstrip("/"), tuple(dict.fromkeys(hosts)), headers)
    return None


def _join_named_base_urls(content: str) -> str:
    pattern = re.compile(
        r'(?P<head>\bformat!\s*\(\s*")\{\}(?P<path>[A-Za-z0-9])'
        r'(?P<tail>(?:\\.|[^"\\])*")(?P<separator>\s*,\s*)'
        r"(?P<provider>(?:self\s*\.\s*)?[A-Za-z_]\w*(?:\s*\(\s*\))?)"
        r"(?P<end>\s*(?=,|\)))"
    )

    def replace(match: re.Match[str]) -> str:
        identifiers = re.findall(r"[A-Za-z_]\w*", match.group("provider"))
        return (
            match.expand(r"\g<head>{}/\g<path>\g<tail>\g<separator>\g<provider>\g<end>")
            if identifiers and {"base", "domain", "origin"}.intersection(identifiers[-1].split("_"))
            else match.group(0)
        )

    return pattern.sub(replace, content)


def _site_suffix(host: str) -> tuple[str, ...]:
    return tuple(filter(None, host.casefold().strip(".").split(".")))[-2:]


def _rewrite_direct_setting_fallback(content: str, key: str, default: str) -> str:
    key_literal, default_literal = json.dumps(key), json.dumps(default)
    for node in RustInspection.from_content(content).nodes("match_expression"):
        match_text = node.text.decode("utf-8", errors="replace")
        scrutinee = match_text.split("{", 1)[0]
        direct_setting = re.fullmatch(
            rf"\s*match\s+(?:aidoku::imports::defaults::)?"
            rf"defaults_get(?:::<String>)?\(\s*{re.escape(key_literal)}\s*\)\s*",
            scrutinee,
        )
        if direct_setting is None:
            continue
        normalized = re.sub(
            rf"(?P<prefix>\b_\s*=>\s*)"
            rf"(?:String::from\(\s*{_STRING}\s*\)|{_STRING}\.to_string\(\)|{_STRING}\.into\(\))",
            rf"\g<prefix>String::from({default_literal})",
            match_text,
        )
        if normalized != match_text:
            content = content.replace(match_text, normalized, 1)
    return content


def _with_content(generated: GeneratedFile, content: str) -> GeneratedFile:
    if content == generated.content:
        return generated
    return generated.model_copy(update={"content": content})


def _bypass_helpers(policy: _PageBypass, normal_headers: tuple[tuple[str, str], ...]) -> str:
    hosts = ", ".join(json.dumps(host) for host in policy.hosts)
    normal = "\n".join(
        f"    request = request.header({json.dumps(name)}, {json.dumps(value)});"
        for name, value in normal_headers
    )
    bypass = "\n".join(
        f"        request = request.header({json.dumps(name)}, {json.dumps(value)});"
        for name, value in policy.headers
    )
    return f"""const C2A_PAGE_BYPASS_HOSTS: &[&str] = &[{hosts}];

fn c2a_page_bypass_url(path: &str) -> String {{
    let clean = path.strip_suffix({json.dumps(policy.marker)}).unwrap_or(path);
    let route = clean.split_once("://")
        .and_then(|(_, value)| value.find('/').map(|index| &value[index..]))
        .unwrap_or(clean);
    format!("https://{{}}{{}}/{{}}", C2A_PAGE_BYPASS_HOSTS[0],
        {json.dumps(policy.path_prefix)}, route.trim_start_matches('/'))
}}

fn c2a_page_bypass_request(url: &str) -> Result<Request> {{
    let mut request = Request::get(url)?;
{normal}
    if C2A_PAGE_BYPASS_HOSTS.iter().any(|host| url.contains(host)) {{
{bypass}
    }}
    Ok(request)
}}"""


def _project_page_bypass(
    files: list[GeneratedFile], policy: _PageBypass | None
) -> list[GeneratedFile]:
    if policy is None:
        return files
    for index, generated in enumerate(files):
        content = generated.content
        if not generated.path.endswith(".rs") or "c2a_page_bypass_url" in content:
            continue
        functions = RustInspection.from_content(content).functions
        absolute = next((item for item in functions if item.name == "absolute_url"), None)
        request = next(
            (
                item
                for item in functions
                if "Request::get" in item.text
                and ".send()" in item.text
                and "Response" in item.text.split("{", 1)[0]
                and re.search(r"\burl\s*:\s*String\b", item.text.split("{", 1)[0])
            ),
            None,
        )
        if absolute is None or request is None:
            continue
        absolute_open = absolute.text.find("{")
        request_open = request.text.find("{")
        path = re.search(
            r"\(\s*&self\s*,\s*(?P<name>[A-Za-z_]\w*)\s*:\s*&str\b",
            absolute.text[:absolute_open],
        )
        url = re.search(
            r"\(\s*&self\s*,\s*(?P<name>[A-Za-z_]\w*)\s*:\s*String\b",
            request.text[:request_open],
        )
        if path is None or url is None:
            continue
        path_name, url_name = path.group("name"), url.group("name")
        absolute_text = (
            absolute.text[: absolute_open + 1]
            + f"\n        if {path_name}.ends_with({json.dumps(policy.marker)}) {{\n"
            + f"            return c2a_page_bypass_url({path_name});\n        }}"
            + absolute.text[absolute_open + 1 :]
        )
        request_text = (
            request.text[:request_open].rstrip()
            + " {\n"
            + f"        let response = match c2a_page_bypass_request(&{url_name})?.send() {{\n"
            + "            Ok(response) => response,\n"
            + f"            Err(_) => c2a_page_bypass_request(&{url_name})?.send()?,\n"
            + "        };\n        Ok(response)\n    }"
        )
        content = content.replace(absolute.text, absolute_text, 1)
        content = content.replace(request.text, request_text, 1)
        content = (
            content.rstrip() + "\n\n" + _bypass_helpers(policy, _literal_pairs(request.text)) + "\n"
        )
        return [*files[:index], _with_content(generated, content), *files[index + 1 :]]
    return files


@dataclass(frozen=True)
class RequestPolicy:
    public_base_url: str
    _page_bypass: _PageBypass | None

    @classmethod
    def from_source_ir(cls, ir: SourceIR) -> RequestPolicy:
        return cls(ir.metadata.base_url, _recover_page_bypass(ir))

    def project(self, files: list[GeneratedFile]) -> list[GeneratedFile]:
        joined = [
            _with_content(generated, _join_named_base_urls(generated.content))
            if generated.path.endswith(".rs")
            else generated
            for generated in files
        ]
        return _project_page_bypass(joined, self._page_bypass)

    def remediate(
        self,
        manifest: GenerationManifest,
        validation: ValidationResult,
    ) -> RequestPolicyRemediation | None:
        if (
            not _IMAGE_CDN_FAILURE.search(validation.diagnostics)
            or (public_host := urlsplit(self.public_base_url).hostname) is None
        ):
            return None
        public_suffix = _site_suffix(public_host)
        resources = GeneratedResources(manifest)
        for key, values in resources.setting_values().items():
            terms = set(filter(None, re.split(r"[^a-z0-9]+", key.casefold())))
            if not {"image", "cdn"} <= terms:
                continue
            hosts = tuple(
                value
                for value in dict.fromkeys(values)
                if _HOST.fullmatch(value) and _site_suffix(value) != public_suffix
            )
            current = resources.setting_defaults().get(key, "")
            index = hosts.index(current) + 1 if current in hosts else 0
            if index >= len(hosts):
                continue
            candidate = hosts[index]
            updated = resources.with_setting_default(key, candidate)
            files = [
                _with_content(
                    generated,
                    _rewrite_direct_setting_fallback(generated.content, key, candidate),
                )
                if generated.path.endswith(".rs")
                else generated
                for generated in updated.files
            ]
            if files == updated.files:
                continue
            return RequestPolicyRemediation(
                manifest=updated.model_copy(update={"files": files}),
                warning="live image CDN remediation selected recovered fallback host " + candidate,
            )
        return None
