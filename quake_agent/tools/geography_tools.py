"""Deterministic geographic calculations that do not resolve place names."""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel
from pydantic import Field


MEAN_EARTH_RADIUS_KM = 6_371.0088
KM_PER_MILE = 1.609344


class GeographicCoordinate(BaseModel):
    """A validated latitude/longitude coordinate in decimal degrees."""

    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)


def calculate_coordinate_distance(
    start: GeographicCoordinate,
    end: GeographicCoordinate,
) -> dict[str, Any]:
    """Calculate surface distance between two latitude/longitude coordinates.

    The result is the shortest great-circle distance on a mean-radius spherical
    Earth. It does not include elevation, earthquake depth, or travel routes.

    Args:
        start: Starting coordinate in decimal degrees.
        end: Ending coordinate in decimal degrees.

    Returns:
        Distance in kilometers and statute miles, plus calculation metadata.
    """
    start_lat = math.radians(start.latitude)
    end_lat = math.radians(end.latitude)
    latitude_delta = end_lat - start_lat
    longitude_delta = math.radians(end.longitude - start.longitude)

    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(start_lat)
        * math.cos(end_lat)
        * math.sin(longitude_delta / 2) ** 2
    )
    central_angle = 2 * math.asin(math.sqrt(min(1.0, max(0.0, haversine))))
    distance_km = MEAN_EARTH_RADIUS_KM * central_angle

    return {
        "status": "ok",
        "start_coord": [start.longitude, start.latitude],
        "end_coord": [end.longitude, end.latitude],
        "distance_km": round(distance_km, 3),
        "distance_miles": round(distance_km / KM_PER_MILE, 3),
        "calculation": "great_circle_haversine",
        "earth_model": "mean_radius_sphere",
        "earth_radius_km": MEAN_EARTH_RADIUS_KM,
        "includes_elevation_or_depth": False,
    }
