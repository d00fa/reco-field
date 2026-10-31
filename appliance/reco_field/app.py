from __future__ import annotations

import asyncio
import hmac
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import __version__
from .auth import CSRF_COOKIE, SESSION_COOKIE, SessionStore, Session
from .config import FieldSettings, RuntimeConfig
from .network import network_request, wifi_qr_png
from .storage import ensure_directories, require_storage
from .supervisor import MAINTENANCE_FILE, Supervisor, SupervisorError


PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stream: bool | None = None
    mode: Literal["panorama", "sources", "both"] | None = None


class WifiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ssid: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=8, max_length=64, repr=False)


class SetupWifiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ssid: str = Field(min_length=1, max_length=32)
    password: str = Field(default="", max_length=63, repr=False)

    @field_validator("ssid")
    @classmethod
    def valid_ssid(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 32 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Wi-Fi name must be 1–32 UTF-8 bytes without control characters.")
        return value

    @field_validator("password")
    @classmethod
    def valid_password(cls, value: str) -> str:
        if value and (len(value) < 8 or any(not 32 <= ord(c) <= 126 for c in value)):
            raise ValueError("Use 8–63 letters, numbers or symbols, or leave blank to keep the password.")
        return value


class BodyTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    def __init__(self, app: Any, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        total = 0

        async def limited_receive() -> dict:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    raise BodyTooLarge
            return message

        response_started = False

        async def tracked_send(message: dict) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except BodyTooLarge:
            if response_started:
                raise
            await send(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"detail":"Request body is too large."}',
                }
            )


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    expected = f"{request.url.scheme}://{request.url.netloc}"
    return origin.rstrip("/") == expected


def create_app(
    runtime: RuntimeConfig,
    auth: SessionStore | None = None,
) -> FastAPI:
    require_storage(runtime.data_dir, runtime.storage_mount, runtime.storage_uuid)
    ensure_directories(runtime.data_dir)
    auth = auth or SessionStore(runtime.session_hours)
    supervisor = Supervisor(runtime)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.supervisor = supervisor
        supervisor.device_telemetry.start()
        await asyncio.to_thread(supervisor.refresh_capabilities)
        if supervisor.settings.auto_record:
            threading.Thread(
                target=_try_auto_start, args=(supervisor,), daemon=True
            ).start()
        yield
        await asyncio.to_thread(supervisor.shutdown)

    app = FastAPI(
        title="Reco Field",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.auth = auth
    app.state.runtime = runtime
    app.state.supervisor = supervisor
    app.add_middleware(BodyLimitMiddleware, max_bytes=runtime.max_body_bytes)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                too_large = int(content_length) > runtime.max_body_bytes
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length."}, 400)
            if too_large:
                return JSONResponse({"detail": "Request body is too large."}, 413)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data: blob:; media-src 'self' blob:; "
            "connect-src 'self'; worker-src 'self' blob:; "
            "frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        return response

    @app.exception_handler(SupervisorError)
    async def supervisor_error(
        request: Request, exc: SupervisorError
    ) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Validation responses must not echo passwords or stream keys.
        return JSONResponse({"detail": [{key: error[key] for key in ("loc", "msg", "type")}
                                        for error in exc.errors()]}, 422)

    def require_session(request: Request) -> Session:
        session = auth.get_session(request.cookies.get(SESSION_COOKIE))
        if not session:
            raise HTTPException(401, "Refresh the page to reconnect.")
        return session

    def require_write(request: Request) -> Session:
        session = require_session(request)
        if MAINTENANCE_FILE.exists() and not request.url.path.endswith(("/stop", "/cancel")):
            raise HTTPException(503, "Reco Field is updating. Try again after the update finishes.")
        if not _same_origin(request):
            raise HTTPException(403, "Cross-origin writes are not allowed.")
        csrf_cookie = request.cookies.get(CSRF_COOKIE, "")
        csrf_header = request.headers.get("x-csrf-token", "")
        if (
            not csrf_cookie
            or not csrf_header
            or not hmac.compare_digest(csrf_cookie, csrf_header)
            or not hmac.compare_digest(csrf_header, session.csrf)
        ):
            raise HTTPException(403, "Invalid CSRF token.")
        return session

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/session")
    async def session_status(request: Request, response: Response) -> dict[str, Any]:
        if not _same_origin(request) or request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "Cross-origin sessions are not allowed.")
        session = auth.get_session(request.cookies.get(SESSION_COOKIE)) or auth.create()
        response.set_cookie(
            SESSION_COOKIE,
            session.token,
            max_age=runtime.session_hours * 3600,
            httponly=True,
            secure=runtime.secure_cookies,
            samesite="strict",
            path="/",
        )
        response.set_cookie(
            CSRF_COOKIE,
            session.csrf,
            max_age=runtime.session_hours * 3600,
            httponly=False,
            secure=runtime.secure_cookies,
            samesite="strict",
            path="/",
        )
        return {"csrf": session.csrf}

    @app.get("/api/status")
    async def status(_: Session = Depends(require_session)) -> dict[str, Any]:
        return supervisor.status()

    @app.get("/api/preflight")
    async def preflight(_: Session = Depends(require_session)) -> dict[str, Any]:
        return await asyncio.to_thread(supervisor.preflight)

    @app.get("/api/settings")
    async def get_settings(_: Session = Depends(require_session)) -> dict[str, Any]:
        return supervisor.public_settings()

    @app.get("/api/network")
    async def network_status(_: Session = Depends(require_session)) -> dict[str, Any]:
        return await asyncio.to_thread(network_request, runtime.network_socket, "status")

    @app.post("/api/network/scan")
    async def network_scan(_: Session = Depends(require_write)) -> dict[str, Any]:
        return await asyncio.to_thread(network_request, runtime.network_socket, "scan")

    @app.post("/api/network/connect")
    async def network_connect(payload: WifiRequest, _: Session = Depends(require_write)) -> dict[str, Any]:
        status = supervisor.status()
        if any(status[key]["state"] in {"starting", "running", "stopping"} for key in ("capture", "calibration", "snapshots", "lens")):
            raise HTTPException(409, "Stop recording and camera setup before changing Wi-Fi.")
        result = await asyncio.to_thread(network_request, runtime.network_socket, "connect",
                                        ssid=payload.ssid, password=payload.password)
        if not result.get("accepted"):
            raise HTTPException(400, result.get("error", "Wi-Fi setup is unavailable."))
        return result

    @app.get("/api/network/setup/qr.png")
    async def setup_wifi_qr(_: Session = Depends(require_session)) -> Response:
        result = await asyncio.to_thread(network_request, runtime.network_socket, "status")
        setup = result.get("setup")
        if not result.get("available") or not setup:
            raise HTTPException(503, "Camera Wi-Fi is unavailable.")
        image = await asyncio.to_thread(wifi_qr_png, setup["ssid"], setup["password"])
        return Response(image, media_type="image/png",
                        headers={"Cache-Control": "no-store", "Content-Disposition": 'inline; filename="reco-field-wifi.png"'})

    @app.post("/api/network/setup")
    async def setup_wifi(payload: SetupWifiRequest, _: Session = Depends(require_write)) -> dict[str, Any]:
        status = supervisor.status()
        if any(status[key]["state"] in {"starting", "running", "stopping"} for key in ("capture", "calibration", "snapshots", "lens")):
            raise HTTPException(409, "Stop recording and camera setup before changing Wi-Fi.")
        result = await asyncio.to_thread(network_request, runtime.network_socket, "set_setup",
                                        ssid=payload.ssid, password=payload.password)
        if not result.get("accepted"):
            raise HTTPException(400, result.get("error", "Camera Wi-Fi could not be updated."))
        return result

    @app.put("/api/settings")
    async def update_settings(
        payload: FieldSettings,
        _: Session = Depends(require_write),
    ) -> dict[str, Any]:
        merged = supervisor.merge_settings(payload)
        supervisor.save_settings(merged)
        return supervisor.public_settings()

    @app.post("/api/recordings/start")
    async def start_recording(
        payload: StartRequest,
        _: Session = Depends(require_write),
    ) -> dict[str, Any]:
        return await asyncio.to_thread(supervisor.start_recording, payload.stream, payload.mode)

    @app.post("/api/preview/start")
    async def start_preview(_: Session = Depends(require_write)) -> dict[str, Any]:
        return await asyncio.to_thread(supervisor.start_preview)

    @app.post("/api/preview/stop")
    async def stop_preview(_: Session = Depends(require_write)) -> dict[str, Any]:
        return await asyncio.to_thread(supervisor.stop_preview)

    @app.post("/api/recordings/stop")
    async def stop_recording(
        _: Session = Depends(require_write),
    ) -> dict[str, Any]:
        return await asyncio.to_thread(supervisor.stop_recording)

    @app.get("/api/recordings")
    async def recordings(_: Session = Depends(require_session)) -> list[dict[str, Any]]:
        return await asyncio.to_thread(supervisor.list_recordings)

    @app.get("/api/sessions")
    async def recording_sessions(_: Session = Depends(require_session)) -> list[dict[str, Any]]:
        return await asyncio.to_thread(supervisor.list_sessions)

    @app.post("/api/sessions/{session_id}/stitch")
    async def start_session_stitch(session_id: str, _: Session = Depends(require_write)) -> dict[str, Any]:
        return await asyncio.to_thread(supervisor.start_offline_stitch, session_id)

    @app.post("/api/sessions/{session_id}/stitch/cancel")
    async def cancel_session_stitch(session_id: str, _: Session = Depends(require_write)) -> dict[str, Any]:
        return await asyncio.to_thread(supervisor.cancel_offline_stitch, session_id)

    @app.get("/api/sessions/{session_id}/{filename}")
    async def download_session_file(session_id: str, filename: str, _: Session = Depends(require_session)) -> FileResponse:
        path = supervisor.session_file(session_id, filename)
        return FileResponse(path, media_type="video/x-matroska", filename=f"{session_id}-{filename}",
                            content_disposition_type="attachment")

    @app.get("/api/recordings/{filename}")
    async def download_recording(
        filename: str, _: Session = Depends(require_session)
    ) -> FileResponse:
        path = supervisor.recording_file(filename)
        return FileResponse(
            path,
            media_type="video/x-matroska",
            filename=path.name,
            content_disposition_type="attachment",
        )

    @app.post("/api/calibrate")
    async def start_calibration(
        _: Session = Depends(require_write),
    ) -> dict[str, Any]:
        return await asyncio.to_thread(supervisor.start_calibration)

    @app.post("/api/calibrate/stop")
    async def stop_calibration(
        _: Session = Depends(require_write),
    ) -> dict[str, Any]:
        return await asyncio.to_thread(supervisor.stop_calibration)

    @app.post("/api/calibration/import")
    async def import_calibration(
        request: Request,
        kind: str = "calibration",
        _: Session = Depends(require_write),
    ) -> dict[str, Any]:
        data = await request.body()
        if len(data) > runtime.max_body_bytes:
            raise HTTPException(413, "Calibration file is too large.")
        try:
            return await asyncio.to_thread(supervisor.import_calibration, data, kind)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/lenses/board.svg")
    async def lens_board(_: Session = Depends(require_session)) -> FileResponse:
        try:
            path = await asyncio.to_thread(supervisor.lens_calibration.board_file)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(503, str(exc)) from exc
        return FileResponse(path, media_type="image/svg+xml", filename="reco-charuco-30mm.svg")

    @app.get("/api/lenses/{side}/preview.jpg")
    async def lens_preview(side: str, _: Session = Depends(require_session)) -> FileResponse:
        try:
            path = supervisor.lens_calibration.preview_file(side)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/lenses/stop")
    async def stop_lens(_: Session = Depends(require_write)) -> dict[str, Any]:
        return await asyncio.to_thread(supervisor.stop_lens_calibration)

    @app.post("/api/lenses/{side}/{operation}")
    async def lens_action(side: str, operation: str, _: Session = Depends(require_write)) -> dict[str, Any]:
        return await asyncio.to_thread(supervisor.lens_action, side, operation)

    @app.post("/api/snapshots")
    async def capture_snapshots(
        _: Session = Depends(require_write),
    ) -> dict[str, Any]:
        return await asyncio.to_thread(supervisor.start_snapshots)

    @app.post("/api/snapshots/stop")
    async def stop_snapshots(
        _: Session = Depends(require_write),
    ) -> dict[str, Any]:
        return await asyncio.to_thread(supervisor.stop_snapshots)

    @app.get("/api/snapshots/{side}.jpg")
    async def snapshot_image(
        side: str, _: Session = Depends(require_session)
    ) -> FileResponse:
        path = supervisor.snapshot_file(side)
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/preview/{path:path}")
    async def preview_proxy(
        path: str,
        request: Request,
        _: Session = Depends(require_session),
    ) -> Response:
        if supervisor.record_state not in {"running", "starting", "stopping"}:
            raise HTTPException(503, "Preview is available while recording is active.")
        relative = urllib.parse.unquote(path)
        allowed = (".m3u8", ".ts", ".mp4", ".m4s", ".aac", ".vtt")
        if (
            not relative
            or ".." in relative
            or relative.startswith("/")
            or not relative.lower().endswith(allowed)
        ):
            raise HTTPException(404, "Preview resource not found.")
        query = []
        for key, value in request.query_params.multi_items():
            if key in {"_HLS_msn", "_HLS_part"} and value.isdigit():
                query.append((key, value))
            elif key == "_HLS_skip" and value in {"YES", "v2"}:
                query.append((key, value))
            elif key == "session" and re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                value,
            ):
                query.append((key, value))
        target = (
            runtime.relay_hls_base
            + "/"
            + urllib.parse.quote(relative, safe="/._-")
        )
        if query:
            target += "?" + urllib.parse.urlencode(query)
        try:
            body, content_type = await asyncio.to_thread(_fetch_preview, target)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise HTTPException(502, f"Preview relay unavailable: {exc}") from exc
        return Response(
            body,
            media_type=content_type,
            headers={"Cache-Control": "no-store"},
        )

    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")
    return app


def _fetch_preview(url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "Reco-Field/0.1"})
    with urllib.request.urlopen(request, timeout=8) as response:
        body = response.read(16 * 1024 * 1024 + 1)
        if len(body) > 16 * 1024 * 1024:
            raise OSError("preview object exceeds 16 MiB")
        return body, response.headers.get_content_type()


def _try_auto_start(supervisor: Supervisor) -> None:
    for attempt in range(61):
        try:
            supervisor.start_recording(supervisor.settings.auto_stream)
            return
        except SupervisorError as exc:
            if exc.status_code == 503 and MAINTENANCE_FILE.exists() and attempt < 60:
                time.sleep(0.5)
                continue
            supervisor.record_error = f"Auto-start failed: {exc}"
            return
