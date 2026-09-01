from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import StreamingResponse
import httpx
import pytest

from web import app as web_app


def _run_payload(
    *,
    text: str = "Map recent earthquakes",
    streaming: bool = False,
) -> dict[str, Any]:
    return {
        "appName": "quake_agent",
        "userId": str(uuid4()),
        "sessionId": str(uuid4()),
        "newMessage": {"role": "user", "parts": [{"text": text}]},
        "streaming": streaming,
    }


@pytest.fixture
def app_factory(monkeypatch) -> Callable[..., FastAPI]:
    def build(
        *,
        fail_run: bool = False,
        stream_error: bool = False,
        stream_started: asyncio.Event | None = None,
        stream_release: asyncio.Event | None = None,
        **environment: str,
    ) -> FastAPI:
        for name in (
            "QUAKE_AGENT_API_BASE_URL",
            "QUAKE_AGENT_ALLOWED_ORIGINS",
            "QUAKE_AGENT_RATE_LIMIT",
            "QUAKE_AGENT_RATE_WINDOW_SECONDS",
        ):
            monkeypatch.delenv(name, raising=False)
        for name, value in environment.items():
            monkeypatch.setenv(name, value)

        def fake_adk_app(**_: Any) -> FastAPI:
            app = FastAPI()

            @app.get("/health")
            async def health() -> dict[str, str]:
                return {"status": "ok"}

            @app.post("/run")
            async def run(request: Request) -> list[dict[str, Any]]:
                if fail_run:
                    raise RuntimeError("private test failure")
                payload = await request.json()
                return [
                    {
                        "author": "seismic_analyst",
                        "content": {
                            "role": "model",
                            "parts": [{"text": payload["newMessage"]["parts"][0]["text"]}],
                        },
                        "actions": {
                            "artifactDelta": {"earthquake-map.png": 0}
                        },
                    }
                ]

            @app.post("/run_sse")
            async def run_sse(request: Request) -> StreamingResponse:
                if fail_run:
                    raise RuntimeError("private test failure")
                payload = await request.json()

                async def event_stream():
                    if stream_started is not None:
                        stream_started.set()
                    if stream_release is not None:
                        await stream_release.wait()
                    if stream_error:
                        yield (
                            'data: {"error":"private stream failure",'
                            '"error_details":{"stacktrace":"secret traceback"}}\n\n'
                        )
                        return
                    text = payload["newMessage"]["parts"][0]["text"]
                    partial_event = {
                        "author": "seismic_analyst",
                        "partial": True,
                        "content": {
                            "role": "model",
                            "parts": [{"text": text[:8]}],
                        },
                        "actions": {"artifactDelta": {}},
                    }
                    final_event = {
                        "author": "seismic_analyst",
                        "partial": False,
                        "content": {
                            "role": "model",
                            "parts": [{"text": text}],
                        },
                        "actions": {
                            "artifactDelta": {"earthquake-map.png": 0}
                        },
                    }
                    yield f"data: {json.dumps(partial_event)}\n\n"
                    yield f"data: {json.dumps(final_event)}\n\n"

                return StreamingResponse(
                    event_stream(),
                    media_type="text/event-stream",
                )

            @app.get(
                "/apps/{app_name}/users/{user_id}/sessions/{session_id}/"
                "artifacts/{artifact_name:path}/versions/{version_id}"
            )
            async def artifact() -> dict[str, Any]:
                return {
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": "iVBORw0KGgo=",
                    }
                }

            @app.get("/list-apps")
            async def list_apps() -> list[str]:
                return ["should-not-be-public"]

            return app

        monkeypatch.setattr(web_app, "get_fast_api_app", fake_adk_app)
        return web_app.create_app()

    return build


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.8", 1234)),
        base_url="http://testserver",
    )


async def test_serves_chat_ui_and_runtime_endpoint(app_factory) -> None:
    app = app_factory(
        QUAKE_AGENT_API_BASE_URL="https://api.example.test/adk/",
    )
    async with _client(app) as client:
        page = await client.get("/")
        favicon = await client.get("/favicon.svg")
        config = await client.get("/runtime-config.json")

    assert page.status_code == 200
    assert '<link rel="icon" href="/favicon.svg" type="image/svg+xml">' in page.text
    assert '<img class="mark" src="/favicon.svg" alt="">' in page.text
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")
    assert "Quake Agent" in favicon.text
    assert "#67d4c0" in favicon.text
    assert "Quake Agent" in page.text
    assert "current and historical USGS earthquake data" in page.text
    assert "bounded historical searches" in page.text
    assert "Public place names may be sent to OpenStreetMap's Nominatim" in page.text
    assert "OpenStreetMap contributors" in page.text
    assert "artifactDelta" in page.text
    assert "inlineData" in page.text
    assert "event.errorMessage" in page.text
    assert "event.finishReason" in page.text
    assert "HTTP ${response.status}" in page.text
    assert "stream ended without text or diagnostic details" in page.text
    assert 'apiUrl("/run_sse")' in page.text
    assert '"Accept": "text/event-stream"' in page.text
    assert "response.body.getReader()" in page.text
    assert "streaming: true" in page.text
    assert "event.partial === true" in page.text
    assert "functionCall?.name" in page.text
    assert "functionCall.args" in page.text
    assert "part.functionResponse" in page.text
    assert "functionResponse.response" in page.text
    assert 'tools.className = "thinking-tools"' in page.text
    assert 'block.className = "thinking-tool"' in page.text
    assert "trackToolEvents(toolState, event)" in page.text
    assert "updateThinkingTools(thinking, toolState.calls)" in page.text
    assert 'research.className = "research"' in page.text
    assert 'label.textContent = "Research"' in page.text
    assert "research.open" not in page.text
    assert "research = showResearch(thinking, research, toolState, agentMessage)" in page.text
    assert "research?.isConnected" in page.text
    assert 'details.className = "research-call"' in page.text
    assert "state.calls.push(call)" in page.text
    assert "sensitiveToolKey.test(key)" in page.text
    assert "Internal orchestration output omitted." in page.text
    assert "rendered.length > 8_000" in page.text
    research_marker = "research = showResearch(thinking, research, toolState, agentMessage)"
    message_marker = 'if (!agentMessage) agentMessage = addMessage("agent", "")'
    assert page.text.index(research_marker) < page.text.index(message_marker)
    assert 'replace(/-/g, "+").replace(/_/g, "/")' in page.text
    assert "renderMarkdown" in page.text
    assert 'link.target = "_blank"' in page.text
    assert 'link.rel = "noopener noreferrer"' in page.text
    assert 'class="image-viewer"' in page.text
    assert "showModal()" in page.text
    assert 'event.key === "Escape"' in page.text
    assert "Full size" not in page.text
    assert "Download" in page.text
    prompt_bank_match = re.search(
        r'<script id="example-prompt-bank" type="application/json">\s*'
        r"(\[.*?\])\s*</script>",
        page.text,
        re.DOTALL,
    )
    assert prompt_bank_match is not None
    prompt_bank = json.loads(prompt_bank_match.group(1))
    featured_prompt = "Show me a map of all earthquakes in the last month."
    assert len(prompt_bank) == 34
    assert len(set(prompt_bank)) == 34
    assert {
        "Give me a map of all earthquakes of magnitude 5 or greater within a 100 km radius of Tokyo during the last five years.",
        "Map the largest earthquake in the last month and surrounding earthquakes.",
        "Map earthquakes near 37.7775, -122.416389 in the last month.",
        "Map earthquakes near Mountain View.",
    } <= set(prompt_bank)
    assert prompt_bank.count(featured_prompt) == 1
    assert prompt_bank[0] == featured_prompt
    assert "const examplePromptCount = 6;" in page.text
    assert "const candidates = examplePromptBank.slice(1);" in page.text
    assert "selected.splice(featuredPosition, 0, featuredPrompt);" in page.text
    assert "renderExamplePrompts();" in page.text
    assert 'new URLSearchParams(window.location.hash.slice(1)).get("q")' in page.text
    assert 'typeof crypto.randomUUID === "function"' in page.text
    assert "crypto.getRandomValues(new Uint8Array(16))" in page.text
    assert "value = createUuid();" in page.text
    assert "sessionStorage.setItem(sessionKey, createUuid());" in page.text
    assert "rememberFirstPrompt(cleanText);" in page.text
    assert 'window.history.pushState(null, "", url);' in page.text
    assert 'window.addEventListener("popstate"' in page.text
    assert "if (historyPrompt !== firstPrompt) window.location.reload();" in page.text
    assert "if (sharedPrompt) submitPrompt(sharedPrompt);" in page.text
    assert "clearSharedPrompt();" in page.text
    assert 'newConversation.addEventListener("click"' in page.text
    assert "newConversation.disabled" not in page.text
    assert '<button class="prompt-chip"' not in page.text
    assert page.headers["cache-control"] == "no-store"
    assert config.json() == {
        "apiBaseUrl": "https://api.example.test/adk",
        "appName": "quake_agent",
        "maxPromptCharacters": 2000,
        "temporarySessions": True,
    }


async def test_valid_run_body_reaches_adk_with_body_intact(app_factory) -> None:
    app = app_factory()
    payload = _run_payload(text="Show the largest hourly earthquakes")
    async with _client(app) as client:
        response = await client.post("/run", json=payload)

    assert response.status_code == 200
    event = response.json()[0]
    assert event["content"]["parts"][0]["text"] == payload["newMessage"][
        "parts"
    ][0]["text"]
    assert event["actions"]["artifactDelta"] == {"earthquake-map.png": 0}
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_streamed_run_reaches_adk_and_returns_sse(app_factory) -> None:
    app = app_factory()
    payload = _run_payload(
        text="Show the largest hourly earthquakes",
        streaming=True,
    )
    async with _client(app) as client:
        response = await client.post("/run_sse", json=payload)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    frames = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert frames[0]["partial"] is True
    assert frames[1]["partial"] is False
    assert frames[1]["content"]["parts"][0]["text"] == payload["newMessage"][
        "parts"
    ][0]["text"]
    assert frames[1]["actions"]["artifactDelta"] == {
        "earthquake-map.png": 0
    }


async def test_streamed_run_sanitizes_adk_errors(app_factory) -> None:
    app = app_factory(stream_error=True)
    async with _client(app) as client:
        response = await client.post(
            "/run_sse",
            json=_run_payload(streaming=True),
        )

    assert response.status_code == 200
    assert "private stream failure" not in response.text
    assert "secret traceback" not in response.text
    event = json.loads(response.text.removeprefix("data: "))
    assert event["error"].startswith("The server could not complete this request.")
    assert "Error reference:" in event["error"]


async def test_session_stays_locked_until_stream_finishes(app_factory) -> None:
    stream_started = asyncio.Event()
    stream_release = asyncio.Event()
    app = app_factory(
        stream_started=stream_started,
        stream_release=stream_release,
    )
    payload = _run_payload(streaming=True)
    async with _client(app) as client:
        first_request = asyncio.create_task(client.post("/run_sse", json=payload))
        await asyncio.wait_for(stream_started.wait(), timeout=1)
        second = await client.post("/run_sse", json=payload)
        stream_release.set()
        first = await asyncio.wait_for(first_request, timeout=1)

    assert first.status_code == 200
    assert second.status_code == 409


async def test_unhandled_run_error_returns_safe_json_500(app_factory) -> None:
    app = app_factory(fail_run=True)
    async with _client(app) as client:
        response = await client.post("/run", json=_run_payload())

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail.startswith("The server could not complete this request.")
    assert "Error reference:" in detail
    assert "private test failure" not in detail
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "change, expected",
    [
        ({"appName": "other"}, "appName"),
        ({"userId": "not-a-uuid"}, "UUID"),
        ({"streaming": True}, "/run_sse"),
        ({"streaming": "yes"}, "boolean"),
        ({"newMessage": {"role": "user", "parts": [{"text": ""}]}}, "empty"),
        (
            {
                "newMessage": {
                    "role": "user",
                    "parts": [{"inlineData": {"data": "unsafe"}}],
                }
            },
            "Only text",
        ),
    ],
)
async def test_rejects_unsupported_public_run_payloads(
    app_factory,
    change: dict[str, Any],
    expected: str,
) -> None:
    app = app_factory()
    payload = _run_payload()
    payload.update(change)
    async with _client(app) as client:
        response = await client.post("/run", json=payload)

    assert response.status_code == 400
    assert expected in response.json()["detail"]


async def test_stream_endpoint_requires_streaming_true(app_factory) -> None:
    app = app_factory()
    async with _client(app) as client:
        response = await client.post("/run_sse", json=_run_payload())

    assert response.status_code == 400
    assert "streaming must be true" in response.json()["detail"]


async def test_rate_limits_valid_prompts_by_client(app_factory) -> None:
    app = app_factory(
        QUAKE_AGENT_RATE_LIMIT="1",
        QUAKE_AGENT_RATE_WINDOW_SECONDS="60",
    )
    async with _client(app) as client:
        first = await client.post("/run", json=_run_payload())
        second = await client.post("/run", json=_run_payload())

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["retry-after"] == "60"


async def test_allows_only_versioned_png_artifacts_and_public_routes(
    app_factory,
) -> None:
    app = app_factory()
    user_id = str(uuid4())
    session_id = str(uuid4())
    png_path = (
        f"/apps/quake_agent/users/{user_id}/sessions/{session_id}/artifacts/"
        "maps/latest.png/versions/3"
    )
    json_path = png_path.replace("latest.png", "latest.json")
    async with _client(app) as client:
        artifact = await client.get(png_path)
        non_image = await client.get(json_path)
        apps = await client.get("/list-apps")
        docs = await client.get("/docs")

    assert artifact.status_code == 200
    assert artifact.json()["inlineData"]["mimeType"] == "image/png"
    assert non_image.status_code == 404
    assert apps.status_code == 404
    assert docs.status_code == 404


def test_rejects_invalid_runtime_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("QUAKE_AGENT_API_BASE_URL", "javascript:alert(1)")
    with pytest.raises(RuntimeError, match="absolute HTTP"):
        web_app.create_app()


def test_client_ip_uses_cloud_run_forwarding_position() -> None:
    request = Request(
        {
            "type": "http",
            "headers": [
                (
                    b"x-forwarded-for",
                    b"spoofed.example, 203.0.113.9, 35.191.0.1",
                )
            ],
            "client": ("127.0.0.1", 1234),
        }
    )

    assert web_app._client_ip(request) == "203.0.113.9"


def test_main_uses_host_and_port_environment(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setenv("HOST", "127.0.0.2")
    monkeypatch.setenv("PORT", "9876")
    monkeypatch.setattr(
        web_app.uvicorn,
        "run",
        lambda application, **kwargs: calls.append(
            {"application": application, **kwargs}
        ),
    )

    web_app.main()

    assert calls == [
        {
            "application": web_app.app,
            "host": "127.0.0.2",
            "port": 9876,
            "proxy_headers": True,
        }
    ]
