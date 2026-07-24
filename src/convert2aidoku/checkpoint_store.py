from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .errors import InputError
from .models import ConversionCheckpoint, GenerationManifest, SourceIR


@dataclass(frozen=True)
class ManifestWrite:
    number: int
    manifest: GenerationManifest


@dataclass(frozen=True)
class CheckpointStore:
    workspace: Path

    @classmethod
    def initialize(
        cls,
        workspace: Path,
        *,
        source_ir: SourceIR,
        checkpoint: ConversionCheckpoint,
    ) -> CheckpointStore:
        store = cls(workspace)
        store.commit(source_ir=source_ir, checkpoint=checkpoint)
        return store

    @classmethod
    def resume(
        cls,
        workspace: Path,
        *,
        installed_output: Path,
    ) -> tuple[CheckpointStore, ConversionCheckpoint]:
        if workspace.is_symlink():
            raise InputError(f"refusing symbolic-link resume workspace: {workspace}")
        store = cls(workspace)
        if not workspace.is_dir():
            if not installed_output.is_dir():
                raise InputError(
                    f"no resumable conversion workspace for output: {installed_output}"
                )
            store._restore_installed(installed_output)
        return store, store.read_checkpoint()

    @property
    def project(self) -> Path:
        return self.workspace / "project"

    def commit(
        self,
        *,
        checkpoint: ConversionCheckpoint | None = None,
        source_ir: SourceIR | None = None,
        manifest: ManifestWrite | None = None,
    ) -> None:
        if source_ir is not None:
            self._atomic_write(
                self.workspace / "source-ir.json",
                source_ir.model_dump_json(indent=2, exclude={"license_text"}) + "\n",
            )
        if manifest is not None:
            relative = self.round_path(manifest.number)
            path = self._manifest_path(relative)
            path.parent.mkdir(exist_ok=True)
            self._atomic_write(path, manifest.manifest.model_dump_json(indent=2) + "\n")
        if checkpoint is not None:
            self._atomic_write(
                self.workspace / "checkpoint.json",
                checkpoint.model_dump_json(indent=2, exclude_none=True) + "\n",
            )

    def read_checkpoint(self) -> ConversionCheckpoint:
        path = self.workspace / "checkpoint.json"
        if not path.is_file():
            raise InputError(f"resume workspace has no checkpoint: {self.workspace}")
        try:
            return ConversionCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InputError(f"invalid resume checkpoint {path}: {exc}") from exc

    def read_source_ir(self) -> SourceIR:
        path = self.workspace / "source-ir.json"
        try:
            return SourceIR.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InputError(f"invalid saved SourceIR {path}: {exc}") from exc

    def read_manifest(self, relative: str) -> GenerationManifest:
        path = self._manifest_path(relative)
        if not path.is_file():
            raise InputError(f"saved generation manifest is missing: {path}")
        try:
            return GenerationManifest.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InputError(f"invalid saved generation manifest {path}: {exc}") from exc

    def read_round(self, number: int) -> GenerationManifest:
        return self.read_manifest(self.round_path(number))

    def publish_audit(self, project: Path) -> list[str]:
        destination = project / ".c2a"
        if destination.is_symlink():
            raise InputError(f"refusing symbolic-link conversion audit: {destination}")
        if destination.exists():
            shutil.rmtree(destination)
        manifests = self.workspace / "manifests"
        if manifests.is_symlink():
            raise InputError(f"refusing symbolic-link manifest directory: {manifests}")
        destination.mkdir()
        shutil.copy2(self.workspace / "checkpoint.json", destination / "checkpoint.json")
        shutil.copy2(self.workspace / "source-ir.json", destination / "source-ir.json")
        shutil.copytree(manifests, destination / "manifests")
        return self.audit_files(project)

    def audit_files(self, project: Path | None = None) -> list[str]:
        manifest_root = (project / ".c2a" if project is not None else self.workspace) / "manifests"
        files = [".c2a/checkpoint.json", ".c2a/source-ir.json"]
        files.extend(f".c2a/manifests/{path.name}" for path in sorted(manifest_root.glob("*.json")))
        return files

    def _restore_installed(self, output: Path) -> None:
        audit = output / ".c2a"
        if audit.is_symlink():
            raise InputError(f"refusing symbolic-link installed conversion audit: {audit}")
        checkpoint_path = audit / "checkpoint.json"
        if not checkpoint_path.is_file():
            raise InputError(f"no resumable conversion workspace for output: {output}")
        try:
            ConversionCheckpoint.model_validate_json(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InputError(f"invalid installed conversion checkpoint: {exc}") from exc

        self.workspace.mkdir()
        try:
            os.replace(output, self.project)
            installed_audit = self.project / ".c2a"
            shutil.copy2(installed_audit / "checkpoint.json", self.workspace / "checkpoint.json")
            shutil.copy2(installed_audit / "source-ir.json", self.workspace / "source-ir.json")
            shutil.copytree(installed_audit / "manifests", self.workspace / "manifests")
        except BaseException:
            if self.project.exists() and not output.exists():
                os.replace(self.project, output)
            shutil.rmtree(self.workspace, ignore_errors=True)
            raise

    @staticmethod
    def round_path(number: int) -> str:
        if number < 1:
            raise InputError(f"generation manifest round must be positive: {number}")
        return f"manifests/round-{number:02d}.json"

    def _manifest_path(self, relative: str) -> Path:
        candidate = Path(relative)
        if (
            candidate.is_absolute()
            or ".." in candidate.parts
            or candidate.parts[:1] != ("manifests",)
            or len(candidate.parts) != 2
        ):
            raise InputError(f"invalid manifest path in resume checkpoint: {relative}")
        path = self.workspace.joinpath(*candidate.parts)
        if path.is_symlink():
            raise InputError(f"refusing symbolic-link resume manifest: {path}")
        return path

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
