from __future__ import annotations

import json
import os
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from .models import GenerationManifest, SourceIR

_SOURCE_METADATA_PATH = Path("res/source.json")


@dataclass(frozen=True)
class GeneratedSourceMetadata:
    """Aidoku source metadata with its nested JSON shape hidden from callers."""

    _document: dict[str, Any] = field(repr=False)

    @classmethod
    def from_source_ir(cls, source_ir: SourceIR) -> Self:
        metadata = source_ir.metadata
        return cls(
            {
                "info": {
                    "id": metadata.source_id,
                    "name": metadata.name,
                    "version": metadata.version,
                    "url": metadata.base_url,
                    "contentRating": metadata.content_rating.aidoku_value,
                    "languages": [metadata.language],
                }
            }
        )

    @classmethod
    def load(cls, project: Path) -> Self:
        document = json.loads(cls.path(project).read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("source.json root must be an object")
        if not isinstance(document.get("info"), dict):
            raise ValueError("source.json info must be an object")
        return cls(document)

    @staticmethod
    def path(project: Path) -> Path:
        return project / _SOURCE_METADATA_PATH

    @classmethod
    def exists(cls, project: Path) -> bool:
        return cls.path(project).is_file()

    @property
    def _info(self) -> dict[str, Any]:
        info = self._document["info"]
        if not isinstance(info, dict):  # Only direct construction can violate this invariant.
            raise ValueError("source.json info must be an object")
        return info

    @property
    def source_id(self) -> str | None:
        value = self._info.get("id")
        return str(value) if value is not None else None

    @property
    def site_url(self) -> str | None:
        value = self._info.get("url")
        return value if isinstance(value, str) else None

    @property
    def version(self) -> int:
        try:
            return int(self._info["version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("source.json info.version must be an integer") from exc

    @property
    def minimum_app_version(self) -> str | None:
        value = self._info.get("minAppVersion")
        return value if isinstance(value, str) else None

    def with_bumped_version(self, minimum: int) -> Self:
        return self._with_info(version=max(minimum, self.version + 1))

    def with_manifest_requirements(self, manifest: GenerationManifest) -> Self:
        """Project generated Rust host imports into Aidoku compatibility metadata."""
        rust = "\n".join(item.content for item in manifest.files if item.path.endswith(".rs"))
        minimum_version: str | None = None
        if re.search(r"\bparse_(?:local_)?date(?:_with_options)?\b", rust):
            minimum_version = "0.7.1"
        if re.search(r"\b(?:timeout|set_timeout)\s*\(", rust):
            minimum_version = "0.8.3"
        return self._with_info(minAppVersion=minimum_version)

    def _with_info(self, **updates: object) -> Self:
        document = deepcopy(self._document)
        info = document["info"]
        if not isinstance(info, dict):
            raise ValueError("source.json info must be an object")
        for name, value in updates.items():
            if value is None:
                info.pop(name, None)
            else:
                info[name] = value
        return type(self)(document)

    def write(self, project: Path) -> None:
        path = self.path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
        content = json.dumps(self._document, ensure_ascii=False, indent="\t") + "\n"
        try:
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
