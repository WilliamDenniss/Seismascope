from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from fastapi import Request
import httpx
import pytest

from web import app as web_app


def _run_payload(*, text: str = "Map recent earthquakes") -> dict[str, Any]:
    return {
        "appName": "quake_agent",
        "userId": str(uuid4()),
        "sessionId": str(uuid4()),
        "newMessage": {"role": "user", "parts": [{"text": text}]},
        "streaming": False,
    }


@pytest.fixture
def app_factory(monkeypatch) -> Callable[..., FastAPI]:
    def build(**environment: str) -> FastAPI:
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
    assert "artifactDelta" in page.text
    assert "inlineData" in page.text
    assert 'replace(/-/g, "+").replace(/_/g, "/")' in page.text
    assert "renderMarkdown" in page.text
    assert 'link.target = "_blank"' in page.text
    assert 'link.rel = "noopener noreferrer"' in page.text
    assert 'class="image-viewer"' in page.text
    assert "showModal()" in page.text
    assert 'event.key === "Escape"' in page.text
    assert "Full size" not in page.text
    assert "Download" in page.text
    assert page.text.count('class="prompt-chip"') == 6
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


@pytest.mark.parametrize(
    "change, expected",
    [
        ({"appName": "other"}, "appName"),
        ({"userId": "not-a-uuid"}, "UUID"),
        ({"streaming": True}, "Streaming"),
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
