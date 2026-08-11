from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .models import GeneratedFile, SourceIR
from .rust_inspection import RustInspection

_JAVA_STRING = r'"(?:\\.|[^"\\])*"'


@dataclass(frozen=True)
class PageBypassPolicy:
    marker: str
    path_prefix: str
    hosts: tuple[str, ...]
    headers: tuple[tuple[str, str], ...]


def _decode(literal: str) -> str | None:
    try:
        value = json.loads(literal)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


def _balanced_block(content: str, opening: int) -> str | None:
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(content)):
        character = content[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return content[opening + 1 : index]
    return None


def recover_page_bypass_policy(ir: SourceIR) -> PageBypassPolicy | None:
    java = "\n".join(source.content for source in ir.files)
    marker_match = re.search(
        rf"\blast[A-Za-z0-9_]*Mark\s*=\s*(?P<marker>{_JAVA_STRING})\s*;",
        java,
        re.IGNORECASE,
    )
    hosts_match = re.search(
        r"\b[A-Za-z_]*bypass[A-Za-z_]*hosts?\s*=\s*"
        r"CollectionsKt\.listOf\(new\s+String\[\]\s*\{(?P<hosts>[^}]+)\}\)",
        java,
        re.IGNORECASE,
    )
    if marker_match is None or hosts_match is None:
        return None
    marker = _decode(marker_match.group("marker"))
    hosts = tuple(
        value
        for literal in re.findall(_JAVA_STRING, hosts_match.group("hosts"))
        if (value := _decode(literal)) is not None
    )
    if not marker or not hosts:
        return None

    for source in ir.files:
        content = source.content
        if not all(
            needle in content
            for needle in (
                "implements Interceptor",
                "isPageListLink",
                "removeSuffix",
                "encodedPath",
            )
        ):
            continue
        condition = content.find("isPageListLink")
        opening = content.find("{", condition)
        if opening < 0 or (block := _balanced_block(content, opening)) is None:
            continue
        prefix_match = re.search(
            rf"\bString\s+[A-Za-z_]\w*\s*=\s*(?P<prefix>{_JAVA_STRING})\s*\+\s*"
            r"StringsKt\.removeSuffix",
            block,
        )
        if prefix_match is None:
            continue
        path_prefix = _decode(prefix_match.group("prefix"))
        if not path_prefix or not path_prefix.startswith("/"):
            continue
        headers: list[tuple[str, str]] = []
        for found in re.finditer(
            rf"\.header\(\s*(?P<name>{_JAVA_STRING})\s*,\s*"
            rf"(?P<value>{_JAVA_STRING})\s*\)",
            block,
        ):
            name = _decode(found.group("name"))
            value = _decode(found.group("value"))
            if name is not None and value is not None:
                headers.append((name, value))
        if not headers:
            continue
        return PageBypassPolicy(
            marker=marker,
            path_prefix=path_prefix.rstrip("/"),
            hosts=tuple(dict.fromkeys(hosts)),
            headers=tuple(dict.fromkeys(headers)),
        )
    return None


def _request_headers(function: str) -> tuple[tuple[str, str], ...]:
    headers: list[tuple[str, str]] = []
    for found in re.finditer(
        rf"\.header\(\s*(?P<name>{_JAVA_STRING})\s*,\s*"
        rf"(?P<value>{_JAVA_STRING})\s*\)",
        function,
    ):
        name = _decode(found.group("name"))
        value = _decode(found.group("value"))
        if name is not None and value is not None:
            headers.append((name, value))
    return tuple(dict.fromkeys(headers))


def _helpers(policy: PageBypassPolicy, normal_headers: tuple[tuple[str, str], ...]) -> str:
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
    let route = clean
        .split_once("://")
        .and_then(|(_, value)| value.find('/').map(|index| &value[index..]))
        .unwrap_or(clean);
    format!(
        "https://{{}}{{}}/{{}}",
        C2A_PAGE_BYPASS_HOSTS[0],
        {json.dumps(policy.path_prefix)},
        route.trim_start_matches('/'),
    )
}}

fn c2a_page_bypass_request(url: &str) -> Result<Request> {{
    let mut request = Request::get(url)?;
{normal}
    if C2A_PAGE_BYPASS_HOSTS.iter().any(|host| url.contains(host)) {{
{bypass}
    }}
    Ok(request)
}}"""


def project_recovered_page_bypass(ir: SourceIR, files: list[GeneratedFile]) -> list[GeneratedFile]:
    policy = recover_page_bypass_policy(ir)
    if policy is None:
        return files
    updated: list[GeneratedFile] = []
    projected = False
    for generated in files:
        content = generated.content
        if projected or not generated.path.endswith(".rs") or "c2a_page_bypass_url" in content:
            updated.append(generated)
            continue
        inspection = RustInspection.from_content(content)
        absolute = next(
            (function for function in inspection.functions if function.name == "absolute_url"),
            None,
        )
        request = next(
            (
                function
                for function in inspection.functions
                if "Request::get" in function.text
                and ".send()" in function.text
                and "Response" in function.text.split("{", 1)[0]
                and re.search(r"\burl\s*:\s*String\b", function.text.split("{", 1)[0])
            ),
            None,
        )
        if absolute is None or request is None:
            updated.append(generated)
            continue
        opening = absolute.text.find("{")
        argument = re.search(
            r"\(\s*&self\s*,\s*(?P<name>[A-Za-z_]\w*)\s*:\s*&str\b",
            absolute.text[:opening],
        )
        request_opening = request.text.find("{")
        url_argument = re.search(
            r"\(\s*&self\s*,\s*(?P<name>[A-Za-z_]\w*)\s*:\s*String\b",
            request.text[:request_opening],
        )
        if opening < 0 or request_opening < 0 or argument is None or url_argument is None:
            updated.append(generated)
            continue
        path = argument.group("name")
        normalized_absolute = (
            absolute.text[: opening + 1]
            + f"\n        if {path}.ends_with({json.dumps(policy.marker)}) {{\n"
            + f"            return c2a_page_bypass_url({path});\n"
            + "        }"
            + absolute.text[opening + 1 :]
        )
        url = url_argument.group("name")
        header = request.text[:request_opening].rstrip()
        normalized_request = (
            header
            + " {\n"
            + f"        let response = match c2a_page_bypass_request(&{url})?.send() {{\n"
            + "            Ok(response) => response,\n"
            + f"            Err(_) => c2a_page_bypass_request(&{url})?.send()?,\n"
            + "        };\n"
            + "        Ok(response)\n"
            + "    }"
        )
        content = content.replace(absolute.text, normalized_absolute, 1)
        content = content.replace(request.text, normalized_request, 1)
        helpers = _helpers(policy, _request_headers(request.text))
        content = content.rstrip() + "\n\n" + helpers + "\n"
        projected = True
        updated.append(generated.model_copy(update={"content": content}))
    return updated
