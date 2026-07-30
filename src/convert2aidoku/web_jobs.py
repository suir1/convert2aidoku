from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import ReasoningEffort, load_ai_settings
from .conversion_completion import ConversionOutcome
from .converter import convert_source
from .errors import InputError

WebJobStatus = Literal[
    "queued",
    "running",
    "verified",
    "build_only",
    "blocked",
    "failed",
]
TERMINAL_WEB_JOB_STATUSES = frozenset({"verified", "build_only", "blocked", "failed"})


class WebConversionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_ref: str = Field(min_length=1, max_length=4_096)
    output: str = Field(min_length=1, max_length=4_096)
    base_url: str = Field(min_length=1, max_length=2_048)
    model: str = Field(min_length=1, max_length=256)
    query: str | None = Field(default=None, max_length=256)
    max_repairs: int | None = Field(default=None, ge=0, le=8)
    generation_reasoning: ReasoningEffort | None = None
    repair_reasoning: ReasoningEffort | None = None
    live: bool = True
    force: bool = False
    resume: bool = False
    proxy: str | None = Field(default=None, max_length=2_048)
    consent: Literal[True]


class WebJobSnapshot(BaseModel):
    id: str
    status: WebJobStatus
    message: str
    logs: list[str]
    version: int
    created_at: float
    updated_at: float
    input_ref: str
    output: str
    ai_rounds: int = 0
    total_tokens: int = 0
    error: str | None = None
    artifacts: dict[str, str] = Field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_WEB_JOB_STATUSES


@dataclass
class _WebJob:
    id: str
    request: WebConversionRequest
    status: WebJobStatus = "queued"
    message: str = "等待转换任务"
    logs: list[str] = field(default_factory=list)
    version: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    outcome: ConversionOutcome | None = None
    error: str | None = None

    def snapshot(self) -> WebJobSnapshot:
        report = self.outcome.report if self.outcome is not None else None
        rounds = report.ai_rounds if report is not None else []
        total_tokens = sum(
            item.usage.total_tokens or 0 for item in rounds if item.usage is not None
        )
        artifacts: dict[str, str] = {}
        if self.outcome is not None:
            output = self.outcome.output
            candidates = {
                "package": output / "package.aix",
                "report_md": output / "report.md",
                "report_json": output / "report.json",
            }
            artifacts = {
                name: f"/api/jobs/{self.id}/artifacts/{name}"
                for name, path in candidates.items()
                if path.is_file()
            }
        return WebJobSnapshot(
            id=self.id,
            status=self.status,
            message=self.message,
            logs=list(self.logs),
            version=self.version,
            created_at=self.created_at,
            updated_at=self.updated_at,
            input_ref=self.request.input_ref,
            output=self.request.output,
            ai_rounds=len(rounds),
            total_tokens=total_tokens,
            error=self.error,
            artifacts=artifacts,
        )


class WebJobManager:
    """Run one conversion at a time and expose bounded, credential-free job snapshots."""

    def __init__(self, *, working_directory: Path, workers: int = 1) -> None:
        self.working_directory = working_directory.resolve()
        self._jobs: dict[str, _WebJob] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="c2a-web")

    def submit(self, request: WebConversionRequest) -> WebJobSnapshot:
        job_id = uuid.uuid4().hex
        job = _WebJob(id=job_id, request=request)
        with self._lock:
            self._jobs[job_id] = job
        self._executor.submit(self._run, job_id)
        return self.snapshot(job_id)

    def resume(self, job_id: str) -> WebJobSnapshot:
        with self._lock:
            previous = self._job(job_id)
            if previous.status not in {"failed", "blocked"}:
                raise InputError("only failed or blocked conversion jobs can be resumed")
            request = previous.request.model_copy(
                update={"resume": True, "force": False, "consent": True}
            )
        return self.submit(request)

    def snapshot(self, job_id: str) -> WebJobSnapshot:
        with self._lock:
            return self._job(job_id).snapshot()

    def list(self) -> list[WebJobSnapshot]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
            return [job.snapshot() for job in jobs]

    def artifact(self, job_id: str, name: str) -> Path:
        with self._lock:
            job = self._job(job_id)
            if job.outcome is None:
                raise KeyError(name)
            candidates = {
                "package": job.outcome.output / "package.aix",
                "report_md": job.outcome.output / "report.md",
                "report_json": job.outcome.output / "report.json",
            }
            path = candidates.get(name)
            if path is None or not path.is_file():
                raise KeyError(name)
            return path

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _job(self, job_id: str) -> _WebJob:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise KeyError(job_id) from exc

    def _update(
        self,
        job_id: str,
        *,
        status: WebJobStatus | None = None,
        message: str | None = None,
        error: str | None = None,
        outcome: ConversionOutcome | None = None,
    ) -> None:
        with self._lock:
            job = self._job(job_id)
            if status is not None:
                job.status = status
            if message is not None:
                job.message = message
                if not job.logs or job.logs[-1] != message:
                    job.logs.append(message)
                    job.logs = job.logs[-80:]
            if error is not None:
                job.error = error
            if outcome is not None:
                job.outcome = outcome
            job.version += 1
            job.updated_at = time.time()

    def _run(self, job_id: str) -> None:
        with self._lock:
            request = self._job(job_id).request
        self._update(job_id, status="running", message="准备转换环境")
        try:
            settings = load_ai_settings(
                base_url=request.base_url,
                model=request.model,
                max_repair_rounds=request.max_repairs,
                generation_reasoning_effort=request.generation_reasoning,
                repair_reasoning_effort=request.repair_reasoning,
            )
            output = Path(request.output).expanduser()
            if not output.is_absolute():
                output = self.working_directory / output
            outcome = convert_source(
                request.input_ref,
                output=output.resolve(),
                settings=settings,
                query=request.query or None,
                live=request.live,
                force=request.force,
                proxy=request.proxy or None,
                resume=request.resume,
                progress=lambda message: self._update(job_id, message=message),
            )
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            self._update(
                job_id,
                status="failed",
                message="转换未完成",
                error=message[-4_000:],
            )
            return
        status: WebJobStatus = outcome.report.status.value
        self._update(
            job_id,
            status=status,
            message=f"转换结束：{status}",
            outcome=outcome,
        )
