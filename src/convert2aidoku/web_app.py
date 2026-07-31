from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from anyio import to_thread
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from platformdirs import user_cache_dir
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .ai import OpenAICompatibleClient
from .analyzer import analyze_path
from .config import ai_config_defaults, load_ai_settings
from .conversion_assessment import assess_source_ir
from .errors import C2AError, InputError
from .toolchain import doctor
from .web_jobs import TERMINAL_WEB_JOB_STATUSES, WebConversionRequest, WebJobManager

MAX_WEB_UPLOAD_BYTES = 512 * 1024 * 1024
_RESOURCE_ROOT = Path(__file__).parent / "resources" / "web"


class WebAIProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(min_length=1, max_length=2_048)
    model: str = Field(min_length=1, max_length=256)


def _doctor_payload() -> list[dict[str, object]]:
    return [
        {
            "name": item.name,
            "available": item.available,
            "detail": (item.detail or item.path or "").splitlines()[0][:160],
        }
        for item in doctor()
    ]


def _analysis_payload(ir, *, input_ref: str, working_directory: Path) -> dict[str, object]:
    assessment = assess_source_ir(ir)
    return {
        "input_ref": input_ref,
        "suggested_output": str(
            (working_directory / "generated" / ir.metadata.source_id).resolve()
        ),
        "source": {
            "id": ir.metadata.source_id,
            "name": ir.metadata.name,
            "base_url": ir.metadata.base_url,
            "format": ir.source_format,
            "capabilities": [item.value for item in ir.capabilities],
            "filters": len(ir.filter_specs),
            "files": len(ir.files),
            "license": ir.license_name,
            "warnings": ir.warnings,
            "unsupported_features": ir.unsupported_features,
        },
        "assessment": assessment.model_dump(mode="json"),
    }


def _store_apk(upload: UploadFile, upload_root: Path) -> Path:
    filename = Path(upload.filename or "source.apk").name
    if Path(filename).suffix.casefold() != ".apk":
        raise InputError("Web uploads currently accept .apk files only")
    directory = upload_root / uuid.uuid4().hex
    directory.mkdir(parents=True, mode=0o700)
    destination = directory / filename
    total = 0
    try:
        with destination.open("xb") as output:
            while chunk := upload.file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_WEB_UPLOAD_BYTES:
                    raise InputError("uploaded APK exceeds the 512 MiB Web UI limit")
                output.write(chunk)
    except BaseException:
        destination.unlink(missing_ok=True)
        directory.rmdir()
        raise
    return destination


def _origin_is_local(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    parsed = urlparse(origin)
    return parsed.hostname in {"localhost", "127.0.0.1", "::1", request.url.hostname}


def create_web_app(
    *,
    working_directory: Path | None = None,
    allow_network: bool = False,
) -> FastAPI:
    working_directory = (working_directory or Path.cwd()).expanduser().resolve()
    csrf_token = secrets.token_urlsafe(32)
    upload_root = Path(user_cache_dir("convert2aidoku")) / "web-uploads"
    templates = Jinja2Templates(directory=_RESOURCE_ROOT / "templates")
    jobs = WebJobManager(working_directory=working_directory)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        jobs.shutdown()

    app = FastAPI(
        title="C2A Local UI",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.c2a_csrf = csrf_token
    app.state.c2a_jobs = jobs
    app.state.c2a_working_directory = working_directory
    app.state.c2a_upload_root = upload_root
    allowed_hosts = ["*"] if allow_network else ["localhost", "127.0.0.1", "[::1]", "testserver"]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.mount("/static", StaticFiles(directory=_RESOURCE_ROOT / "static"), name="static")

    @app.middleware("http")
    async def local_security_headers(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not _origin_is_local(request):
            return JSONResponse({"detail": "cross-origin request rejected"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(C2AError)
    async def c2a_error_handler(_request: Request, exc: C2AError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def require_csrf(x_c2a_csrf: str | None = Header(default=None)) -> None:
        if not x_c2a_csrf or not secrets.compare_digest(x_c2a_csrf, csrf_token):
            raise HTTPException(status_code=403, detail="invalid local session token")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "csrf_token": csrf_token,
                "doctor": _doctor_payload(),
                "ai": ai_config_defaults(),
                "api_key_configured": bool(os.getenv("C2A_API_KEY")),
                "working_directory": str(working_directory),
            },
        )

    @app.get("/api/doctor")
    def doctor_endpoint() -> dict[str, object]:
        statuses = _doctor_payload()
        return {"ready": all(bool(item["available"]) for item in statuses), "items": statuses}

    @app.post("/api/ai-check", dependencies=[Depends(require_csrf)])
    async def ai_check_endpoint(payload: WebAIProbeRequest) -> dict[str, object]:
        settings = load_ai_settings(base_url=payload.base_url, model=payload.model)

        def check() -> tuple[str, bool]:
            with OpenAICompatibleClient(settings) as client:
                result = client.check()
            return result.model, result.structured_output

        model, structured = await to_thread.run_sync(check)
        return {"ok": True, "model": model, "structured_output": structured}

    @app.post("/api/analyze", dependencies=[Depends(require_csrf)])
    async def analyze_endpoint(
        input_ref: Annotated[str, Form()] = "",
        source_file: Annotated[UploadFile | None, File()] = None,
    ) -> dict[str, object]:
        selected = input_ref.strip()
        if source_file is not None and source_file.filename:
            selected = str(await to_thread.run_sync(_store_apk, source_file, upload_root))
        if not selected:
            raise InputError("choose an APK or enter a local module/GitHub URL")
        ir = await to_thread.run_sync(analyze_path, selected)
        return _analysis_payload(ir, input_ref=selected, working_directory=working_directory)

    @app.post("/api/jobs", dependencies=[Depends(require_csrf)], status_code=202)
    def create_job(payload: WebConversionRequest) -> dict[str, object]:
        return app.state.c2a_jobs.submit(payload).model_dump(mode="json")

    @app.get("/api/jobs")
    def list_jobs() -> list[dict[str, object]]:
        return [item.model_dump(mode="json") for item in app.state.c2a_jobs.list()]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, object]:
        try:
            snapshot = app.state.c2a_jobs.snapshot(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="conversion job not found") from exc
        return snapshot.model_dump(mode="json")

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str) -> StreamingResponse:
        try:
            app.state.c2a_jobs.snapshot(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="conversion job not found") from exc

        async def stream():
            version = -1
            while True:
                snapshot = app.state.c2a_jobs.snapshot(job_id)
                if snapshot.version != version:
                    version = snapshot.version
                    yield "data: " + snapshot.model_dump_json() + "\n\n"
                if snapshot.status in TERMINAL_WEB_JOB_STATUSES:
                    break
                await asyncio.sleep(0.4)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/api/jobs/{job_id}/resume", dependencies=[Depends(require_csrf)], status_code=202)
    def resume_job(job_id: str) -> dict[str, object]:
        try:
            snapshot = app.state.c2a_jobs.resume(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="conversion job not found") from exc
        return snapshot.model_dump(mode="json")

    @app.get("/api/jobs/{job_id}/artifacts/{name}")
    def download_artifact(job_id: str, name: str) -> FileResponse:
        try:
            path = app.state.c2a_jobs.artifact(job_id, name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="artifact not found") from exc
        media_types = {
            "package": "application/octet-stream",
            "report_md": "text/markdown; charset=utf-8",
            "report_json": "application/json",
        }
        return FileResponse(path, media_type=media_types[name], filename=path.name)

    return app
