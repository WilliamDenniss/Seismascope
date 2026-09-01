from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import quake_agent.tools.geocoding_tools as geocoding_tools
from quake_agent.tools.geocoding_tools import GeocoderError
from quake_agent.tools.geocoding_tools import MAX_GEOCODER_RESPONSE_BYTES
from quake_agent.tools.geocoding_tools import NominatimGeocoder
from quake_agent.tools.geocoding_tools import _parse_nominatim_response
from quake_agent.tools.geocoding_tools import _validate_base_url


def _tokyo_station_result() -> dict[str, object]:
    return {
        "place_id": 123,
        "licence": "Data © OpenStreetMap contributors, ODbL 1.0",
        "osm_type": "node",
        "osm_id": 456,
        "lat": "35.6817523",
        "lon": "139.7671248",
        "category": "railway",
        "type": "station",
        "display_name": "Tokyo Station, Marunouchi, Chiyoda, Tokyo, Japan",
        "boundingbox": ["35.676", "35.687", "139.759", "139.773"],
        "address": {"country": "Japan", "country_code": "jp"},
    }


class FakeTime:
    def __init__(self) -> None:
        self.value = 100.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


async def test_search_returns_top_normalized_match_and_country_filter() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[_tokyo_station_result()])

    geocoder = NominatimGeocoder(
        "https://geocoder.example.test/base/",
        min_request_interval_seconds=0,
        transport=httpx.MockTransport(handler),
    )

    result = await geocoder.search("  Tokyo   Station  ", "JP")

    assert result == {
        "status": "ok",
        "query": "Tokyo Station",
        "provider": "OpenStreetMap Nominatim",
        "attribution": "Data © OpenStreetMap contributors, ODbL 1.0",
        "attribution_url": "https://www.openstreetmap.org/copyright",
        "usage_policy_url": (
            "https://operations.osmfoundation.org/policies/nominatim/"
        ),
        "cached": False,
        "requested_country_code": "jp",
        "match": {
            "display_name": "Tokyo Station, Marunouchi, Chiyoda, Tokyo, Japan",
            "coordinate": {"latitude": 35.6817523, "longitude": 139.7671248},
            "map_coord": [139.7671248, 35.6817523],
            "country_code": "jp",
            "country": "Japan",
            "bounding_box": {
                "south": 35.676,
                "north": 35.687,
                "west": 139.759,
                "east": 139.773,
            },
            "category": "railway",
            "place_type": "station",
            "osm_type": "node",
            "osm_id": 456,
        },
    }
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/base/search"
    assert request.url.params["q"] == "Tokyo Station"
    assert request.url.params["format"] == "jsonv2"
    assert request.url.params["limit"] == "1"
    assert request.url.params["dedupe"] == "1"
    assert request.url.params["addressdetails"] == "1"
    assert request.url.params["accept-language"] == "en"
    assert request.url.params["countrycodes"] == "jp"
    assert request.headers["user-agent"].startswith("QuakeAgent/0.1")


async def test_cache_normalizes_query_and_caches_no_match() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[])

    geocoder = NominatimGeocoder(
        "https://geocoder.example.test",
        min_request_interval_seconds=0,
        transport=httpx.MockTransport(handler),
    )

    first = await geocoder.search("Missing Place", "US")
    second = await geocoder.search("  missing   place ", "us")

    assert first["status"] == "no_match"
    assert first["cached"] is False
    assert second["status"] == "no_match"
    assert second["cached"] is True
    assert calls == 1


async def test_cache_expires_and_evicts_least_recently_used_entry() -> None:
    clock = FakeTime()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        result = _tokyo_station_result()
        result["display_name"] = request.url.params["q"]
        return httpx.Response(200, json=[result])

    geocoder = NominatimGeocoder(
        "https://geocoder.example.test",
        cache_capacity=1,
        cache_ttl_seconds=10,
        min_request_interval_seconds=0,
        clock=clock.now,
        sleep=clock.sleep,
        transport=httpx.MockTransport(handler),
    )

    await geocoder.search("First")
    await geocoder.search("Second")
    await geocoder.search("First")
    assert calls == 3

    clock.value += 11
    await geocoder.search("First")
    assert calls == 4


async def test_concurrent_identical_requests_are_coalesced() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return httpx.Response(200, json=[_tokyo_station_result()])

    geocoder = NominatimGeocoder(
        "https://geocoder.example.test",
        min_request_interval_seconds=0,
        transport=httpx.MockTransport(handler),
    )

    first, second = await asyncio.gather(
        geocoder.search("Tokyo Station"),
        geocoder.search("Tokyo Station"),
    )

    assert calls == 1
    assert {first["cached"], second["cached"]} == {False, True}


async def test_uncached_requests_and_retry_are_globally_paced() -> None:
    clock = FakeTime()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": "busy"})
        return httpx.Response(200, json=[_tokyo_station_result()])

    geocoder = NominatimGeocoder(
        "https://geocoder.example.test",
        min_request_interval_seconds=1.1,
        clock=clock.now,
        sleep=clock.sleep,
        transport=httpx.MockTransport(handler),
    )

    result = await geocoder.search("Tokyo Station")
    second = await geocoder.search("Yokohama Station")

    assert result["status"] == "ok"
    assert second["status"] == "ok"
    assert calls == 3
    assert clock.sleeps == pytest.approx([1.1, 1.1])


async def test_public_tool_returns_controlled_rate_limit_error(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "60"})

    geocoder = NominatimGeocoder(
        "https://geocoder.example.test",
        min_request_interval_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(geocoding_tools, "_geocoder", geocoder)

    result = await geocoding_tools.geocode_place("Tokyo Station")

    assert result == {
        "status": "error",
        "provider": "OpenStreetMap Nominatim",
        "error": "Geocoder is temporarily rate limited. Try again later.",
    }


async def test_public_tool_retries_timeout_once_then_returns_controlled_error(
    monkeypatch,
) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    geocoder = NominatimGeocoder(
        "https://geocoder.example.test",
        min_request_interval_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(geocoding_tools, "_geocoder", geocoder)

    result = await geocoding_tools.geocode_place("Tokyo Station")

    assert calls == 2
    assert result == {
        "status": "error",
        "provider": "OpenStreetMap Nominatim",
        "error": "Geocoder is temporarily unavailable.",
    }


@pytest.mark.parametrize(
    ("query", "country_code", "expected_error"),
    [
        ("", None, "query must not be empty"),
        ("x" * 201, None, "query must not exceed 200 characters"),
        ("Tokyo", "JPN", "country_code must be a two-letter"),
        ("Tokyo", "1p", "country_code must be a two-letter"),
    ],
)
async def test_public_tool_rejects_invalid_inputs(
    query: str,
    country_code: str | None,
    expected_error: str,
) -> None:
    result = await geocoding_tools.geocode_place(query, country_code)

    assert result["status"] == "error"
    assert expected_error in result["error"]


def test_response_parser_rejects_malformed_or_oversized_data() -> None:
    with pytest.raises(GeocoderError, match="not a result list"):
        _parse_nominatim_response(json.dumps({"result": []}).encode())
    with pytest.raises(GeocoderError, match="out of range"):
        result = _tokyo_station_result()
        result["lat"] = "91"
        _parse_nominatim_response(json.dumps([result]).encode())
    with pytest.raises(GeocoderError, match="exceeds"):
        _parse_nominatim_response(b" " * (MAX_GEOCODER_RESPONSE_BYTES + 1))


@pytest.mark.parametrize(
    "value",
    [
        "http://geocoder.example.test",
        "https://user:password@geocoder.example.test",
        "https://geocoder.example.test?key=value",
        "https://geocoder.example.test#fragment",
        "not-a-url",
    ],
)
def test_geocoder_base_url_requires_safe_https_endpoint(value: str) -> None:
    with pytest.raises(ValueError, match="absolute HTTPS URL"):
        _validate_base_url(value)
