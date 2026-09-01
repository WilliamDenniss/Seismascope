"""Bounded forward geocoding for public geographic place names."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
import json
import math
import os
import re
import time
import unicodedata
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from .geography_tools import GeographicCoordinate


DEFAULT_GEOCODER_BASE_URL = "https://nominatim.openstreetmap.org"
GEOCODER_BASE_URL_ENV = "QUAKE_AGENT_GEOCODER_BASE_URL"
GEOCODER_USER_AGENT = (
    "QuakeAgent/0.1 (+https://github.com/WilliamDenniss/quakeagent)"
)
GEOCODER_PROVIDER = "OpenStreetMap Nominatim"
GEOCODER_ATTRIBUTION = "Data © OpenStreetMap contributors, ODbL 1.0"
GEOCODER_ATTRIBUTION_URL = "https://www.openstreetmap.org/copyright"
GEOCODER_USAGE_POLICY_URL = (
    "https://operations.osmfoundation.org/policies/nominatim/"
)
MAX_GEOCODER_QUERY_CHARACTERS = 200
MAX_GEOCODER_RESPONSE_BYTES = 128 * 1024
DEFAULT_CACHE_CAPACITY = 512
DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = 1.1

_COUNTRY_CODE = re.compile(r"^[A-Za-z]{2}$")


class GeocoderError(RuntimeError):
    """A controlled geocoder failure."""


class GeocoderRateLimitedError(GeocoderError):
    """The configured geocoder declined a request because of its rate limit."""


@dataclass
class _CacheEntry:
    expires_at: float
    match: dict[str, Any] | None


def _validate_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{GEOCODER_BASE_URL_ENV} must be an absolute HTTPS URL without "
            "credentials, a query, or a fragment."
        )
    return normalized


def _configured_base_url() -> str:
    return _validate_base_url(
        os.getenv(GEOCODER_BASE_URL_ENV, DEFAULT_GEOCODER_BASE_URL)
    )


def _clean_query(query: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", query).split())


def _normalize_request(
    query: Any,
    country_code: Any,
) -> tuple[str, str | None]:
    if not isinstance(query, str):
        raise ValueError("query must be a string.")
    cleaned_query = _clean_query(query)
    if not cleaned_query:
        raise ValueError("query must not be empty.")
    if len(cleaned_query) > MAX_GEOCODER_QUERY_CHARACTERS:
        raise ValueError(
            f"query must not exceed {MAX_GEOCODER_QUERY_CHARACTERS} characters."
        )

    if country_code is None:
        return cleaned_query, None
    if not isinstance(country_code, str) or not _COUNTRY_CODE.fullmatch(
        country_code.strip()
    ):
        raise ValueError("country_code must be a two-letter ISO country code.")
    return cleaned_query, country_code.strip().lower()


def _finite_float(value: Any) -> float | None:
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return None
    return normalized if math.isfinite(normalized) else None


def _normalized_bounding_box(value: Any) -> dict[str, float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    south, north, west, east = (_finite_float(item) for item in value)
    if None in (south, north, west, east):
        return None
    assert south is not None
    assert north is not None
    assert west is not None
    assert east is not None
    if not (-90 <= south <= north <= 90 and -180 <= west <= east <= 180):
        return None
    return {"south": south, "north": north, "west": west, "east": east}


def _parse_nominatim_response(raw: bytes) -> dict[str, Any] | None:
    if len(raw) > MAX_GEOCODER_RESPONSE_BYTES:
        raise GeocoderError(
            f"Geocoder response exceeds {MAX_GEOCODER_RESPONSE_BYTES} bytes."
        )
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GeocoderError("Geocoder response is not valid UTF-8 JSON.") from exc
    if not isinstance(payload, list):
        raise GeocoderError("Geocoder response is not a result list.")
    if not payload:
        return None

    result = payload[0]
    if not isinstance(result, dict):
        raise GeocoderError("Geocoder result is invalid.")
    display_name = result.get("display_name")
    latitude = _finite_float(result.get("lat"))
    longitude = _finite_float(result.get("lon"))
    if not isinstance(display_name, str) or not display_name.strip():
        raise GeocoderError("Geocoder result has no display name.")
    if latitude is None or longitude is None:
        raise GeocoderError("Geocoder result has no valid coordinate.")
    try:
        coordinate = GeographicCoordinate(
            latitude=latitude,
            longitude=longitude,
        )
    except ValidationError as exc:
        raise GeocoderError("Geocoder result coordinate is out of range.") from exc

    match: dict[str, Any] = {
        "display_name": display_name.strip(),
        "coordinate": {
            "latitude": coordinate.latitude,
            "longitude": coordinate.longitude,
        },
        "map_coord": [coordinate.longitude, coordinate.latitude],
    }
    address = result.get("address")
    if isinstance(address, dict):
        result_country_code = address.get("country_code")
        if isinstance(result_country_code, str) and _COUNTRY_CODE.fullmatch(
            result_country_code
        ):
            match["country_code"] = result_country_code.lower()
        country = address.get("country")
        if isinstance(country, str) and country.strip():
            match["country"] = country.strip()

    bounding_box = _normalized_bounding_box(result.get("boundingbox"))
    if bounding_box is not None:
        match["bounding_box"] = bounding_box

    for source_key, result_key in (
        ("category", "category"),
        ("type", "place_type"),
        ("osm_type", "osm_type"),
    ):
        value = result.get(source_key)
        if isinstance(value, str) and value.strip():
            match[result_key] = value.strip()
    osm_id = result.get("osm_id")
    if isinstance(osm_id, int) and not isinstance(osm_id, bool):
        match["osm_id"] = osm_id
    return match


class NominatimGeocoder:
    """A single-process, policy-bounded Nominatim search client."""

    def __init__(
        self,
        base_url: str,
        *,
        cache_capacity: int = DEFAULT_CACHE_CAPACITY,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        min_request_interval_seconds: float = DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if cache_capacity < 1:
            raise ValueError("cache_capacity must be positive.")
        if cache_ttl_seconds <= 0:
            raise ValueError("cache_ttl_seconds must be positive.")
        if min_request_interval_seconds < 0:
            raise ValueError("min_request_interval_seconds must not be negative.")
        self.base_url = _validate_base_url(base_url)
        self.cache_capacity = cache_capacity
        self.cache_ttl_seconds = cache_ttl_seconds
        self.min_request_interval_seconds = min_request_interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._transport = transport
        self._cache: OrderedDict[tuple[str, str | None], _CacheEntry] = OrderedDict()
        self._request_lock = asyncio.Lock()
        self._last_request_started: float | None = None

    def _cache_key(self, query: str, country_code: str | None) -> tuple[str, str | None]:
        return query.casefold(), country_code

    def _cached_match(
        self,
        key: tuple[str, str | None],
    ) -> tuple[bool, dict[str, Any] | None]:
        entry = self._cache.get(key)
        if entry is None:
            return False, None
        if entry.expires_at <= self._clock():
            self._cache.pop(key, None)
            return False, None
        self._cache.move_to_end(key)
        return True, deepcopy(entry.match)

    def _cache_match(
        self,
        key: tuple[str, str | None],
        match: dict[str, Any] | None,
    ) -> None:
        self._cache[key] = _CacheEntry(
            expires_at=self._clock() + self.cache_ttl_seconds,
            match=deepcopy(match),
        )
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_capacity:
            self._cache.popitem(last=False)

    async def _pace_request(self) -> None:
        now = self._clock()
        if self._last_request_started is not None:
            remaining = (
                self.min_request_interval_seconds
                - (now - self._last_request_started)
            )
            if remaining > 0:
                await self._sleep(remaining)
        self._last_request_started = self._clock()

    async def _request_once(
        self,
        query: str,
        country_code: str | None,
    ) -> bytes:
        params: dict[str, str | int] = {
            "q": query,
            "format": "jsonv2",
            "limit": 1,
            "dedupe": 1,
            "addressdetails": 1,
            "accept-language": "en",
        }
        if country_code is not None:
            params["countrycodes"] = country_code

        timeout = httpx.Timeout(10.0, connect=5.0)
        headers = {
            "Accept": "application/json",
            "User-Agent": GEOCODER_USER_AGENT,
        }
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            async with client.stream(
                "GET",
                f"{self.base_url}/search",
                params=params,
                headers=headers,
            ) as response:
                if response.status_code == 429:
                    raise GeocoderRateLimitedError(
                        "Geocoder is temporarily rate limited."
                    )
                response.raise_for_status()
                length_header = response.headers.get("content-length")
                if length_header:
                    try:
                        declared_length = int(length_header)
                    except ValueError:
                        declared_length = 0
                    if declared_length > MAX_GEOCODER_RESPONSE_BYTES:
                        raise GeocoderError(
                            "Geocoder response exceeds the size limit."
                        )

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_GEOCODER_RESPONSE_BYTES:
                        raise GeocoderError(
                            "Geocoder response exceeds the size limit."
                        )
                    chunks.append(chunk)
                return b"".join(chunks)

    async def _lookup(
        self,
        query: str,
        country_code: str | None,
    ) -> dict[str, Any] | None:
        last_error: Exception | None = None
        for attempt in range(2):
            await self._pace_request()
            try:
                raw = await self._request_once(query, country_code)
                return _parse_nominatim_response(raw)
            except GeocoderRateLimitedError:
                raise
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if exc.response.status_code < 500 or attempt == 1:
                    break
            except httpx.TransportError as exc:
                last_error = exc
                if attempt == 1:
                    break
            except GeocoderError:
                raise
        raise GeocoderError("Geocoder is temporarily unavailable.") from last_error

    async def search(
        self,
        query: str,
        country_code: str | None = None,
    ) -> dict[str, Any]:
        cleaned_query, normalized_country_code = _normalize_request(
            query,
            country_code,
        )
        key = self._cache_key(cleaned_query, normalized_country_code)
        found, match = self._cached_match(key)
        if not found:
            async with self._request_lock:
                found, match = self._cached_match(key)
                if not found:
                    match = await self._lookup(
                        cleaned_query,
                        normalized_country_code,
                    )
                    self._cache_match(key, match)

        result: dict[str, Any] = {
            "status": "ok" if match is not None else "no_match",
            "query": cleaned_query,
            "provider": GEOCODER_PROVIDER,
            "attribution": GEOCODER_ATTRIBUTION,
            "attribution_url": GEOCODER_ATTRIBUTION_URL,
            "usage_policy_url": GEOCODER_USAGE_POLICY_URL,
            "cached": found,
        }
        if normalized_country_code is not None:
            result["requested_country_code"] = normalized_country_code
        if match is not None:
            result["match"] = match
        return result


_geocoder = NominatimGeocoder(_configured_base_url())


async def geocode_place(
    query: str,
    country_code: str | None = None,
) -> dict[str, Any]:
    """Resolve one public place name to the top matching coordinate.

    Use this only for an end-user-triggered public landmark or geographic area.
    Do not submit personal, confidential, autocomplete, systematic, or bulk
    queries. The optional country code is a hard two-letter ISO country filter.

    Args:
        query: Public place, landmark, or geographic area, from 1 to 200 characters.
        country_code: Optional two-letter ISO country code that restricts results.

    Returns:
        The top normalized match, provider attribution, and cache status; a
        controlled no-match or error result otherwise.
    """
    try:
        return await _geocoder.search(query, country_code)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}
    except GeocoderRateLimitedError:
        return {
            "status": "error",
            "provider": GEOCODER_PROVIDER,
            "error": "Geocoder is temporarily rate limited. Try again later.",
        }
    except (GeocoderError, httpx.HTTPError):
        return {
            "status": "error",
            "provider": GEOCODER_PROVIDER,
            "error": "Geocoder is temporarily unavailable.",
        }
