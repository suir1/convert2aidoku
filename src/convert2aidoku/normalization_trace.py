from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

_RULE_ID = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass
class NormalizationTrace:
    """Count files changed by deterministic rewrites behind one small interface."""

    _counts: Counter[str] = field(default_factory=Counter)
    _rule_ids: set[str] = field(default_factory=set)

    def apply(
        self,
        rule_id: str,
        content: str,
        rewrite: Callable[[str], str],
    ) -> str:
        normalized = rewrite(content)
        self.observe(rule_id, content, normalized)
        return normalized

    def observe(self, rule_id: str, before: str, after: str) -> None:
        self.hit(rule_id, changed=after != before)

    def hit(self, rule_id: str, *, changed: bool = True) -> None:
        if _RULE_ID.fullmatch(rule_id) is None:
            raise ValueError(f"invalid normalization rule id: {rule_id!r}")
        self._rule_ids.add(rule_id)
        if changed:
            self._counts[rule_id] += 1

    def merge(self, counts: Mapping[str, int]) -> None:
        for rule_id, count in counts.items():
            if count < 0:
                raise ValueError("normalization rule counts cannot be negative")
            self.hit(rule_id, changed=False)
            self._counts[rule_id] += count

    @property
    def counts(self) -> dict[str, int]:
        return dict(sorted(self._counts.items()))

    @property
    def rule_ids(self) -> frozenset[str]:
        return frozenset(self._rule_ids)
