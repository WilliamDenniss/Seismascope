from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest

from quake_agent.tools.map_tools import BASE_MAP_PATH
from quake_agent.tools.map_tools import DEFAULT_HIGH_RESOLUTION_CROP_THRESHOLD_PERCENT
from quake_agent.tools.map_tools import HIGH_RESOLUTION_BASE_MAP_PATH
from quake_agent.tools.map_tools import MapEvent
from quake_agent.tools.map_tools import WEB_MERCATOR_MAX_LAT
from quake_agent.tools.map_tools import _wrapped_circle_centers
from quake_agent.tools.map_tools import latitude_radius_to_pixels
from quake_agent.tools.map_tools import load_current_map_spec
from quake_agent.tools.map_tools import project_web_mercator
from quake_agent.tools.map_tools import render_events_on_world_map
from conftest import ArtifactContext


def test_projection_known_points_and_latitude_scaling() -> None:
    assert project_web_mercator(-180, 0, 2048, 2048) == pytest.approx((0, 1024))
    assert project_web_mercator(0, 0, 2048, 2048) == pytest.approx((1024, 1024))
    assert project_web_mercator(180, 0, 2048, 2048) == pytest.approx((2048, 1024))
    assert project_web_mercator(0, WEB_MERCATOR_MAX_LAT, 2048, 2048)[1] == pytest.approx(0)
    assert project_web_mercator(0, -WEB_MERCATOR_MAX_LAT, 2048, 2048)[1] == pytest.approx(2048)
    assert latitude_radius_to_pixels(60, 1, 2048) > latitude_radius_to_pixels(
        0, 1, 2048
    )


def test_antimeridian_circle_centers_wrap_both_directions() -> None:
    assert _wrapped_circle_centers(3, 10, 100) == [3, 103]
    assert _wrapped_circle_centers(97, 10, 100) == [97, -3]
    assert _wrapped_circle_centers(50, 10, 100) == [50]


async def test_render_saves_versioned_png_and_spec_without_mutating_source(
    artifact_context,
) -> None:
    source_hash = hashlib.sha256(BASE_MAP_PATH.read_bytes()).hexdigest()
    events = [
        MapEvent(
            coord=[-122.3, 37.8],
            label="M4.5 Northern California",
            latitude_radius=2,
            color="#ef4444",
        ),
        MapEvent(
            coord=[179.5, 10],
            label="Antimeridian event",
            latitude_radius=4,
            color="royalblue",
        ),
        MapEvent(
            coord=[0, 0],
            label="Invalid color",
            latitude_radius=1,
            color="definitely-not-a-color",
        ),
    ]

    first = await render_events_on_world_map(events, tool_context=artifact_context)
    second = await render_events_on_world_map(events[:2], tool_context=artifact_context)

    assert first["status"] == "ok"
    assert first["rendered_count"] == 2
    assert first["skipped_count"] == 1
    assert first["map_artifact_version"] == 0
    assert first["spec_artifact_version"] == 0
    assert second["map_artifact_version"] == 1
    assert second["spec_artifact_version"] == 1
    assert hashlib.sha256(BASE_MAP_PATH.read_bytes()).hexdigest() == source_hash

    map_part = await artifact_context.load_artifact("earthquake-map.png", version=1)
    assert map_part.inline_data is not None
    with Image.open(BytesIO(bytes(map_part.inline_data.data))) as image:
        assert image.size == (2048, 2048)
        assert image.format == "PNG"

    loaded = await load_current_map_spec(tool_context=artifact_context)
    assert loaded["status"] == "ok"
    assert loaded["artifact_version"] == 1
    assert len(loaded["spec"]["events"]) == 2

    historical = await load_current_map_spec(
        artifact_version=0, tool_context=artifact_context
    )
    assert historical["status"] == "ok"
    assert historical["artifact_version"] == 0
    assert len(historical["spec"]["events"]) == 2


async def test_state_and_artifacts_survive_new_file_service_instance(tmp_path: Path) -> None:
    root = tmp_path / "persistent-artifacts"
    first_context = ArtifactContext(root)
    rendered = await render_events_on_world_map(
        [
            MapEvent(
                coord=[0, 0],
                label="Equator",
                latitude_radius=2,
                color="#00aa00",
            )
        ],
        tool_context=first_context,
    )
    assert rendered["status"] == "ok"

    restarted_context = ArtifactContext(root, state=dict(first_context.state))
    loaded = await load_current_map_spec(tool_context=restarted_context)
    assert loaded["status"] == "ok"
    assert loaded["spec"]["events"][0]["label"] == "Equator"


async def test_crop_to_drawn_area_records_dimensions_bounds_and_padding(
    artifact_context,
) -> None:
    rendered = await render_events_on_world_map(
        [
            MapEvent(
                coord=[0, 0],
                label="",
                latitude_radius=2,
                color="#00aa00",
            )
        ],
        crop_to_drawn_area=True,
        crop_padding_px=12,
        tool_context=artifact_context,
    )

    assert rendered["status"] == "ok"
    assert rendered["crop"]["requested"] is True
    assert rendered["crop"]["applied"] is True
    assert rendered["crop"]["padding_px"] == 12
    assert rendered["crop"]["source_padding_px"] == 48
    assert rendered["crop"]["used_high_resolution_source"] is True
    assert (
        rendered["crop"]["area_percent_of_world"]
        < DEFAULT_HIGH_RESOLUTION_CROP_THRESHOLD_PERCENT
    )
    assert rendered["source_map"] == "static/world_map_4x.png"
    assert rendered["width"] < 400
    assert rendered["height"] < 400
    assert rendered["bounds"]["west"] < 0 < rendered["bounds"]["east"]
    assert rendered["bounds"]["south"] < 0 < rendered["bounds"]["north"]

    map_part = await artifact_context.load_artifact("earthquake-map.png", version=0)
    assert map_part.inline_data is not None
    with Image.open(BytesIO(bytes(map_part.inline_data.data))) as image:
        assert image.size == (rendered["width"], rendered["height"])

    loaded = await load_current_map_spec(tool_context=artifact_context)
    assert loaded["spec"]["source"]["width"] == 8192
    assert loaded["spec"]["source"]["height"] == 8192
    assert loaded["spec"]["source"]["artifact"] == "static/world_map_4x.png"
    assert loaded["spec"]["crop"] == rendered["crop"]


async def test_crop_stitches_antimeridian_into_compact_output(artifact_context) -> None:
    rendered = await render_events_on_world_map(
        [
            MapEvent(
                coord=[179.5, 0],
                label="",
                latitude_radius=4,
                color="royalblue",
            )
        ],
        crop_to_drawn_area=True,
        crop_padding_px=8,
        tool_context=artifact_context,
    )

    assert rendered["status"] == "ok"
    assert rendered["width"] < 600
    assert rendered["crop"]["wraps_antimeridian"] is True
    assert rendered["crop"]["used_high_resolution_source"] is True
    assert rendered["bounds"]["west"] > rendered["bounds"]["east"]


async def test_zero_threshold_keeps_standard_map_for_small_crop(
    artifact_context,
) -> None:
    rendered = await render_events_on_world_map(
        [
            MapEvent(
                coord=[0, 0],
                label="",
                latitude_radius=2,
                color="#00aa00",
            )
        ],
        crop_to_drawn_area=True,
        high_resolution_crop_threshold_percent=0,
        tool_context=artifact_context,
    )

    assert rendered["status"] == "ok"
    assert rendered["source_map"] == "static/world_map.png"
    assert rendered["crop"]["used_high_resolution_source"] is False
    assert rendered["crop"]["high_resolution_threshold_percent"] == 0
    assert HIGH_RESOLUTION_BASE_MAP_PATH.is_file()


async def test_crop_with_no_drawable_content_keeps_full_map(artifact_context) -> None:
    rendered = await render_events_on_world_map(
        [],
        crop_to_drawn_area=True,
        tool_context=artifact_context,
    )

    assert rendered["status"] == "ok"
    assert rendered["width"] == 2048
    assert rendered["height"] == 2048
    assert rendered["crop"]["requested"] is True
    assert rendered["crop"]["applied"] is False
    assert any("no drawable content" in warning for warning in rendered["warnings"])


async def test_renderer_clamps_latitude_and_rejects_unsafe_artifact_name(
    artifact_context,
) -> None:
    clamped = await render_events_on_world_map(
        [MapEvent(coord=[190, 90], label="Clamped", latitude_radius=4, color="red")],
        tool_context=artifact_context,
    )
    unsafe = await render_events_on_world_map(
        [], artifact_name="../escape.png", tool_context=artifact_context
    )

    assert clamped["status"] == "ok"
    assert any("normalized" in warning for warning in clamped["warnings"])
    assert any("clamped" in warning for warning in clamped["warnings"])
    assert unsafe["status"] == "error"
