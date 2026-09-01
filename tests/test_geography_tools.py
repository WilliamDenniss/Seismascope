from __future__ import annotations

import pytest
from pydantic import ValidationError

from quake_agent.tools.geography_tools import GeographicCoordinate
from quake_agent.tools.geography_tools import calculate_coordinate_distance


def test_coordinate_distance_is_zero_for_identical_points() -> None:
    point = GeographicCoordinate(latitude=35.681236, longitude=139.767125)

    result = calculate_coordinate_distance(point, point)

    assert result["status"] == "ok"
    assert result["distance_km"] == 0
    assert result["distance_miles"] == 0
    assert result["includes_elevation_or_depth"] is False


def test_coordinate_distance_uses_shortest_antimeridian_path() -> None:
    west = GeographicCoordinate(latitude=0, longitude=179)
    east = GeographicCoordinate(latitude=0, longitude=-179)

    result = calculate_coordinate_distance(west, east)

    assert result["distance_km"] == pytest.approx(222.39, abs=0.001)
    assert result["calculation"] == "great_circle_haversine"
    assert result["earth_model"] == "mean_radius_sphere"


def test_coordinate_distance_preserves_longitude_latitude_result_order() -> None:
    start = GeographicCoordinate(latitude=0, longitude=0)
    end = GeographicCoordinate(latitude=0, longitude=1)

    result = calculate_coordinate_distance(start, end)

    assert result["start_coord"] == [0.0, 0.0]
    assert result["end_coord"] == [1.0, 0.0]
    assert result["distance_km"] == pytest.approx(111.195, abs=0.001)


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [
        (91, 0),
        (-91, 0),
        (0, 181),
        (0, -181),
        (float("nan"), 0),
        (0, float("inf")),
    ],
)
def test_coordinate_rejects_invalid_values(latitude: float, longitude: float) -> None:
    with pytest.raises(ValidationError):
        GeographicCoordinate(latitude=latitude, longitude=longitude)
