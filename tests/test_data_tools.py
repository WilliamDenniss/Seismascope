from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from quake_agent.tools import data_tools
from quake_agent.tools.data_tools import FeedDownloadError
from quake_agent.tools.data_tools import GeoBounds
from quake_agent.tools.data_tools import download_usgs_feed
from quake_agent.tools.data_tools import query_usgs_feed


FIXTURE = Path(__file__).parent / "fixtures" / "usgs-sample.geojson"


@pytest.fixture
def catalog_bytes() -> bytes:
    return FIXTURE.read_bytes()


async def test_download_saves_version_and_reuses_fresh_cache(
    monkeypatch: pytest.MonkeyPatch,
    artifact_context,
    catalog_bytes: bytes,
) -> None:
    calls = 0

    async def fake_fetch(url: str) -> bytes:
        nonlocal calls
        calls += 1
        assert url == data_tools.USGS_FEED_URLS["hourly"]
        return catalog_bytes

    monkeypatch.setattr(data_tools, "_fetch_usgs_bytes", fake_fetch)
    first = await download_usgs_feed("hourly", tool_context=artifact_context)
    second = await download_usgs_feed("hourly", tool_context=artifact_context)

    assert first["status"] == "ok"
    assert first["artifact_version"] == 0
    assert first["cached"] is False
    assert second["artifact_version"] == 0
    assert second["cached"] is True
    assert second["stale"] is False
    assert calls == 1
    assert artifact_context.state["catalog_hourly_version"] == 0
    assert await artifact_context.list_versions("usgs-hourly.geojson") == [0]


async def test_failed_refresh_returns_explicit_stale_artifact(
    monkeypatch: pytest.MonkeyPatch,
    artifact_context,
    catalog_bytes: bytes,
) -> None:
    async def successful_fetch(url: str) -> bytes:
        return catalog_bytes

    monkeypatch.setattr(data_tools, "_fetch_usgs_bytes", successful_fetch)
    await download_usgs_feed("monthly", tool_context=artifact_context)

    async def failed_fetch(url: str) -> bytes:
        raise FeedDownloadError("network unavailable")

    monkeypatch.setattr(data_tools, "_fetch_usgs_bytes", failed_fetch)
    result = await download_usgs_feed(
        "monthly", force_refresh=True, tool_context=artifact_context
    )

    assert result["status"] == "ok"
    assert result["cached"] is True
    assert result["stale"] is True
    assert result["artifact_version"] == 0
    assert "network unavailable" in result["refresh_error"]


async def test_fetch_retries_one_transport_error(
    monkeypatch: pytest.MonkeyPatch,
    catalog_bytes: bytes,
) -> None:
    calls = 0

    async def flaky_request(url: str) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("temporary")
        return catalog_bytes

    async def no_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(data_tools, "_request_usgs_once", flaky_request)
    monkeypatch.setattr(data_tools.asyncio, "sleep", no_sleep)
    result = await data_tools._fetch_usgs_bytes("https://example.invalid/feed")

    assert result == catalog_bytes
    assert calls == 2


def test_catalog_validation_rejects_invalid_and_oversized_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(FeedDownloadError, match="FeatureCollection"):
        data_tools._validate_catalog_bytes(b'{"type":"Point"}')
    with pytest.raises(FeedDownloadError, match="valid UTF-8 JSON"):
        data_tools._validate_catalog_bytes(b"not-json")
    monkeypatch.setattr(data_tools, "MAX_RESPONSE_BYTES", 10)
    with pytest.raises(FeedDownloadError, match="exceeds"):
        data_tools._validate_catalog_bytes(b"x" * 11)


async def test_query_filters_sorts_deduplicates_and_tracks_provenance(
    monkeypatch: pytest.MonkeyPatch,
    artifact_context,
    catalog_bytes: bytes,
) -> None:
    async def fake_fetch(url: str) -> bytes:
        return catalog_bytes

    monkeypatch.setattr(data_tools, "_fetch_usgs_bytes", fake_fetch)
    await download_usgs_feed("monthly", tool_context=artifact_context)

    result = await query_usgs_feed(
        "monthly",
        min_magnitude=4.0,
        sort="magnitude_desc",
        limit=10,
        tool_context=artifact_context,
    )

    assert result["status"] == "ok"
    assert result["artifact_name"] == "usgs-monthly.geojson"
    assert result["artifact_version"] == 0
    assert [event["id"] for event in result["events"]] == ["event-2", "event-1"]
    assert result["events"][1]["google_maps_url"] == (
        "https://www.google.com/maps/place/37.8,-122.3/@37.8,-122.3,6z/"
    )
    assert result["deduplicated_count"] == 1
    assert result["skipped_invalid"] == 2
    assert artifact_context.state["active_event_ids"] == ["event-2", "event-1"]


async def test_query_handles_antimeridian_and_missing_optional_fields(
    monkeypatch: pytest.MonkeyPatch,
    artifact_context,
    catalog_bytes: bytes,
) -> None:
    async def fake_fetch(url: str) -> bytes:
        return catalog_bytes

    monkeypatch.setattr(data_tools, "_fetch_usgs_bytes", fake_fetch)
    await download_usgs_feed("hourly", tool_context=artifact_context)

    wrapped = await query_usgs_feed(
        "hourly",
        bounds=GeoBounds(west=170, south=0, east=-170, north=20),
        limit=10,
        tool_context=artifact_context,
    )
    missing = await query_usgs_feed(
        "hourly",
        bounds=GeoBounds(west=0, south=-20, east=20, north=0),
        limit=10,
        tool_context=artifact_context,
    )

    assert {event["id"] for event in wrapped["events"]} == {"event-2", "event-3"}
    links_by_id = {
        event["id"]: event["google_maps_url"] for event in wrapped["events"]
    }
    assert links_by_id == {
        "event-2": (
            "https://www.google.com/maps/place/10.0,179.5/@10.0,179.5,6z/"
        ),
        "event-3": (
            "https://www.google.com/maps/place/11.0,-179.7/@11.0,-179.7,6z/"
        ),
    }
    assert missing["events"][0]["id"] == "event-4"
    assert missing["events"][0]["magnitude"] is None
    assert missing["events"][0]["place"] is None


async def test_query_rejects_bad_limits_times_and_missing_catalog(
    artifact_context,
) -> None:
    missing = await query_usgs_feed("hourly", tool_context=artifact_context)
    bad_limit = await query_usgs_feed("hourly", limit=501, tool_context=artifact_context)
    bad_time = await query_usgs_feed(
        "hourly", start_time="yesterday-ish", tool_context=artifact_context
    )

    assert missing["status"] == "error"
    assert "download_usgs_feed" in missing["error"]
    assert bad_limit["status"] == "error"
    assert bad_time["status"] == "error"
