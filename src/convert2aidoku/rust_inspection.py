from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from tree_sitter import Node, Tree
from tree_sitter_language_pack import get_parser

_FUNCTION_CALL = re.compile(r"\b(?:self\.)?([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_ROUTE_LITERAL = re.compile(r'"(/[^"\\]*(?:\\.[^"\\]*)*)"')


@dataclass(frozen=True)
class RustFunction:
    name: str
    text: str
    node: Node
    calls: frozenset[str]
    route_literals: frozenset[str]

    @classmethod
    def from_node(cls, node: Node) -> RustFunction | None:
        identifier = node.child_by_field_name("name")
        if identifier is None:
            return None
        text = node.text.decode("utf-8", errors="replace")
        return cls(
            name=identifier.text.decode("utf-8", errors="replace"),
            text=text,
            node=node,
            calls=frozenset(_FUNCTION_CALL.findall(text)),
            route_literals=frozenset(_ROUTE_LITERAL.findall(text)),
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

    @staticmethod
    def compact_node(node: Node) -> str:
        text = node.text.decode("utf-8", errors="replace")
        text = re.sub(r"/\*[\s\S]*?\*/|//[^\r\n]*", "", text)
        return "".join(text.split())
