from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from tree_sitter import Node, Tree
from tree_sitter_language_pack import get_parser

_FUNCTION_CALL = re.compile(r"\b(?:self\.)?([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_ROUTE_LITERAL = re.compile(r'"(/[^"\\]*(?:\\.[^"\\]*)*)"')
_REQUEST_CALL_TOKENS = frozenset({"delete", "fetch", "get", "patch", "post", "put", "request"})


@dataclass(frozen=True)
class RustFunction:
    name: str
    text: str
    node: Node
    calls: frozenset[str]
    route_literals: frozenset[str]
    parameter_names: tuple[str, ...]

    @classmethod
    def from_node(cls, node: Node) -> RustFunction | None:
        identifier = node.child_by_field_name("name")
        if identifier is None:
            return None
        text = node.text.decode("utf-8", errors="replace")
        parameters = node.child_by_field_name("parameters")
        parameter_names = tuple(
            pattern.text.decode("utf-8", errors="replace")
            for parameter in (parameters.named_children if parameters is not None else ())
            if parameter.type == "parameter"
            and (pattern := parameter.child_by_field_name("pattern")) is not None
            and pattern.type == "identifier"
        )
        return cls(
            name=identifier.text.decode("utf-8", errors="replace"),
            text=text,
            node=node,
            calls=frozenset(_FUNCTION_CALL.findall(text)),
            route_literals=frozenset(_ROUTE_LITERAL.findall(text)),
            parameter_names=parameter_names,
        )


@dataclass(frozen=True)
class RustStructField:
    name: str
    type_text: str
    serialized_name: str
    node: Node
    attributes: tuple[Node, ...]


@dataclass(frozen=True)
class RustStruct:
    name: str
    text: str
    node: Node
    fields: tuple[RustStructField, ...]

    @classmethod
    def from_node(cls, node: Node) -> RustStruct | None:
        identifier = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if identifier is None or body is None:
            return None
        fields: list[RustStructField] = []
        attributes: list[Node] = []
        for child in body.named_children:
            if child.type == "attribute_item":
                attributes.append(child)
                continue
            if child.type != "field_declaration":
                attributes.clear()
                continue
            name = child.child_by_field_name("name")
            type_node = child.child_by_field_name("type")
            if name is None or type_node is None:
                attributes.clear()
                continue
            field_name = name.text.decode("utf-8", errors="replace")
            attribute_text = "\n".join(
                attribute.text.decode("utf-8", errors="replace") for attribute in attributes
            )
            rename = re.search(r'\brename\s*=\s*"([^"\\]+)"', attribute_text)
            fields.append(
                RustStructField(
                    name=field_name,
                    type_text=type_node.text.decode("utf-8", errors="replace"),
                    serialized_name=rename.group(1) if rename else field_name,
                    node=child,
                    attributes=tuple(attributes),
                )
            )
            attributes.clear()
        return cls(
            name=identifier.text.decode("utf-8", errors="replace"),
            text=node.text.decode("utf-8", errors="replace"),
            node=node,
            fields=tuple(fields),
        )


class RustInspection:
    """Parse generated Rust once and expose reusable syntax/function facts."""

    def __init__(self, contents: Iterable[str]):
        parser = get_parser("rust")
        self._trees: list[Tree] = [parser.parse(content.encode("utf-8")) for content in contents]
        functions: list[RustFunction] = []
        by_name: dict[str, list[RustFunction]] = {}
        for node in self.nodes("function_item"):
            function = RustFunction.from_node(node)
            if function is None:
                continue
            functions.append(function)
            by_name.setdefault(function.name, []).append(function)
        self.functions = tuple(functions)
        self._by_name = {name: tuple(items) for name, items in by_name.items()}
        structs = [
            item
            for node in self.nodes("struct_item")
            if (item := RustStruct.from_node(node)) is not None
        ]
        self.structs = tuple(structs)
        self._structs_by_name = {item.name: item for item in structs}

    @classmethod
    def from_content(cls, content: str) -> RustInspection:
        return cls([content])

    def nodes(self, node_type: str | None = None) -> Iterator[Node]:
        for tree in self._trees:
            stack = [tree.root_node]
            while stack:
                node = stack.pop()
                if node_type is None or node.type == node_type:
                    yield node
                stack.extend(reversed(node.children))

    def named(self, name: str) -> tuple[RustFunction, ...]:
        return self._by_name.get(name, ())

    def has_function(self, name: str) -> bool:
        return name in self._by_name

    def function_contains(self, name: str, needle: str) -> bool:
        return any(needle in function.text for function in self.named(name))

    def function_has_header(self, name: str, header: str) -> bool:
        pattern = re.compile(rf'\.header\s*\(\s*"{re.escape(header)}"', re.IGNORECASE)
        return any(pattern.search(function.text) is not None for function in self.named(name))

    def calls(self, name: str) -> set[str]:
        return {called for function in self.named(name) for called in function.calls}

    def route_literals(self, name: str) -> set[str]:
        return {route for function in self.named(name) for route in function.route_literals}

    def request_route_literals(self, name: str) -> set[str]:
        routes: set[str] = set()
        for function in self.named(name):
            stack = [function.node]
            while stack:
                node = stack.pop()
                if node.type == "string_literal":
                    match = _ROUTE_LITERAL.fullmatch(node.text.decode("utf-8", errors="replace"))
                    if match is not None and _has_request_call_ancestor(node, function.node):
                        routes.add(match.group(1))
                stack.extend(reversed(node.children))
        return routes

    def reachable_functions(self, start: str) -> set[str]:
        reachable = {start}
        pending = [start]
        while pending:
            current = pending.pop()
            for candidate in self.calls(current):
                if candidate in self._by_name and candidate not in reachable:
                    reachable.add(candidate)
                    pending.append(candidate)
        return reachable

    def struct_named(self, name: str) -> RustStruct | None:
        return self._structs_by_name.get(name)

    def struct_field_type(self, owner: str, field_name: str) -> str | None:
        field = self.struct_field(owner, field_name)
        return field.type_text if field is not None else None

    def struct_field(self, owner: str, field_name: str) -> RustStructField | None:
        struct = self.struct_named(owner)
        if struct is None:
            return None
        return next((field for field in struct.fields if field.name == field_name), None)

    @staticmethod
    def compact_node(node: Node) -> str:
        text = node.text.decode("utf-8", errors="replace")
        text = re.sub(r"/\*[\s\S]*?\*/|//[^\r\n]*", "", text)
        return "".join(text.split())


def _has_request_call_ancestor(node: Node, function: Node) -> bool:
    ancestor = node.parent
    while ancestor is not None and ancestor != function:
        if ancestor.type == "call_expression":
            target = ancestor.child_by_field_name("function")
            if target is not None:
                call_name = target.text.decode("utf-8", errors="replace").rsplit(".", 1)[-1]
                if _REQUEST_CALL_TOKENS.intersection(call_name.casefold().split("_")):
                    return True
        ancestor = ancestor.parent
    return False
