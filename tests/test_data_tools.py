from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from quake_agent.tools import data_tools
from quake_agent.tools.data_tools import FeedDownloadError
from quake_agent.tools.data_tools import GeoBounds
from quake_agent.tools.data_tools import SearchCircle
from quake_agent.tools.data_tools import download_usgs_feed
from quake_agent.tools.data_tools import query_usgs_feed
from quake_agent.tools.data_tools import query_usgs_search
from quake_agent.tools.data_tools import search_usgs_events


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


async def test_historical_search_builds_exact_circle_query_and_reuses_cache(
    monkeypatch: pytest.MonkeyPatch,
    artifact_context,
    catalog_bytes: bytes,
) -> None:
    count_calls: list[dict[str, object]] = []
    query_calls: list[tuple[str, dict[str, object]]] = []

    async def fake_count(params: dict[str, object]) -> int:
        count_calls.append(params)
        return 7

    async def fake_fetch(
        url: str, *, params: dict[str, object] | None = None
    ) -> bytes:
        assert params is not None
        query_calls.append((url, params))
        return catalog_bytes

    monkeypatch.setattr(data_tools, "_fetch_usgs_count", fake_count)
    monkeypatch.setattr(data_tools, "_fetch_usgs_bytes", fake_fetch)
    circle = SearchCircle(
        latitude=35.6762,
        longitude=139.6503,
        max_radius_km=100,
    )

    first = await search_usgs_events(
        "2021-08-31",
        "2026-08-31T12:30:00+09:00",
        min_magnitude=5,
        circle=circle,
        tool_context=artifact_context,
    )
    second = await search_usgs_events(
        "2021-08-31T00:00:00Z",
        "2026-08-31T03:30:00Z",
        min_magnitude=5.0,
        circle=circle,
        tool_context=artifact_context,
    )

    expected_base = {
        "format": "geojson",
        "starttime": "2021-08-31T00:00:00Z",
        "endtime": "2026-08-31T03:30:00Z",
        "eventtype": "earthquake",
        "minmagnitude": 5.0,
        "latitude": 35.6762,
        "longitude": 139.6503,
        "maxradiuskm": 100.0,
    }
    assert count_calls == [expected_base]
    assert query_calls == [
        (
            data_tools.USGS_EVENT_QUERY_URL,
            {**expected_base, "limit": 7, "orderby": "magnitude"},
        )
    ]
    assert first["status"] == "ok"
    assert first["artifact_name"] == "usgs-search.geojson"
    assert first["artifact_version"] == 0
    assert first["cached"] is False
    assert first["truncated"] is False
    assert second["artifact_version"] == 0
    assert second["cached"] is True

    part = await artifact_context.load_artifact("usgs-search.geojson", version=0)
    assert part.inline_data is not None
    stored = json.loads(bytes(part.inline_data.data))
    provenance = stored[data_tools.SEARCH_PROVENANCE_KEY]
    assert provenance["kind"] == "usgs_event_search"
    assert provenance["query"]["circle"] == circle.model_dump()
    assert provenance["total_matched"] == 7
    assert provenance["stored_count"] == 7


async def test_historical_search_truncates_strongest_and_supports_listing(
    monkeypatch: pytest.MonkeyPatch,
    artifact_context,
    catalog_bytes: bytes,
) -> None:
    query_params: dict[str, object] = {}

    async def fake_count(params: dict[str, object]) -> int:
        assert {"latitude", "longitude", "maxradiuskm"}.isdisjoint(params)
        return 20_001

    async def fake_fetch(
        url: str, *, params: dict[str, object] | None = None
    ) -> bytes:
        assert params is not None
        query_params.update(params)
        return catalog_bytes

    monkeypatch.setattr(data_tools, "_fetch_usgs_count", fake_count)
    monkeypatch.setattr(data_tools, "_fetch_usgs_bytes", fake_fetch)

    searched = await search_usgs_events(
        "2020-01-01",
        "2025-01-01",
        min_magnitude=3,
        tool_context=artifact_context,
    )
    listed = await query_usgs_search(
        searched["artifact_version"],
        min_magnitude=5,
        sort="magnitude_desc",
        limit=10,
        tool_context=artifact_context,
    )

    assert query_params["limit"] == 20_000
    assert query_params["orderby"] == "magnitude"
    assert searched["truncated"] is True
    assert "strongest 7 of 20,001 matches" in searched["truncation_notice"]
    assert listed["truncated"] is True
    assert listed["truncation_notice"] == searched["truncation_notice"]
    assert [event["id"] for event in listed["events"]] == ["event-2"]


async def test_historical_search_empty_result_has_no_artifact(
    monkeypatch: pytest.MonkeyPatch,
    artifact_context,
) -> None:
    calls = 0

    async def fake_count(params: dict[str, object]) -> int:
        nonlocal calls
        calls += 1
        return 0

    monkeypatch.setattr(data_tools, "_fetch_usgs_count", fake_count)
    first = await search_usgs_events(
        "1900-01-01", "1901-01-01", tool_context=artifact_context
    )
    second = await search_usgs_events(
        "1900-01-01T00:00:00Z",
        "1901-01-01T00:00:00Z",
        tool_context=artifact_context,
    )

    assert first["status"] == "empty"
    assert second["status"] == "empty"
    assert second["cached"] is True
    assert calls == 1
    assert artifact_context.state["catalog_search_version"] is None
    assert await artifact_context.list_versions("usgs-search.geojson") == []


async def test_historical_search_stale_fallback_isolated_by_query(
    monkeypatch: pytest.MonkeyPatch,
    artifact_context,
    catalog_bytes: bytes,
) -> None:
    async def successful_count(params: dict[str, object]) -> int:
        return 7

    async def successful_fetch(
        url: str, *, params: dict[str, object] | None = None
    ) -> bytes:
        return catalog_bytes

    monkeypatch.setattr(data_tools, "_fetch_usgs_count", successful_count)
    monkeypatch.setattr(data_tools, "_fetch_usgs_bytes", successful_fetch)
    await search_usgs_events(
        "2020-01-01", "2021-01-01", tool_context=artifact_context
    )

    async def failed_count(params: dict[str, object]) -> int:
        raise FeedDownloadError("count unavailable")

    monkeypatch.setattr(data_tools, "_fetch_usgs_count", failed_count)
    identical = await search_usgs_events(
        "2020-01-01",
        "2021-01-01",
        force_refresh=True,
        tool_context=artifact_context,
    )
    different = await search_usgs_events(
        "2020-01-01",
        "2022-01-01",
        force_refresh=True,
        tool_context=artifact_context,
    )

    assert identical["status"] == "ok"
    assert identical["stale"] is True
    assert "count unavailable" in identical["refresh_error"]
    assert different["status"] == "error"
    assert different["stale"] is False


async def test_historical_search_rejects_invalid_inputs_and_missing_catalog(
    artifact_context,
) -> None:
    bad_start = await search_usgs_events(
        "last year", "2025-01-01", tool_context=artifact_context
    )
    reversed_range = await search_usgs_events(
        "2025-01-02", "2025-01-01", tool_context=artifact_context
    )
    bad_magnitude = await search_usgs_events(
        "2025-01-01",
        "2025-01-02",
        min_magnitude=float("nan"),
        tool_context=artifact_context,
    )
    missing = await query_usgs_search(tool_context=artifact_context)

    assert bad_start["status"] == "error"
    assert reversed_range["status"] == "error"
    assert bad_magnitude["status"] == "error"
    assert missing["status"] == "error"
    with pytest.raises(ValueError):
        SearchCircle(latitude=0, longitude=0, max_radius_km=0)


def test_usgs_count_validation_rejects_invalid_and_oversized_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert data_tools._parse_usgs_count(b'{"count":12,"maxAllowed":20000}') == 12
    with pytest.raises(FeedDownloadError, match="valid JSON"):
        data_tools._parse_usgs_count(b"not-json")
    with pytest.raises(FeedDownloadError, match="valid event count"):
        data_tools._parse_usgs_count(b'{"count":true}')
    monkeypatch.setattr(data_tools, "MAX_COUNT_RESPONSE_BYTES", 4)
    with pytest.raises(FeedDownloadError, match="exceeds"):
        data_tools._parse_usgs_count(b'{"count":0}')
