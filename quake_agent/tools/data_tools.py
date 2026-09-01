"""Download, persist, and query allowlisted USGS earthquake catalogs."""

from __future__ import annotations

import asyncio
from datetime import UTC
from datetime import datetime
from datetime import timedelta
import json
import math
from typing import Any
from typing import Literal

import httpx
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from pydantic import BaseModel
from pydantic import Field


FeedName = Literal["hourly", "monthly"]
SortOrder = Literal["time_desc", "magnitude_desc"]

USGS_FEED_URLS: dict[str, str] = {
    "hourly": (
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/"
        "all_hour.geojson"
    ),
    "monthly": (
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/"
        "all_month.geojson"
    ),
}
ARTIFACT_NAMES: dict[str, str] = {
    "hourly": "usgs-hourly.geojson",
    "monthly": "usgs-monthly.geojson",
}
CACHE_TTLS: dict[str, timedelta] = {
    "hourly": timedelta(minutes=5),
    "monthly": timedelta(hours=1),
}
USGS_EVENT_COUNT_URL = "https://earthquake.usgs.gov/fdsnws/event/1/count"
USGS_EVENT_QUERY_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
SEARCH_ARTIFACT_NAME = "usgs-search.geojson"
SEARCH_CACHE_TTL = timedelta(hours=1)
USGS_MAX_SEARCH_RESULTS = 20_000
MAX_RESPONSE_BYTES = 25 * 1024 * 1024
MAX_COUNT_RESPONSE_BYTES = 64 * 1024
MAX_QUERY_RESULTS = 500
SEARCH_PROVENANCE_KEY = "quake_agent"


class GeoBounds(BaseModel):
    """Geographic filter; west > east means the box crosses the antimeridian."""

    west: float = Field(ge=-180, le=180)
    south: float = Field(ge=-90, le=90)
    east: float = Field(ge=-180, le=180)
    north: float = Field(ge=-90, le=90)


class SearchCircle(BaseModel):
    """Center and radius for a USGS Event API circle search."""

    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    max_radius_km: float = Field(gt=0, le=20_001.6, allow_inf_nan=False)


class FeedDownloadError(RuntimeError):
    """A controlled failure while retrieving a USGS feed."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _epoch_ms_to_utc(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    try:
        return _format_utc(datetime.fromtimestamp(value / 1000, UTC))
    except (OverflowError, OSError, ValueError):
        return None


def _state_prefix(feed: str) -> str:
    return f"catalog_{feed}"


def _part_bytes(part: types.Part | None) -> bytes | None:
    if part is None:
        return None
    if part.inline_data and part.inline_data.data is not None:
        return bytes(part.inline_data.data)
    if part.text is not None:
        return part.text.encode("utf-8")
    return None


def _validate_catalog_bytes(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise FeedDownloadError(
            f"USGS response exceeds the {MAX_RESPONSE_BYTES}-byte limit."
        )
    try:
        catalog = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeedDownloadError("USGS response is not valid UTF-8 JSON.") from exc
    if not isinstance(catalog, dict) or catalog.get("type") != "FeatureCollection":
        raise FeedDownloadError("USGS response is not a GeoJSON FeatureCollection.")
    if not isinstance(catalog.get("features"), list):
        raise FeedDownloadError("USGS FeatureCollection has no feature array.")
    if not isinstance(catalog.get("metadata", {}), dict):
        raise FeedDownloadError("USGS FeatureCollection metadata is invalid.")
    return catalog


async def _request_usgs_once(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
) -> bytes:
    timeout = httpx.Timeout(10.0, connect=5.0)
    headers = {"Accept": "application/geo+json, application/json"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        async with client.stream(
            "GET", url, params=params, headers=headers
        ) as response:
            response.raise_for_status()
            length_header = response.headers.get("content-length")
            if length_header:
                try:
                    declared_length = int(length_header)
                except ValueError:
                    declared_length = 0
                if declared_length > max_response_bytes:
                    raise FeedDownloadError(
                        f"USGS response exceeds the {max_response_bytes}-byte limit."
                    )

            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_response_bytes:
                    raise FeedDownloadError(
                        f"USGS response exceeds the {max_response_bytes}-byte limit."
                    )
                chunks.append(chunk)
            return b"".join(chunks)


def _is_retryable_http_error(exc: httpx.HTTPStatusError) -> bool:
    return exc.response.status_code == 429 or exc.response.status_code >= 500


async def _fetch_usgs_response(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
) -> bytes:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            if params is None and max_response_bytes == MAX_RESPONSE_BYTES:
                return await _request_usgs_once(url)
            return await _request_usgs_once(
                url,
                params=params,
                max_response_bytes=max_response_bytes,
            )
        except FeedDownloadError:
            raise
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if not _is_retryable_http_error(exc) or attempt == 1:
                break
        except httpx.TransportError as exc:
            last_error = exc
            if attempt == 1:
                break
        if attempt == 0:
            await asyncio.sleep(0.2)
    raise FeedDownloadError(f"USGS feed request failed: {last_error}") from last_error


async def _fetch_usgs_bytes(
    url: str,
    *,
    params: dict[str, Any] | None = None,
) -> bytes:
    raw = await _fetch_usgs_response(url, params=params)
    _validate_catalog_bytes(raw)
    return raw


def _catalog_handle(
    *,
    feed: str,
    catalog: dict[str, Any],
    version: int,
    fetched_at: str,
    cached: bool,
    stale: bool,
    refresh_error: str | None = None,
) -> dict[str, Any]:
    metadata = catalog.get("metadata", {})
    generated_at = _epoch_ms_to_utc(metadata.get("generated"))
    result: dict[str, Any] = {
        "status": "ok",
        "feed": feed,
        "source_url": USGS_FEED_URLS[feed],
        "artifact_name": ARTIFACT_NAMES[feed],
        "artifact_version": version,
        "event_count": len(catalog["features"]),
        "source_generated_at": generated_at,
        "fetched_at": fetched_at,
        "bbox": catalog.get("bbox"),
        "cached": cached,
        "stale": stale,
    }
    if refresh_error:
        result["refresh_error"] = refresh_error
    return result


async def _load_catalog(
    tool_context: ToolContext,
    *,
    artifact_name: str,
    version: int,
) -> tuple[dict[str, Any], bytes] | None:
    part = await tool_context.load_artifact(artifact_name, version=version)
    raw = _part_bytes(part)
    if raw is None:
        return None
    return _validate_catalog_bytes(raw), raw


async def download_usgs_feed(
    feed: FeedName,
    force_refresh: bool = False,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Download and version an allowlisted USGS feed, with a bounded stale fallback.

    Args:
        feed: Either "hourly" for recent events or "monthly" for 30-day context.
        force_refresh: Ignore the freshness window and contact USGS now.

    Returns:
        A compact catalog handle with source, artifact version, timestamps,
        cache status, and staleness. The full GeoJSON is stored as an artifact.
    """
    if tool_context is None:
        return {"status": "error", "error": "Tool context is unavailable."}
    if feed not in USGS_FEED_URLS:
        return {"status": "error", "error": f"Unsupported feed: {feed!r}."}

    prefix = _state_prefix(feed)
    artifact_name = ARTIFACT_NAMES[feed]
    version_value = tool_context.state.get(f"{prefix}_version")
    fetched_at_value = tool_context.state.get(f"{prefix}_fetched_at")
    previous_version = version_value if isinstance(version_value, int) else None
    previous_fetched_at = (
        fetched_at_value if isinstance(fetched_at_value, str) else None
    )

    if not force_refresh and previous_version is not None:
        fetched_at = _parse_utc(previous_fetched_at)
        if fetched_at and _utc_now() - fetched_at <= CACHE_TTLS[feed]:
            try:
                loaded = await _load_catalog(
                    tool_context,
                    artifact_name=artifact_name,
                    version=previous_version,
                )
            except FeedDownloadError:
                loaded = None
            if loaded:
                catalog, _ = loaded
                return _catalog_handle(
                    feed=feed,
                    catalog=catalog,
                    version=previous_version,
                    fetched_at=previous_fetched_at or _format_utc(fetched_at),
                    cached=True,
                    stale=False,
                )

    try:
        raw = await _fetch_usgs_bytes(USGS_FEED_URLS[feed])
        catalog = _validate_catalog_bytes(raw)
        fetched_at = _format_utc(_utc_now())
        part = types.Part.from_bytes(data=raw, mime_type="application/geo+json")
        version = await tool_context.save_artifact(
            artifact_name,
            part,
            custom_metadata={
                "feed": feed,
                "source_url": USGS_FEED_URLS[feed],
                "fetched_at": fetched_at,
                "source_generated_at": _epoch_ms_to_utc(
                    catalog.get("metadata", {}).get("generated")
                ),
            },
        )
        tool_context.state[f"{prefix}_artifact"] = artifact_name
        tool_context.state[f"{prefix}_version"] = version
        tool_context.state[f"{prefix}_fetched_at"] = fetched_at
        return _catalog_handle(
            feed=feed,
            catalog=catalog,
            version=version,
            fetched_at=fetched_at,
            cached=False,
            stale=False,
        )
    except (FeedDownloadError, httpx.HTTPError, OSError, ValueError) as exc:
        if previous_version is not None:
            try:
                loaded = await _load_catalog(
                    tool_context,
                    artifact_name=artifact_name,
                    version=previous_version,
                )
            except (FeedDownloadError, OSError, ValueError):
                loaded = None
            if loaded:
                catalog, _ = loaded
                return _catalog_handle(
                    feed=feed,
                    catalog=catalog,
                    version=previous_version,
                    fetched_at=previous_fetched_at or "unknown",
                    cached=True,
                    stale=True,
                    refresh_error=str(exc),
                )
        return {
            "status": "error",
            "feed": feed,
            "source_url": USGS_FEED_URLS[feed],
            "error": str(exc),
            "cached": False,
            "stale": False,
        }


def _url_with_params(url: str, params: dict[str, Any]) -> str:
    return str(httpx.URL(url, params=params))


def _parse_usgs_count(raw: bytes) -> int:
    if len(raw) > MAX_COUNT_RESPONSE_BYTES:
        raise FeedDownloadError(
            f"USGS count response exceeds the {MAX_COUNT_RESPONSE_BYTES}-byte limit."
        )
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeedDownloadError("USGS count response is not valid JSON.") from exc
    count = payload.get("count") if isinstance(payload, dict) else None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise FeedDownloadError("USGS count response has no valid event count.")
    return count


async def _fetch_usgs_count(params: dict[str, Any]) -> int:
    raw = await _fetch_usgs_response(
        USGS_EVENT_COUNT_URL,
        params=params,
        max_response_bytes=MAX_COUNT_RESPONSE_BYTES,
    )
    return _parse_usgs_count(raw)


def _search_provenance(catalog: dict[str, Any]) -> dict[str, Any] | None:
    provenance = catalog.get(SEARCH_PROVENANCE_KEY)
    if not isinstance(provenance, dict):
        return None
    if provenance.get("kind") != "usgs_event_search":
        return None
    query = provenance.get("query")
    count_url = provenance.get("count_url")
    query_url = provenance.get("query_url")
    fetched_at = provenance.get("fetched_at")
    signature = provenance.get("query_signature")
    total_matched = provenance.get("total_matched")
    stored_count = provenance.get("stored_count")
    truncated = provenance.get("truncated")
    if (
        not isinstance(query, dict)
        or not isinstance(count_url, str)
        or not count_url.startswith(f"{USGS_EVENT_COUNT_URL}?")
        or not isinstance(query_url, str)
        or not query_url.startswith(f"{USGS_EVENT_QUERY_URL}?")
        or not isinstance(fetched_at, str)
        or _parse_utc(fetched_at) is None
        or not isinstance(signature, str)
        or not signature
        or isinstance(total_matched, bool)
        or not isinstance(total_matched, int)
        or total_matched < 0
        or isinstance(stored_count, bool)
        or not isinstance(stored_count, int)
        or stored_count < 0
        or not isinstance(truncated, bool)
        or stored_count != len(catalog.get("features", []))
    ):
        return None
    if truncated and not isinstance(provenance.get("truncation_notice"), str):
        return None
    return provenance


def _truncation_notice(total_matched: int, stored_count: int) -> str:
    return (
        f"Showing the strongest {stored_count:,} of {total_matched:,} matches; "
        "the result is truncated."
    )


def _search_catalog_handle(
    *,
    catalog: dict[str, Any],
    version: int,
    cached: bool,
    stale: bool,
    refresh_error: str | None = None,
) -> dict[str, Any]:
    provenance = _search_provenance(catalog)
    if provenance is None:
        raise FeedDownloadError("Stored historical search provenance is invalid.")
    result: dict[str, Any] = {
        "status": "ok",
        "catalog": "search",
        "source_url": provenance["query_url"],
        "count_url": provenance["count_url"],
        "artifact_name": SEARCH_ARTIFACT_NAME,
        "artifact_version": version,
        "event_count": len(catalog["features"]),
        "total_matched": provenance["total_matched"],
        "stored_count": provenance["stored_count"],
        "truncated": provenance["truncated"],
        "query": provenance["query"],
        "source_generated_at": _epoch_ms_to_utc(
            catalog.get("metadata", {}).get("generated")
        ),
        "fetched_at": provenance["fetched_at"],
        "cached": cached,
        "stale": stale,
    }
    notice = provenance.get("truncation_notice")
    if isinstance(notice, str):
        result["truncation_notice"] = notice
    if refresh_error:
        result["refresh_error"] = refresh_error
    return result


def _empty_search_result(
    *,
    query: dict[str, Any],
    count_url: str,
    query_url: str,
    fetched_at: str,
    cached: bool,
) -> dict[str, Any]:
    return {
        "status": "empty",
        "catalog": "search",
        "source_url": query_url,
        "count_url": count_url,
        "query": query,
        "total_matched": 0,
        "stored_count": 0,
        "truncated": False,
        "fetched_at": fetched_at,
        "cached": cached,
        "stale": False,
    }


async def search_usgs_events(
    start_time: str,
    end_time: str,
    min_magnitude: float | None = None,
    circle: SearchCircle | None = None,
    force_refresh: bool = False,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Fetch a bounded historical catalog from the official USGS Event API.

    Args:
        start_time: Required inclusive ISO-8601 UTC lower bound.
        end_time: Required inclusive ISO-8601 UTC upper bound.
        min_magnitude: Optional inclusive minimum magnitude.
        circle: Optional center and radius; omit for a worldwide search.
        force_refresh: Ignore an identical cached search and contact USGS now.

    Returns:
        A compact versioned search-artifact handle. Searches over 20,000
        matches contain the strongest 20,000 events and are marked truncated.
    """
    if tool_context is None:
        return {"status": "error", "error": "Tool context is unavailable."}
    start = _parse_utc(start_time)
    end = _parse_utc(end_time)
    if start is None:
        return {"status": "error", "error": "start_time is not valid ISO-8601."}
    if end is None:
        return {"status": "error", "error": "end_time is not valid ISO-8601."}
    if start > end:
        return {"status": "error", "error": "start_time must not exceed end_time."}
    if min_magnitude is not None and (
        isinstance(min_magnitude, bool)
        or not isinstance(min_magnitude, (int, float))
        or not math.isfinite(min_magnitude)
    ):
        return {"status": "error", "error": "min_magnitude must be finite."}

    canonical_start = _format_utc(start)
    canonical_end = _format_utc(end)
    normalized_magnitude = (
        float(min_magnitude) if min_magnitude is not None else None
    )
    normalized_circle = circle.model_dump() if circle is not None else None
    query = {
        "start_time": canonical_start,
        "end_time": canonical_end,
        "min_magnitude": normalized_magnitude,
        "circle": normalized_circle,
        "event_type": "earthquake",
    }
    signature = json.dumps(query, sort_keys=True, separators=(",", ":"))
    api_params: dict[str, Any] = {
        "format": "geojson",
        "starttime": canonical_start,
        "endtime": canonical_end,
        "eventtype": "earthquake",
    }
    if normalized_magnitude is not None:
        api_params["minmagnitude"] = normalized_magnitude
    if normalized_circle is not None:
        api_params.update(
            {
                "latitude": normalized_circle["latitude"],
                "longitude": normalized_circle["longitude"],
                "maxradiuskm": normalized_circle["max_radius_km"],
            }
        )
    count_url = _url_with_params(USGS_EVENT_COUNT_URL, api_params)

    prefix = _state_prefix("search")
    version_value = tool_context.state.get(f"{prefix}_version")
    fetched_at_value = tool_context.state.get(f"{prefix}_fetched_at")
    signature_value = tool_context.state.get(f"{prefix}_signature")
    previous_version = version_value if isinstance(version_value, int) else None
    previous_fetched_at = (
        fetched_at_value if isinstance(fetched_at_value, str) else None
    )
    identical_previous = signature_value == signature

    if not force_refresh and identical_previous:
        fetched_at = _parse_utc(previous_fetched_at)
        if fetched_at and _utc_now() - fetched_at <= SEARCH_CACHE_TTL:
            if tool_context.state.get(f"{prefix}_empty") is True:
                query_url = _url_with_params(USGS_EVENT_QUERY_URL, api_params)
                return _empty_search_result(
                    query=query,
                    count_url=count_url,
                    query_url=query_url,
                    fetched_at=previous_fetched_at or _format_utc(fetched_at),
                    cached=True,
                )
            if previous_version is not None:
                try:
                    loaded = await _load_catalog(
                        tool_context,
                        artifact_name=SEARCH_ARTIFACT_NAME,
                        version=previous_version,
                    )
                except FeedDownloadError:
                    loaded = None
                if loaded:
                    catalog, _ = loaded
                    provenance = _search_provenance(catalog)
                    if provenance and provenance.get("query_signature") == signature:
                        return _search_catalog_handle(
                            catalog=catalog,
                            version=previous_version,
                            cached=True,
                            stale=False,
                        )

    try:
        total_matched = await _fetch_usgs_count(api_params)
        fetched_at = _format_utc(_utc_now())
        if total_matched == 0:
            query_url = _url_with_params(USGS_EVENT_QUERY_URL, api_params)
            tool_context.state[f"{prefix}_version"] = None
            tool_context.state[f"{prefix}_fetched_at"] = fetched_at
            tool_context.state[f"{prefix}_signature"] = signature
            tool_context.state[f"{prefix}_empty"] = True
            return _empty_search_result(
                query=query,
                count_url=count_url,
                query_url=query_url,
                fetched_at=fetched_at,
                cached=False,
            )

        truncated = total_matched > USGS_MAX_SEARCH_RESULTS
        query_params = {
            **api_params,
            "limit": min(total_matched, USGS_MAX_SEARCH_RESULTS),
            "orderby": "magnitude",
        }
        query_url = _url_with_params(USGS_EVENT_QUERY_URL, query_params)
        raw = await _fetch_usgs_bytes(USGS_EVENT_QUERY_URL, params=query_params)
        catalog = _validate_catalog_bytes(raw)
        stored_count = len(catalog["features"])
        notice = (
            _truncation_notice(total_matched, stored_count) if truncated else None
        )
        provenance: dict[str, Any] = {
            "kind": "usgs_event_search",
            "query_signature": signature,
            "query": query,
            "count_url": count_url,
            "query_url": query_url,
            "fetched_at": fetched_at,
            "total_matched": total_matched,
            "stored_count": stored_count,
            "truncated": truncated,
            "truncation_order": "magnitude_desc" if truncated else None,
        }
        if notice:
            provenance["truncation_notice"] = notice
        catalog[SEARCH_PROVENANCE_KEY] = provenance
        stored_raw = json.dumps(
            catalog, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        if len(stored_raw) > MAX_RESPONSE_BYTES:
            raise FeedDownloadError(
                f"USGS response exceeds the {MAX_RESPONSE_BYTES}-byte limit "
                "after provenance is added."
            )
        version = await tool_context.save_artifact(
            SEARCH_ARTIFACT_NAME,
            types.Part.from_bytes(
                data=stored_raw, mime_type="application/geo+json"
            ),
            custom_metadata={
                "catalog": "search",
                "source_url": query_url,
                "fetched_at": fetched_at,
                "total_matched": total_matched,
                "stored_count": stored_count,
                "truncated": truncated,
            },
        )
        tool_context.state[f"{prefix}_artifact"] = SEARCH_ARTIFACT_NAME
        tool_context.state[f"{prefix}_version"] = version
        tool_context.state[f"{prefix}_fetched_at"] = fetched_at
        tool_context.state[f"{prefix}_signature"] = signature
        tool_context.state[f"{prefix}_empty"] = False
        return _search_catalog_handle(
            catalog=catalog,
            version=version,
            cached=False,
            stale=False,
        )
    except (FeedDownloadError, httpx.HTTPError, OSError, ValueError) as exc:
        if identical_previous and previous_version is not None:
            try:
                loaded = await _load_catalog(
                    tool_context,
                    artifact_name=SEARCH_ARTIFACT_NAME,
                    version=previous_version,
                )
            except (FeedDownloadError, OSError, ValueError):
                loaded = None
            if loaded:
                catalog, _ = loaded
                provenance = _search_provenance(catalog)
                if provenance and provenance.get("query_signature") == signature:
                    return _search_catalog_handle(
                        catalog=catalog,
                        version=previous_version,
                        cached=True,
                        stale=True,
                        refresh_error=str(exc),
                    )
        return {
            "status": "error",
            "catalog": "search",
            "source_url": USGS_EVENT_QUERY_URL,
            "count_url": count_url,
            "query": query,
            "error": str(exc),
            "cached": False,
            "stale": False,
        }


def _coord_from_feature(feature: dict[str, Any]) -> tuple[float, float, float | None] | None:
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        return None
    coords = geometry.get("coordinates")
    if not isinstance(coords, list) or len(coords) < 2:
        return None
    lon, lat = coords[0], coords[1]
    depth = coords[2] if len(coords) > 2 else None
    if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
        return None
    if not math.isfinite(lon) or not math.isfinite(lat):
        return None
    if lon < -180 or lon > 180 or lat < -90 or lat > 90:
        return None
    normalized_depth = (
        float(depth)
        if isinstance(depth, (int, float)) and math.isfinite(depth)
        else None
    )
    return float(lon), float(lat), normalized_depth


def _in_bounds(lon: float, lat: float, bounds: GeoBounds | None) -> bool:
    if bounds is None:
        return True
    if bounds.south > bounds.north or not bounds.south <= lat <= bounds.north:
        return False
    if bounds.west <= bounds.east:
        return bounds.west <= lon <= bounds.east
    return lon >= bounds.west or lon <= bounds.east


def _google_maps_url(lon: float, lat: float) -> str:
    coordinate = f"{lat},{lon}"
    return f"https://www.google.com/maps/place/{coordinate}/@{coordinate},6z/"


def _normalize_event(feature: dict[str, Any]) -> dict[str, Any] | None:
    coords = _coord_from_feature(feature)
    if coords is None:
        return None
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    event_id = feature.get("id")
    if not isinstance(event_id, str) or not event_id:
        return None
    magnitude = properties.get("mag")
    if not isinstance(magnitude, (int, float)) or not math.isfinite(magnitude):
        magnitude = None
    event_time_ms = properties.get("time")
    event_time = _epoch_ms_to_utc(event_time_ms)
    if event_time is None:
        return None
    significance = properties.get("sig")
    if not isinstance(significance, int):
        significance = None
    detail_url = properties.get("detail")
    if not isinstance(detail_url, str):
        detail_url = None
    place = properties.get("place")
    if not isinstance(place, str):
        place = None
    lon, lat, depth = coords
    return {
        "id": event_id,
        "coord": [lon, lat],
        "google_maps_url": _google_maps_url(lon, lat),
        "depth_km": depth,
        "magnitude": float(magnitude) if magnitude is not None else None,
        "place": place,
        "time": event_time,
        "time_ms": event_time_ms,
        "significance": significance,
        "detail_url": detail_url,
    }


def _select_catalog_events(
    catalog: dict[str, Any],
    *,
    min_magnitude: float | None,
    bounds: GeoBounds | None,
    start: datetime | None,
    end: datetime | None,
    sort: SortOrder,
) -> tuple[list[dict[str, Any]], int, int]:
    """Normalize, deduplicate, filter, and sort a loaded catalog in process."""
    deduplicated: dict[str, dict[str, Any]] = {}
    skipped = 0
    duplicates = 0
    for feature in catalog["features"]:
        if not isinstance(feature, dict):
            skipped += 1
            continue
        event = _normalize_event(feature)
        if event is None:
            skipped += 1
            continue
        if event["id"] in deduplicated:
            duplicates += 1
            if event["time_ms"] <= deduplicated[event["id"]]["time_ms"]:
                continue
        deduplicated[event["id"]] = event

    filtered: list[dict[str, Any]] = []
    for event in deduplicated.values():
        magnitude = event["magnitude"]
        if min_magnitude is not None and (
            magnitude is None or magnitude < min_magnitude
        ):
            continue
        lon, lat = event["coord"]
        if not _in_bounds(lon, lat, bounds):
            continue
        event_time = _parse_utc(event["time"])
        if event_time is None:
            continue
        if start and event_time < start:
            continue
        if end and event_time > end:
            continue
        filtered.append(event)

    if sort == "magnitude_desc":
        filtered.sort(
            key=lambda event: (
                event["magnitude"]
                if event["magnitude"] is not None
                else -math.inf,
                event["time_ms"],
            ),
            reverse=True,
        )
    else:
        filtered.sort(key=lambda event: event["time_ms"], reverse=True)
    return filtered, skipped, duplicates


async def query_usgs_feed(
    feed: FeedName,
    artifact_version: int | None = None,
    min_magnitude: float | None = None,
    bounds: GeoBounds | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    sort: SortOrder = "time_desc",
    limit: int = 200,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Query one stored USGS snapshot without contacting the network.

    Args:
        feed: The stored "hourly" or "monthly" catalog.
        artifact_version: Exact version to query; omit for the active version.
        min_magnitude: Optional inclusive minimum magnitude.
        bounds: Optional geographic bounds. West greater than east crosses the
            antimeridian.
        start_time: Optional inclusive ISO-8601 UTC lower bound.
        end_time: Optional inclusive ISO-8601 UTC upper bound.
        sort: Sort newest first or largest magnitude first.
        limit: Maximum returned events, from 1 through 500.

    Returns:
        Compact normalized events plus exact source artifact provenance.
    """
    if tool_context is None:
        return {"status": "error", "error": "Tool context is unavailable."}
    if feed not in ARTIFACT_NAMES:
        return {"status": "error", "error": f"Unsupported feed: {feed!r}."}
    if limit < 1 or limit > MAX_QUERY_RESULTS:
        return {
            "status": "error",
            "error": f"limit must be between 1 and {MAX_QUERY_RESULTS}.",
        }
    if sort not in ("time_desc", "magnitude_desc"):
        return {"status": "error", "error": f"Unsupported sort: {sort!r}."}
    if bounds is not None and bounds.south > bounds.north:
        return {"status": "error", "error": "bounds.south must not exceed north."}

    start = _parse_utc(start_time)
    end = _parse_utc(end_time)
    if start_time and start is None:
        return {"status": "error", "error": "start_time is not valid ISO-8601."}
    if end_time and end is None:
        return {"status": "error", "error": "end_time is not valid ISO-8601."}
    if start and end and start > end:
        return {"status": "error", "error": "start_time must not exceed end_time."}

    prefix = _state_prefix(feed)
    selected_version = artifact_version
    if selected_version is None:
        state_version = tool_context.state.get(f"{prefix}_version")
        selected_version = state_version if isinstance(state_version, int) else None
    if selected_version is None:
        return {
            "status": "error",
            "error": f"No stored {feed} catalog. Call download_usgs_feed first.",
        }

    try:
        loaded = await _load_catalog(
            tool_context,
            artifact_name=ARTIFACT_NAMES[feed],
            version=selected_version,
        )
    except (FeedDownloadError, OSError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}
    if not loaded:
        return {
            "status": "error",
            "error": f"Artifact version {selected_version} was not found.",
        }
    catalog, _ = loaded

    filtered, skipped, duplicates = _select_catalog_events(
        catalog,
        min_magnitude=min_magnitude,
        bounds=bounds,
        start=start,
        end=end,
        sort=sort,
    )

    returned = filtered[:limit]
    for event in returned:
        event.pop("time_ms", None)
    tool_context.state["active_event_ids"] = [event["id"] for event in returned]

    metadata = catalog.get("metadata", {})
    return {
        "status": "ok",
        "feed": feed,
        "source_url": USGS_FEED_URLS[feed],
        "artifact_name": ARTIFACT_NAMES[feed],
        "artifact_version": selected_version,
        "source_generated_at": _epoch_ms_to_utc(metadata.get("generated")),
        "total_catalog_events": len(catalog["features"]),
        "total_matched": len(filtered),
        "returned_count": len(returned),
        "skipped_invalid": skipped,
        "deduplicated_count": duplicates,
        "events": returned,
    }


async def query_usgs_search(
    artifact_version: int | None = None,
    min_magnitude: float | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    sort: SortOrder = "time_desc",
    limit: int = 200,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Query one stored USGS historical-search artifact without networking.

    Args:
        artifact_version: Exact search artifact version; omit for the active one.
        min_magnitude: Optional additional inclusive minimum magnitude.
        start_time: Optional additional inclusive ISO-8601 UTC lower bound.
        end_time: Optional additional inclusive ISO-8601 UTC upper bound.
        sort: Sort newest first or largest magnitude first.
        limit: Maximum returned events, from 1 through 500.

    Returns:
        Compact normalized events plus exact historical-search provenance.
    """
    if tool_context is None:
        return {"status": "error", "error": "Tool context is unavailable."}
    if limit < 1 or limit > MAX_QUERY_RESULTS:
        return {
            "status": "error",
            "error": f"limit must be between 1 and {MAX_QUERY_RESULTS}.",
        }
    if sort not in ("time_desc", "magnitude_desc"):
        return {"status": "error", "error": f"Unsupported sort: {sort!r}."}
    if min_magnitude is not None and (
        isinstance(min_magnitude, bool)
        or not isinstance(min_magnitude, (int, float))
        or not math.isfinite(min_magnitude)
    ):
        return {"status": "error", "error": "min_magnitude must be finite."}

    start = _parse_utc(start_time)
    end = _parse_utc(end_time)
    if start_time and start is None:
        return {"status": "error", "error": "start_time is not valid ISO-8601."}
    if end_time and end is None:
        return {"status": "error", "error": "end_time is not valid ISO-8601."}
    if start and end and start > end:
        return {"status": "error", "error": "start_time must not exceed end_time."}

    selected_version = artifact_version
    if selected_version is None:
        state_version = tool_context.state.get("catalog_search_version")
        selected_version = state_version if isinstance(state_version, int) else None
    if selected_version is None:
        return {
            "status": "error",
            "error": "No stored historical search. Call search_usgs_events first.",
        }

    try:
        loaded = await _load_catalog(
            tool_context,
            artifact_name=SEARCH_ARTIFACT_NAME,
            version=selected_version,
        )
    except (FeedDownloadError, OSError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}
    if not loaded:
        return {
            "status": "error",
            "error": f"Artifact version {selected_version} was not found.",
        }
    catalog, _ = loaded
    provenance = _search_provenance(catalog)
    if provenance is None:
        return {"status": "error", "error": "Search provenance is invalid."}

    filtered, skipped, duplicates = _select_catalog_events(
        catalog,
        min_magnitude=(
            float(min_magnitude) if min_magnitude is not None else None
        ),
        bounds=None,
        start=start,
        end=end,
        sort=sort,
    )
    returned = filtered[:limit]
    for event in returned:
        event.pop("time_ms", None)
    tool_context.state["active_event_ids"] = [event["id"] for event in returned]

    result: dict[str, Any] = {
        "status": "ok",
        "catalog": "search",
        "source_url": provenance["query_url"],
        "count_url": provenance["count_url"],
        "artifact_name": SEARCH_ARTIFACT_NAME,
        "artifact_version": selected_version,
        "source_generated_at": _epoch_ms_to_utc(
            catalog.get("metadata", {}).get("generated")
        ),
        "fetched_at": provenance["fetched_at"],
        "query": provenance["query"],
        "search_total_matched": provenance["total_matched"],
        "search_stored_count": provenance["stored_count"],
        "truncated": provenance["truncated"],
        "total_catalog_events": len(catalog["features"]),
        "total_matched": len(filtered),
        "returned_count": len(returned),
        "skipped_invalid": skipped,
        "deduplicated_count": duplicates,
        "events": returned,
    }
    notice = provenance.get("truncation_notice")
    if isinstance(notice, str):
        result["truncation_notice"] = notice
    return result
