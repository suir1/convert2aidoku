from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePath
from xml.etree import ElementTree

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

from .errors import InputError, UnsupportedSourceError
from .models import SourceFile

_ANDROID_XML_NAMESPACE = "http://schemas.android.com/apk/res/android"
_OPTIONAL_CLASS_MARKERS = (
    "AuthorizationInterceptor",
    "ChapterComment",
    "CollectInfo",
    "CollectResult",
    "CommentInfo",
    "LoginResult",
    "TokenProvider",
)
_HELPER_PRIORITY = (
    "PluginMetaData.java",
    "ApiRepo.java",
    "ApiResponse.java",
    "ApiResponseKt.java",
    "ApiDomainOption.java",
    "FilterKt.java",
    "ThemeResult.java",
    "ThemeDetail.java",
    "TypeFilter.java",
    "RankFilter.java",
    "AudienceFilter.java",
    "RegionFilter.java",
    "ThemeFilter.java",
    "FreeTypeFilter.java",
    "SortFilter.java",
    "ResolutionOption.java",
    "CCOption.java",
    "LatestUpdateOption.java",
    "PlatFormOption.java",
    "UserAgentType.java",
    "PreferencesKeys.java",
    "HeadersInterceptor.java",
    "UserAgentInterceptor.java",
    "ContentResult.java",
    "ChapterDetail.java",
    "ContentItem.java",
    "ComicDetailResult.java",
    "ComicDetail.java",
    "Status.java",
    "MangaStatusManager.java",
    "GroupInfo.java",
    "AuthorInfo.java",
    "ThemeInfo.java",
    "LastChapter.java",
    "ChapterInfo.java",
    "ChapterListResult.java",
    "SearchComic.java",
    "SearchResult.java",
    "ComicSummary.java",
    "ComicsListResult.java",
    "NewestItem.java",
    "NewestResult.java",
    "RecommendResult.java",
    "Recommendation.java",
    "RecommendComic.java",
    "RankResult.java",
    "ListItem.java",
)
_JAVA_TYPES = {
    "class_declaration",
    "enum_declaration",
    "interface_declaration",
    "record_declaration",
}
_JAVA_MEMBERS = {"field_declaration", "enum_constant", "static_initializer"}
_GENERATED_METHODS = {"copy", "equals", "hashCode", "serializer", "toString", "write$Self"}
_PUBLIC_ONLY_METHOD_MARKERS = ("auth", "collect", "comment", "login")
_JADX_NOISE_LINE = re.compile(
    r"^\s*(?:Intrinsics\.(?:checkNotNullExpressionValue|checkNotNullParameter)|"
    r"MagicApiIntrinsics\.voidMagicApiCall)\([^;]*;\s*$",
    re.MULTILINE,
)
_STRING_LITERAL = re.compile(r'"(?:\\.|[^"\\])*"')
_HTTP_SOURCE_DECLARATION = re.compile(
    r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\s+extends\s+([A-Za-z0-9_.$]+)"
    r"(?:\s+implements\s+([^\{]+))?\s*\{"
)
_DETAIL_API_ENVELOPE = re.compile(
    r"ApiResponse\.class[\s\S]{0,300}?"
    r"Reflection\.typeOf\((?:Comic)?DetailResult\.class\)"
)
_DTO_SERIALIZED_NAME = re.compile(
    r'^\s*//\s*([A-Za-z_][A-Za-z0-9_]*)\s*->\s*"([^"\\]+)"\s*$',
    re.MULTILINE,
)
_MAX_DTO_DEPENDENCY_FILES = 96


@dataclass(frozen=True)
class DecompiledDtoField:
    name: str
    serialized_name: str
    java_type: str


@dataclass(frozen=True)
class DecompiledDtoShape:
    name: str
    fields: tuple[DecompiledDtoField, ...]

    def render(self) -> str:
        fields = []
        for field in self.fields:
            label = field.name
            if field.serialized_name != field.name:
                label += f" (json {field.serialized_name})"
            fields.append(f"{label}: {field.java_type}")
        return f"{self.name} {{ {', '.join(fields)} }}"


def decompiled_dto_shapes(files: Iterable[SourceFile]) -> tuple[DecompiledDtoShape, ...]:
    """Recover concise DTO field types without asking the provider to infer Java syntax."""
    shapes: list[DecompiledDtoShape] = []
    for source in files:
        path = f"/{source.path}"
        if not source.path.endswith(".java") or (
            "/api/dto/" not in path and "C2A compacted JADX DTO" not in source.content
        ):
            continue
        raw = source.content.encode("utf-8")
        root = get_parser("java").parse(raw).root_node
        declaration = next(
            (node for node in root.named_children if node.type == "class_declaration"),
            None,
        )
        if declaration is None:
            continue
        identifier = declaration.child_by_field_name("name")
        body = declaration.child_by_field_name("body")
        if identifier is None or body is None:
            continue
        serialized_names = dict(_DTO_SERIALIZED_NAME.findall(source.content))
        fields: list[DecompiledDtoField] = []
        for member in body.named_children:
            if member.type != "field_declaration":
                continue
            modifiers = next(
                (child for child in member.named_children if child.type == "modifiers"),
                None,
            )
            if modifiers is not None and "static" in _node_text(raw, modifiers).split():
                continue
            type_node = member.child_by_field_name("type")
            if type_node is None:
                continue
            java_type = " ".join(_node_text(raw, type_node).split())
            for declarator in member.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name_node = declarator.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _node_text(raw, name_node)
                serialized_name = serialized_names.get(name)
                if (
                    serialized_name is None
                    and java_type in {"boolean", "Boolean"}
                    and re.fullmatch(r"is[A-Z][A-Za-z0-9]*", name)
                ):
                    serialized_name = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
                fields.append(
                    DecompiledDtoField(
                        name=name,
                        serialized_name=serialized_name or name,
                        java_type=java_type,
                    )
                )
        if fields:
            shapes.append(
                DecompiledDtoShape(
                    name=_node_text(raw, identifier),
                    fields=tuple(fields),
                )
            )
    return tuple(shapes)


def decompiled_detail_uses_api_envelope(files: Iterable[SourceFile]) -> bool:
    """Return whether JADX proves that manga details decode through ApiResponse.results."""
    return any(_DETAIL_API_ENVELOPE.search(source.content) is not None for source in files)


def decompiled_rank_list_wraps_comic(files: Iterable[SourceFile]) -> bool:
    """Return whether JADX proves that each rank list item wraps a comic field."""
    content = "\n".join(source.content for source in files)
    rank_list = re.search(
        r"\bclass\s+RankResult\b[\s\S]{0,2000}?\bList<ListItem>\s+list\b",
        content,
    )
    item_comic = re.search(
        r"\bclass\s+ListItem\b[\s\S]{0,1200}?\b[A-Za-z_]\w*\s+comic\b",
        content,
    )
    return rank_list is not None and item_comic is not None


def decompiled_dynamic_filter_endpoint(files: Iterable[SourceFile]) -> str | None:
    """Recover the dedicated dynamic-theme endpoint from compacted JADX behavior."""
    for source in files:
        endpoint = re.search(
            r"\btagList\s*\([^)]*\)\s*\{[\s\S]{0,800}?"
            r'return\s+getApiUrl\(\)\s*\+\s*"([^"]+)"',
            source.content,
        )
        if endpoint is not None:
            return endpoint.group(1)
    return None


@dataclass(frozen=True)
class DecompiledManifest:
    package: str
    version_text: str
    application_label: str
    metadata: dict[str, str]

    @classmethod
    def from_content(cls, content: str) -> DecompiledManifest:
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as exc:
            raise InputError(f"unable to parse decompiled AndroidManifest.xml: {exc}") from exc
        return cls._from_root(root)

    @classmethod
    def from_path(cls, path: Path) -> DecompiledManifest:
        try:
            content = path.read_text(encoding="utf-8")
            return cls.from_content(content)
        except (OSError, UnicodeDecodeError) as exc:
            raise InputError(f"unable to parse decompiled AndroidManifest.xml: {exc}") from exc

    @classmethod
    def _from_root(cls, root: ElementTree.Element) -> DecompiledManifest:
        android_name = f"{{{_ANDROID_XML_NAMESPACE}}}name"
        android_value = f"{{{_ANDROID_XML_NAMESPACE}}}value"
        metadata = {
            item.get(android_name, ""): item.get(android_value, "")
            for item in root.findall("./application/meta-data")
        }
        application = root.find("./application")
        android_label = f"{{{_ANDROID_XML_NAMESPACE}}}label"
        android_version = f"{{{_ANDROID_XML_NAMESPACE}}}versionCode"
        return cls(
            package=root.get("package", ""),
            version_text=root.get(android_version, "1"),
            application_label=(
                application.get(android_label, "") if application is not None else ""
            ),
            metadata=metadata,
        )

    @property
    def main_class_name(self) -> str:
        value = self.metadata.get("tachiyomi.extension.class", "").split(",", 1)[0].strip()
        if value:
            return value.rsplit(".", 1)[-1]
        raise InputError("APK manifest does not declare tachiyomi.extension.class")


@dataclass(frozen=True)
class DecompiledInputInspection:
    manifest: DecompiledManifest
    main_file: SourceFile
    main_class: str
    parents: tuple[str, ...]
    java: str
    method_names: tuple[str, ...]
    header_names: tuple[str, ...]

    @classmethod
    def from_files(
        cls,
        files: list[SourceFile],
        *,
        manifest: DecompiledManifest | None = None,
    ) -> DecompiledInputInspection:
        manifest_file = next(
            (item for item in files if item.path == "resources/AndroidManifest.xml"),
            None,
        )
        if manifest_file is None:
            raise InputError("decompiled APK manifest was not collected")
        manifest = manifest or DecompiledManifest.from_content(manifest_file.content)
        expected_main = manifest.main_class_name
        java_files = [item for item in files if item.path.endswith(".java")]
        declarations = [
            (item, _http_source_declaration(item.content))
            for item in java_files
            if Path(item.path).stem == expected_main
        ]
        main = next(((item, fact) for item, fact in declarations if fact is not None), None)
        if main is None:
            main = next(
                (
                    (item, fact)
                    for item in java_files
                    if (fact := _http_source_declaration(item.content)) is not None
                ),
                None,
            )
        if main is None:
            raise UnsupportedSourceError("decompiled APK contains no standalone HttpSource class")
        main_file, declaration = main
        assert declaration is not None
        main_class, parents = declaration
        java = "\n\n".join(item.content for item in java_files)
        return cls(
            manifest=manifest,
            main_file=main_file,
            main_class=main_class,
            parents=parents,
            java=java,
            method_names=_method_names(main_file.content),
            header_names=_header_names(java),
        )


def decompiled_source_paths(
    root: Path,
    *,
    manifest: DecompiledManifest | None = None,
) -> list[Path]:
    manifest_path = root / "resources" / "AndroidManifest.xml"
    manifest = manifest or DecompiledManifest.from_path(manifest_path)
    class_name = manifest.main_class_name
    candidates = sorted((root / "sources").rglob(f"{class_name}.java"))
    main = None
    for path in candidates:
        if path.is_symlink() or not path.is_file():
            continue
        declaration = _http_source_declaration(path.read_text(encoding="utf-8", errors="replace"))
        if declaration is not None and declaration[0] == class_name:
            main = path
            break
    if main is None:
        raise InputError(f"unable to find decompiled HttpSource class {class_name}.java")

    extension_root = main.parent
    all_java = [
        path
        for path in extension_root.rglob("*.java")
        if path.is_file()
        and not path.is_symlink()
        and "$$serializer" not in path.name
        and path.name != "R.java"
    ]
    by_name: dict[str, list[Path]] = {}
    for path in all_java:
        by_name.setdefault(path.name, []).append(path)

    selected = [manifest_path, main]
    for name in _HELPER_PRIORITY:
        selected.extend(sorted(by_name.get(name, [])))
    main_text = main.read_text(encoding="utf-8", errors="replace")
    imports = re.findall(
        r"^\s*import\s+eu\.kanade\.tachiyomi\.extension\."
        r"[A-Za-z0-9_.$]+\.([A-Za-z0-9_$]+);",
        main_text,
        re.MULTILINE,
    )
    for imported_class in sorted(set(imports)):
        if any(marker in imported_class for marker in _OPTIONAL_CLASS_MARKERS):
            continue
        selected.extend(sorted(by_name.get(f"{imported_class}.java", [])))
    _extend_dto_dependency_closure(selected, by_name)
    return list(dict.fromkeys(selected))


def _extend_dto_dependency_closure(
    selected: list[Path],
    by_name: dict[str, list[Path]],
) -> None:
    """Collect DTO field types transitively so response wrappers never become AI guesses."""
    known = set(selected)
    pending = [path for path in selected if "/api/dto/" in f"/{path.as_posix()}"]
    added = 0
    while pending:
        path = pending.pop(0)
        content = path.read_text(encoding="utf-8", errors="replace")
        raw = content.encode("utf-8")
        root = get_parser("java").parse(raw).root_node
        declaration = next(
            (node for node in root.named_children if node.type == "class_declaration"),
            None,
        )
        body = declaration.child_by_field_name("body") if declaration is not None else None
        if body is None:
            continue
        referenced: set[str] = set()
        for member in body.named_children:
            if member.type != "field_declaration":
                continue
            modifiers = next(
                (child for child in member.named_children if child.type == "modifiers"),
                None,
            )
            if modifiers is not None and "static" in _node_text(raw, modifiers).split():
                continue
            type_node = member.child_by_field_name("type")
            if type_node is None:
                continue
            referenced.update(re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", _node_text(raw, type_node)))
        for type_name in sorted(referenced):
            if any(marker in type_name for marker in _OPTIONAL_CLASS_MARKERS):
                continue
            candidates = [
                candidate
                for candidate in by_name.get(f"{type_name}.java", [])
                if "/api/dto/" in f"/{candidate.as_posix()}"
            ]
            for candidate in sorted(candidates):
                if candidate in known:
                    continue
                added += 1
                if added > _MAX_DTO_DEPENDENCY_FILES:
                    raise InputError(
                        "decompiled APK DTO dependency closure exceeds "
                        f"{_MAX_DTO_DEPENDENCY_FILES} files"
                    )
                known.add(candidate)
                selected.append(candidate)
                pending.append(candidate)


def normalize_decompiled_java(content: str, path: PurePath) -> str:
    content = re.sub(r"^\s*@Metadata\([^\n]*\)\s*$", "", content, flags=re.MULTILINE)
    content = re.sub(r"^\s*/\* JADX INFO:.*?\*/\s*$", "", content, flags=re.MULTILINE)
    content = content.strip() + "\n"
    if "/api/dto/" in f"/{path.as_posix()}":
        if "// C2A compacted JADX DTO:" in content:
            return content
        return _compact_dto(content)
    return content


def project_java_behavior(
    content: str,
    *,
    main: bool,
    public_only: bool,
    excluded_methods: frozenset[str] = frozenset(),
) -> str:
    raw = content.encode("utf-8")
    root = get_parser("java").parse(raw).root_node
    declaration = next((node for node in root.named_children if node.type in _JAVA_TYPES), None)
    if declaration is None:
        return content
    body = declaration.child_by_field_name("body")
    if body is None:
        return content

    header = raw[declaration.start_byte : body.start_byte].decode("utf-8", errors="replace")
    members: list[str] = []
    literal_sources: list[str] = []
    for node in body.named_children:
        if node.type in _JAVA_MEMBERS:
            members.append(_node_text(raw, node))
            continue
        if node.type == "constructor_declaration":
            if main:
                members.append(_node_text(raw, node))
            else:
                literal_sources.append(_node_text(raw, node))
            continue
        if node.type == "method_declaration":
            name = _method_name(node)
            if _generated_method(name) or name in excluded_methods:
                continue
            if (
                main
                and public_only
                and any(marker in name.lower() for marker in _PUBLIC_ONLY_METHOD_MARKERS)
            ):
                continue
            members.append(_node_text(raw, node))
            continue
        if main and node.type in _JAVA_TYPES and "WhenMappings" in _node_text(raw, node)[:200]:
            members.append(_node_text(raw, node))
        elif node.type in _JAVA_TYPES:
            literal_sources.append(_node_text(raw, node))

    result = "\n".join([header.strip() + " {", *members, "}"])
    result = _JADX_NOISE_LINE.sub("", result)
    result = re.sub(r"\n\s*\n+", "\n", result).strip() + "\n"
    missing_literals: list[str] = []
    for literal in _STRING_LITERAL.findall("\n".join(literal_sources)):
        if len(literal) > 242 or literal in result or literal in missing_literals:
            continue
        missing_literals.append(literal)
        if len(missing_literals) >= 80:
            break
    if missing_literals:
        result = result.rstrip()[:-1].rstrip()
        result += "\n// Source string literals omitted from method slices:\n"
        result += "// " + ", ".join(missing_literals) + "\n}\n"
    return result if len(result) < len(content) else content


def _http_source_declaration(content: str) -> tuple[str, tuple[str, ...]] | None:
    match = _HTTP_SOURCE_DECLARATION.search(content)
    if match is None or match.group(2).rsplit(".", 1)[-1] != "HttpSource":
        return None
    parents = ["HttpSource"]
    if match.group(3):
        parents.extend(
            item.strip().rsplit(".", 1)[-1] for item in match.group(3).split(",") if item.strip()
        )
    return match.group(1), tuple(parents)


def _method_names(content: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            set(
                re.findall(
                    r"^\s*(?:public|protected)\s+(?:static\s+)?(?:final\s+)?"
                    r"[A-Za-z0-9_.$<>?, \[\]]+\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\(",
                    content,
                    re.MULTILINE,
                )
            )
        )
    )


def _header_names(java: str) -> tuple[str, ...]:
    names = set(
        re.findall(
            r"\.(?:add|set|header|addHeader|setHeader)\(\s*\"([^\"]+)\"\s*,",
            java,
        )
    )
    blocks = re.findall(r"Headers\.Companion\.of\(new String\[\]\s*\{([^}]+)\}\)", java)
    for block in blocks:
        values = re.findall(r'"([^\"]+)"', block)
        names.update(values[::2])
    return tuple(sorted(names))


def _generated_method(name: str, *, dto: bool = False) -> bool:
    return (
        name in _GENERATED_METHODS
        or name.startswith(("component", "copy$"))
        or name.endswith("$annotations")
        or (
            dto
            and (name.startswith(("get", "set", "is")) or re.fullmatch(r"m\d+.*", name) is not None)
        )
    )


def _java_brace_block(content: str, start: int) -> str:
    opening = content.find("{", start)
    if opening < 0:
        return ""
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(content)):
        char = content[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return content[start : index + 1].strip()
    return ""


def _compact_dto(content: str) -> str:
    package = _match_line(r"^package\s+[^;]+;", content)
    declaration = _match_line(
        r"^(?:public\s+)?(?:final\s+)?(?:/\*\s*data\s*\*/\s*)?class\s+[^\{]+",
        content,
    )
    if not declaration:
        return content
    fields = re.findall(
        r"^\s*(?:(?:private|public|protected)\s+)(?:static\s+)?(?:final\s+)?"
        r"[^(){};\n]+(?:\s*=\s*[^;\n]+)?;\s*$",
        content,
        re.MULTILINE,
    )
    fields = [field.strip() for field in fields if "$$serializer" not in field]
    mappings: list[tuple[str, str]] = []
    for match in re.finditer(
        r'@SerialName\("([^\"]+)"\)[\s\S]{0,240}?\bget([A-Za-z0-9_]+)\$annotations\s*\(',
        content,
    ):
        field_name = match.group(2)
        mappings.append((field_name[:1].lower() + field_name[1:], match.group(1)))
    method_pattern = re.compile(
        r"^\s*(?:public|protected)\s+(?:static\s+)?(?:final\s+)?"
        r"[A-Za-z0-9_.$<>?, \[\]]+\s+([A-Za-z_][A-Za-z0-9_$-]*)\s*"
        r"\([^;{}\n]*\)(?:\s+throws\s+[^\{\n]+)?\s*\{",
        re.MULTILINE,
    )
    methods: list[str] = []
    covered_until = -1
    for match in method_pattern.finditer(content):
        if match.start() < covered_until or _generated_method(match.group(1), dto=True):
            continue
        block = _java_brace_block(content, match.start())
        if block:
            methods.append(block)
            covered_until = match.start() + len(block)
    lines = [
        package,
        "",
        "// C2A compacted JADX DTO: generated constructors and value methods removed.",
        declaration.strip() + " {",
    ]
    if mappings:
        lines.append("    // Serialized field names:")
        lines.extend(
            f'    // {field_name} -> "{serialized_name}"'
            for field_name, serialized_name in dict.fromkeys(mappings)
        )
    if fields:
        lines.append("    // Fields:")
        lines.extend(f"    {field}" for field in dict.fromkeys(fields))
    if methods:
        lines.append("    // Source-specific behavior:")
        lines.extend("\n".join(f"    {line}" for line in method.splitlines()) for method in methods)
    lines.append("}")
    return "\n".join(lines).strip() + "\n"


def _match_line(pattern: str, content: str) -> str:
    match = re.search(pattern, content, re.MULTILINE)
    return match.group(0).strip() if match else ""


def _node_text(raw: bytes, node: Node) -> str:
    return raw[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _method_name(node: Node) -> str:
    name = node.child_by_field_name("name")
    return name.text.decode("utf-8", errors="replace") if name is not None else ""
