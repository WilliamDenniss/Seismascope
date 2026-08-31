"""Download, persist, and query allowlisted USGS earthquake feeds."""

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
MAX_RESPONSE_BYTES = 25 * 1024 * 1024
MAX_QUERY_RESULTS = 500


class GeoBounds(BaseModel):
    """Geographic filter; west > east means the box crosses the antimeridian."""

    west: float = Field(ge=-180, le=180)
    south: float = Field(ge=-90, le=90)
    east: float = Field(ge=-180, le=180)
    north: float = Field(ge=-90, le=90)


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


async def _request_usgs_once(url: str) -> bytes:
    timeout = httpx.Timeout(10.0, connect=5.0)
    headers = {"Accept": "application/geo+json, application/json"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        async with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            length_header = response.headers.get("content-length")
            if length_header:
                try:
                    declared_length = int(length_header)
                except ValueError:
                    declared_length = 0
                if declared_length > MAX_RESPONSE_BYTES:
                    raise FeedDownloadError(
                        f"USGS response exceeds the {MAX_RESPONSE_BYTES}-byte limit."
                    )

            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise FeedDownloadError(
                        f"USGS response exceeds the {MAX_RESPONSE_BYTES}-byte limit."
                    )
                chunks.append(chunk)
            return b"".join(chunks)


def _is_retryable_http_error(exc: httpx.HTTPStatusError) -> bool:
    return exc.response.status_code == 429 or exc.response.status_code >= 500


async def _fetch_usgs_bytes(url: str) -> bytes:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            raw = await _request_usgs_once(url)
            _validate_catalog_bytes(raw)
            return raw
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
                event["magnitude"] if event["magnitude"] is not None else -math.inf,
                event["time_ms"],
            ),
            reverse=True,
        )
    else:
        filtered.sort(key=lambda event: event["time_ms"], reverse=True)

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
