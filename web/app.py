"""FastAPI entry point for the public Seismascope demo."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections import deque
import json
import logging
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import time
from typing import Any
from urllib.parse import urlparse
from uuid import UUID
from uuid import uuid4

from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from google.adk.cli.fast_api import get_fast_api_app
import uvicorn


logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

AGENT_DIR = REPO_ROOT / "quake_agent"
INDEX_PATH = Path(__file__).resolve().parent / "static" / "index.html"
FAVICON_PATH = Path(__file__).resolve().parent / "static" / "favicon.svg"
APP_NAME = "quake_agent"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
DEFAULT_RATE_LIMIT = 30
DEFAULT_RATE_WINDOW_SECONDS = 600
MAX_PROMPT_CHARACTERS = 2_000
MAX_REQUEST_BYTES = 16 * 1024

_ARTIFACT_PATH = re.compile(
    r"^/apps/quake_agent/users/([^/]+)/sessions/([^/]+)/artifacts/"
    r"(.+)/versions/(\d+)$"
)


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _api_base_url() -> str:
    value = os.getenv("QUAKE_AGENT_API_BASE_URL", "").strip().rstrip("/")
    if not value:
        return ""
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(
            "QUAKE_AGENT_API_BASE_URL must be an absolute HTTP(S) URL without "
            "a query or fragment."
        )
    return value


def _allowed_origins() -> list[str] | None:
    raw = os.getenv("QUAKE_AGENT_ALLOWED_ORIGINS", "")
    origins = [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]
    return origins or None


def _is_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        addresses = [item.strip() for item in forwarded.split(",") if item.strip()]
        if len(addresses) >= 2:
            return addresses[-2]
        if addresses:
            return addresses[0]
    if request.client:
        return request.client.host
    return "unknown"


def _validate_run_payload(
    body: bytes,
    *,
    expected_streaming: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    if len(body) > MAX_REQUEST_BYTES:
        return None, "Request body is too large."
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "Request body must be valid JSON."
    if not isinstance(payload, dict):
        return None, "Request body must be a JSON object."

    allowed_keys = {"appName", "userId", "sessionId", "newMessage", "streaming"}
    if set(payload) - allowed_keys:
        return None, "Request contains unsupported fields."
    if payload.get("appName") != APP_NAME:
        return None, f"appName must be {APP_NAME!r}."
    if not _is_uuid(payload.get("userId")) or not _is_uuid(
        payload.get("sessionId")
    ):
        return None, "userId and sessionId must be UUIDs."
    streaming = payload.get("streaming", False)
    if not isinstance(streaming, bool):
        return None, "streaming must be a boolean."
    if streaming is not expected_streaming:
        if expected_streaming:
            return None, "streaming must be true for /run_sse."
        return None, "Streaming requests must use /run_sse."

    message = payload.get("newMessage")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None, "newMessage must contain a user message."
    if set(message) - {"role", "parts"}:
        return None, "newMessage contains unsupported fields."
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts:
        return None, "newMessage.parts must contain text."

    text_parts: list[str] = []
    for part in parts:
        if not isinstance(part, dict) or set(part) != {"text"}:
            return None, "Only text message parts are supported."
        text = part.get("text")
        if not isinstance(text, str):
            return None, "Message text must be a string."
        text_parts.append(text)
    prompt = "".join(text_parts).strip()
    if not prompt:
        return None, "Message text cannot be empty."
    if len(prompt) > MAX_PROMPT_CHARACTERS:
        return None, (
            f"Message text cannot exceed {MAX_PROMPT_CHARACTERS:,} characters."
        )
    return payload, None


def _artifact_request(path: str) -> bool:
    match = _ARTIFACT_PATH.fullmatch(path)
    if not match:
        return False
    user_id, session_id, artifact_name, _ = match.groups()
    artifact_path = PurePosixPath(artifact_name)
    return (
        _is_uuid(user_id)
        and _is_uuid(session_id)
        and not artifact_path.is_absolute()
        and ".." not in artifact_path.parts
        and artifact_path.suffix.lower() == ".png"
    )


def _split_sse_frame(buffer: bytes) -> tuple[bytes, bytes] | None:
    """Remove one complete SSE frame from a byte buffer, if available."""
    boundaries = [
        (index, delimiter)
        for delimiter in (b"\n\n", b"\r\n\r\n")
        if (index := buffer.find(delimiter)) >= 0
    ]
    if not boundaries:
        return None
    index, delimiter = min(boundaries, key=lambda item: item[0])
    end = index + len(delimiter)
    return buffer[:end], buffer[end:]


def _sanitize_sse_frame(frame: bytes) -> tuple[bytes, dict[str, Any] | None]:
    """Replace ADK's internal streaming-error payload with a public message."""
    try:
        lines = frame.decode("utf-8").splitlines()
        data = "\n".join(
            line[5:].removeprefix(" ")
            for line in lines
            if line.startswith("data:")
        )
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return frame, None

    if not isinstance(payload, dict) or "error" not in payload:
        return frame, None

    error_id = uuid4().hex[:12]
    public_payload = {
        "error": (
            "The server could not complete this request. "
            f"Error reference: {error_id}."
        )
    }
    sanitized = f"data: {json.dumps(public_payload, separators=(',', ':'))}\n\n"
    return sanitized.encode("utf-8"), {"error_id": error_id, "payload": payload}


class WindowRateLimiter:
    """Small in-process limiter suitable for a single-instance public demo."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._requests: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        async with self._lock:
            timestamps = self._requests[key]
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= self.limit:
                return False
            timestamps.append(now)
            return True


class ActiveSessions:
    """Prevent simultaneous turns from mutating the same ADK session."""

    def __init__(self) -> None:
        self._session_ids: set[str] = set()
        self._lock = asyncio.Lock()

    async def acquire(self, session_id: str) -> bool:
        async with self._lock:
            if session_id in self._session_ids:
                return False
            self._session_ids.add(session_id)
            return True

    async def release(self, session_id: str) -> None:
        async with self._lock:
            self._session_ids.discard(session_id)


def create_app() -> FastAPI:
    """Create the public app around ADK's production-safe API server."""
    api_base_url = _api_base_url()
    rate_limit = _env_int(
        "QUAKE_AGENT_RATE_LIMIT",
        DEFAULT_RATE_LIMIT,
        minimum=1,
        maximum=10_000,
    )
    rate_window = _env_int(
        "QUAKE_AGENT_RATE_WINDOW_SECONDS",
        DEFAULT_RATE_WINDOW_SECONDS,
        minimum=1,
        maximum=86_400,
    )
    limiter = WindowRateLimiter(rate_limit, rate_window)
    active_sessions = ActiveSessions()

    app = get_fast_api_app(
        agents_dir=str(AGENT_DIR),
        session_service_uri="memory://",
        artifact_service_uri="memory://",
        use_local_storage=False,
        allow_origins=_allowed_origins(),
        web=False,
        auto_create_session=True,
    )

    @app.middleware("http")
    async def public_surface(request: Request, call_next):
        path = request.url.path
        method = request.method.upper()
        public_get = path in {
            "/",
            "/favicon.svg",
            "/health",
            "/runtime-config.json",
        }
        artifact_get = method == "GET" and _artifact_request(path)
        if method == "OPTIONS":
            return await call_next(request)
        public_run = method == "POST" and path in {"/run", "/run_sse"}
        if not ((method == "GET" and public_get) or artifact_get or public_run):
            return JSONResponse({"detail": "Not found."}, status_code=404)

        session_id: str | None = None
        if public_run:
            content_type = request.headers.get("content-type", "")
            if not content_type.lower().startswith("application/json"):
                return JSONResponse(
                    {"detail": "Content-Type must be application/json."},
                    status_code=415,
                )
            body = await request.body()
            payload, validation_error = _validate_run_payload(
                body,
                expected_streaming=path == "/run_sse",
            )
            if validation_error or payload is None:
                return JSONResponse(
                    {"detail": validation_error or "Invalid request."},
                    status_code=400,
                )
            if not await limiter.allow(_client_ip(request)):
                return JSONResponse(
                    {
                        "detail": (
                            "Too many prompts. Please wait before trying again."
                        )
                    },
                    status_code=429,
                    headers={"Retry-After": str(rate_window)},
                )
            session_id = payload["sessionId"]
            if not await active_sessions.acquire(session_id):
                return JSONResponse(
                    {"detail": "This conversation is already processing a prompt."},
                    status_code=409,
                )

        try:
            response = await call_next(request)
        except Exception:
            error_id = uuid4().hex[:12]
            logger.exception(
                "Unhandled public request error %s for %s %s",
                error_id,
                method,
                path,
            )
            response = JSONResponse(
                {
                    "detail": (
                        "The server could not complete this request. "
                        f"Error reference: {error_id}."
                    )
                },
                status_code=500,
            )
            if session_id is not None:
                await active_sessions.release(session_id)
                session_id = None

        if session_id is not None:
            body_iterator = response.body_iterator

            if path == "/run_sse":

                async def protected_stream():
                    buffer = b""
                    try:
                        async for chunk in body_iterator:
                            buffer += (
                                chunk.encode("utf-8")
                                if isinstance(chunk, str)
                                else bytes(chunk)
                            )
                            while split := _split_sse_frame(buffer):
                                frame, buffer = split
                                sanitized, error = _sanitize_sse_frame(frame)
                                if error is not None:
                                    logger.error(
                                        "ADK streaming error %s: %r",
                                        error["error_id"],
                                        error["payload"],
                                    )
                                yield sanitized
                        if buffer:
                            sanitized, error = _sanitize_sse_frame(buffer)
                            if error is not None:
                                logger.error(
                                    "ADK streaming error %s: %r",
                                    error["error_id"],
                                    error["payload"],
                                )
                            yield sanitized
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        error_id = uuid4().hex[:12]
                        logger.exception(
                            "Unhandled public stream error %s", error_id
                        )
                        public_error = {
                            "error": (
                                "The server could not complete this request. "
                                f"Error reference: {error_id}."
                            )
                        }
                        yield (
                            f"data: {json.dumps(public_error, separators=(',', ':'))}"
                            "\n\n"
                        ).encode("utf-8")
                    finally:
                        await active_sessions.release(session_id)

                response.body_iterator = protected_stream()
            else:

                async def release_after_response():
                    try:
                        async for chunk in body_iterator:
                            yield chunk
                    finally:
                        await active_sessions.release(session_id)

                response.body_iterator = release_after_response()

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if path == "/run_sse":
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Accel-Buffering"] = "no"
        if path in {"/", "/runtime-config.json"}:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(INDEX_PATH, media_type="text/html")

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(FAVICON_PATH, media_type="image/svg+xml")

    @app.get("/runtime-config.json", include_in_schema=False)
    async def runtime_config() -> JSONResponse:
        return JSONResponse(
            {
                "apiBaseUrl": api_base_url,
                "appName": APP_NAME,
                "maxPromptCharacters": MAX_PROMPT_CHARACTERS,
                "temporarySessions": True,
            }
        )

    return app


app = create_app()


def main() -> None:
    host = os.getenv("HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    port = _env_int("PORT", DEFAULT_PORT, minimum=1, maximum=65_535)
    uvicorn.run(app, host=host, port=port, proxy_headers=True)


if __name__ == "__main__":
    main()
