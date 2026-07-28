from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from .analyzer import analyze_source
from .checkpoint_store import CheckpointStore
from .config import AISettings
from .errors import InputError
from .generated_source_metadata import GeneratedSourceMetadata
from .ingest import resolve_source
from .models import ConversionCheckpoint, SourceIR
from .scaffold import create_scaffold


@dataclass(frozen=True)
class _IntakeRequest:
    input_ref: str
    output: Path
    settings: AISettings
    query: str | None
    live: bool
    force: bool


@dataclass(frozen=True)
class ConversionIntake:
    output: Path
    store: CheckpointStore
    source_ir: SourceIR
    checkpoint: ConversionCheckpoint

    @property
    def project(self) -> Path:
        return self.store.project

    @classmethod
    def prepare(
        cls,
        input_ref: str,
        *,
        output: Path,
        settings: AISettings,
        query: str | None = None,
        live: bool = True,
        force: bool = False,
        resume: bool = False,
    ) -> Self:
        output = output.expanduser().absolute()
        if output.exists() and output.is_symlink():
            raise InputError(f"refusing to replace a symbolic-link output: {output}")
        request = _IntakeRequest(input_ref, output, settings, query, live, force)
        workspace = _workspace_path(output)
        if resume:
            return cls._resume(request, workspace)
        return cls._fresh(request, workspace)

    @classmethod
    def _fresh(
        cls,
        request: _IntakeRequest,
        workspace: Path,
    ) -> Self:
        _prepare_output(request.output, force=request.force)
        if workspace.exists() or workspace.is_symlink():
            raise InputError(
                f"conversion workspace already exists: {workspace}; pass --resume to continue it"
            )
        workspace.mkdir()
        store = CheckpointStore(workspace)
        try:
            with resolve_source(request.input_ref) as resolved:
                source_ir = analyze_source(resolved)
                create_scaffold(store.project, source_ir, resolved)
        except BaseException:
            shutil.rmtree(workspace, ignore_errors=True)
            raise
        checkpoint = ConversionCheckpoint(
            input_ref=request.input_ref,
            output=str(request.output),
            provider_base_url=request.settings.base_url,
            model=request.settings.model,
            query=request.query,
            live=request.live,
            force=request.force,
            warnings=list(source_ir.warnings),
            unsupported_features=list(source_ir.unsupported_features),
        )
        store = CheckpointStore.initialize(
            workspace,
            source_ir=source_ir,
            checkpoint=checkpoint,
        )
        return cls(request.output, store, source_ir, checkpoint)

    @classmethod
    def _resume(
        cls,
        request: _IntakeRequest,
        workspace: Path,
    ) -> Self:
        store, checkpoint = CheckpointStore.resume(
            workspace,
            installed_output=request.output,
        )
        _check_resume_compatibility(checkpoint, request)
        if not store.project.is_dir() or store.project.is_symlink():
            raise InputError(f"resume staging project is missing or unsafe: {store.project}")
        source_ir = _refresh_source_ir(
            store.read_source_ir(),
            input_ref=request.input_ref,
            store=store,
        )
        if checkpoint.phase == "complete":
            source_ir = _bump_completed_version(store, source_ir)
        return cls(request.output, store, source_ir, checkpoint)


def _prepare_output(output: Path, *, force: bool) -> None:
    if output.exists():
        if not force:
            raise InputError(f"output already exists: {output}; pass --force to replace it")
        if output.is_symlink():
            raise InputError(f"refusing to replace a symbolic-link output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)


def _workspace_path(output: Path) -> Path:
    return output.parent / f".{output.name}.c2a-work"


def _refresh_source_ir(
    source_ir: SourceIR,
    *,
    input_ref: str,
    store: CheckpointStore,
) -> SourceIR:
    if source_ir.schema_version >= 3:
        return source_ir
    with resolve_source(input_ref) as resolved:
        refreshed = analyze_source(resolved)
    if refreshed.metadata.source_id != source_ir.metadata.source_id:
        raise InputError(
            "refreshed resume input changed source id from "
            f"{source_ir.metadata.source_id!r} to {refreshed.metadata.source_id!r}"
        )
    store.commit(source_ir=refreshed)
    return refreshed


def _bump_completed_version(store: CheckpointStore, source_ir: SourceIR) -> SourceIR:
    try:
        source = GeneratedSourceMetadata.load(store.project).with_bumped_version(
            source_ir.metadata.version
        )
        version = source.version
        source.write(store.project)
    except (OSError, ValueError) as exc:
        raise InputError(f"unable to bump installed source version: {exc}") from exc
    bumped = source_ir.model_copy(
        update={
            "metadata": source_ir.metadata.model_copy(update={"version": version}),
        }
    )
    store.commit(source_ir=bumped)
    return bumped


def _check_resume_compatibility(
    checkpoint: ConversionCheckpoint,
    request: _IntakeRequest,
) -> None:
    mismatches = []
    if checkpoint.input_ref != request.input_ref:
        mismatches.append("input")
    if checkpoint.output != str(request.output):
        mismatches.append("output")
    if checkpoint.provider_base_url.rstrip("/") != request.settings.base_url.rstrip("/"):
        mismatches.append("base URL")
    if checkpoint.model != request.settings.model:
        mismatches.append("model")
    if checkpoint.query != request.query:
        mismatches.append("query")
    if checkpoint.live != request.live:
        mismatches.append("live mode")
    if mismatches:
        raise InputError("resume options do not match the saved run: " + ", ".join(mismatches))
