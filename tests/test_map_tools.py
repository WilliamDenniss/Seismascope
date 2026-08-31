from __future__ import annotations

import hashlib
import inspect
from io import BytesIO
from pathlib import Path

from google.adk.tools import FunctionTool
from PIL import Image
from PIL import ImageDraw
import pytest

import quake_agent.tools.map_tools as map_tools
from quake_agent.tools.map_tools import MAP_SOURCES
from quake_agent.tools.map_tools import MINIMUM_CROP_LONG_EDGE_PX
from quake_agent.tools.map_tools import MapEvent
from quake_agent.tools.map_tools import MapLegend
from quake_agent.tools.map_tools import MapLegendItem
from quake_agent.tools.map_tools import WEB_MERCATOR_MAX_LAT
from quake_agent.tools.map_tools import _choose_legend_corner
from quake_agent.tools.map_tools import _select_map_source
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


@pytest.mark.parametrize(
    ("width", "height", "source_index"),
    [
        (1400, 100, 0),
        (700, 100, 1),
        (350, 100, 2),
        (349, 100, 2),
    ],
)
def test_map_source_selection_uses_long_edge_boundaries(
    width: int,
    height: int,
    source_index: int,
) -> None:
    assert [size for _, size in MAP_SOURCES] == [
        (2048, 2048),
        (4096, 4096),
        (8192, 8192),
    ]
    assert _select_map_source({"width": width, "height": height}) == MAP_SOURCES[
        source_index
    ]


def test_threshold_arguments_are_not_exposed_by_the_renderer() -> None:
    parameters = inspect.signature(render_events_on_world_map).parameters
    assert "high_resolution_crop_threshold_percent" not in parameters
    assert "two_x_crop_threshold_percent" not in parameters


def test_renderer_schema_exposes_optional_agent_defined_legend() -> None:
    declaration = FunctionTool(render_events_on_world_map)._get_declaration()
    schema = declaration.parameters_json_schema

    assert schema is not None
    assert "legend" not in schema["required"]
    assert schema["properties"]["legend"]["default"] is None
    assert schema["$defs"]["MapLegend"]["required"] == ["items"]
    assert schema["$defs"]["MapLegendItem"]["required"] == ["label", "color"]


def test_legend_corner_selection_avoids_content_and_breaks_ties_bottom_right() -> None:
    transparent = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    corner, box = _choose_legend_corner(transparent, (20, 20), 5)
    assert corner == "bottom_right"
    assert box == (75, 75, 95, 95)
    corner, box = _choose_legend_corner(
        transparent,
        (20, 20),
        5,
        bottom_clearance=10,
    )
    assert corner == "bottom_right"
    assert box == (75, 65, 95, 85)
    assert map_tools._legend_display_text("M 2.0–2.9") == "M 2.0-2.9"

    occupied = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    draw = ImageDraw.Draw(occupied)
    draw.rectangle((75, 75, 95, 95), fill=(255, 0, 0, 255))
    corner, _ = _choose_legend_corner(occupied, (20, 20), 5)
    assert corner == "bottom_left"

    every_corner = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    draw = ImageDraw.Draw(every_corner)
    draw.rectangle((75, 75, 95, 95), fill=(255, 0, 0, 255))
    draw.rectangle((5, 75, 20, 95), fill=(255, 0, 0, 255))
    draw.rectangle((75, 5, 85, 25), fill=(255, 0, 0, 255))
    draw.rectangle((5, 5, 10, 25), fill=(255, 0, 0, 255))
    corner, _ = _choose_legend_corner(every_corner, (20, 20), 5)
    assert corner == "top_left"


async def test_render_saves_versioned_png_and_spec_without_mutating_source(
    artifact_context,
) -> None:
    standard_map_path, _ = MAP_SOURCES[0]
    source_hash = hashlib.sha256(standard_map_path.read_bytes()).hexdigest()
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
    assert hashlib.sha256(standard_map_path.read_bytes()).hexdigest() == source_hash

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


@pytest.mark.parametrize("title", ["Magnitude", None])
async def test_render_persists_titled_and_untitled_legends_without_changing_viewport(
    artifact_context,
    title: str | None,
) -> None:
    events = [
        MapEvent(
            coord=[0, 0],
            label="Equator",
            latitude_radius=2,
            color="#facc15",
        )
    ]
    plain = await render_events_on_world_map(
        events,
        artifact_name="legend-map.png",
        tool_context=artifact_context,
    )
    with_legend = await render_events_on_world_map(
        events,
        artifact_name="legend-map.png",
        legend=MapLegend(
            title=title,
            items=[
                MapLegendItem(label="M 2.0–2.9", color="#facc15"),
                MapLegendItem(label="M 3.0+", color="#dc2626"),
            ],
        ),
        tool_context=artifact_context,
    )

    assert plain["legend"] is None
    assert with_legend["width"] == plain["width"]
    assert with_legend["height"] == plain["height"]
    assert with_legend["bounds"] == plain["bounds"]
    assert with_legend["crop"] == plain["crop"]
    assert with_legend["legend"] == {
        "title": title,
        "items": [
            {"label": "M 2.0–2.9", "color": "#facc15"},
            {"label": "M 3.0+", "color": "#dc2626"},
        ],
        "corner": "bottom_right",
    }

    loaded = await load_current_map_spec(tool_context=artifact_context)
    assert loaded["spec"]["legend"] == with_legend["legend"]
    historical = await load_current_map_spec(
        artifact_version=0,
        tool_context=artifact_context,
    )
    assert historical["spec"]["legend"] is None

    map_part = await artifact_context.load_artifact("legend-map.png", version=1)
    assert map_part.inline_data is not None
    with Image.open(BytesIO(bytes(map_part.inline_data.data))) as image:
        assert image.size == (plain["width"], plain["height"])


async def test_saved_legend_survives_a_follow_up_revision(artifact_context) -> None:
    events = [
        MapEvent(
            coord=[-122.3, 37.8],
            label="California",
            latitude_radius=2,
            color="#f97316",
        )
    ]
    first = await render_events_on_world_map(
        events,
        legend=MapLegend(
            title="Magnitude",
            items=[MapLegendItem(label="M 3.0–3.9", color="#f97316")],
        ),
        tool_context=artifact_context,
    )
    loaded = await load_current_map_spec(tool_context=artifact_context)
    saved_legend = loaded["spec"]["legend"]

    revised = await render_events_on_world_map(
        [MapEvent(**loaded["spec"]["events"][0])],
        legend=MapLegend(
            title=saved_legend["title"],
            items=[MapLegendItem(**item) for item in saved_legend["items"]],
        ),
        tool_context=artifact_context,
    )

    assert first["legend"] == revised["legend"]
    assert revised["map_artifact_version"] == 1
    assert revised["spec_artifact_version"] == 1


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
    assert rendered["crop"]["minimum_long_edge_px"] == 1400
    assert rendered["crop"]["minimum_long_edge_satisfied"] is False
    assert rendered["source_map"] == "static/world_map_4x.png"
    assert rendered["width"] < 400
    assert rendered["height"] < 400
    assert any("1400-pixel target" in warning for warning in rendered["warnings"])
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
    assert loaded["spec"]["source"]["scale"] == 4
    assert "area_percent_of_world" not in loaded["spec"]["crop"]
    assert "high_resolution_threshold_percent" not in loaded["spec"]["crop"]
    assert "two_x_threshold_percent" not in loaded["spec"]["crop"]
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
    assert rendered["source_map"] == "static/world_map_4x.png"
    assert rendered["bounds"]["west"] > rendered["bounds"]["east"]


async def test_legend_does_not_change_scaled_antimeridian_crop(artifact_context) -> None:
    events = [
        MapEvent(
            coord=[179.5, 0],
            label="",
            latitude_radius=4,
            color="royalblue",
        )
    ]
    plain = await render_events_on_world_map(
        events,
        artifact_name="plain-crop.png",
        crop_to_drawn_area=True,
        crop_padding_px=8,
        tool_context=artifact_context,
    )
    with_legend = await render_events_on_world_map(
        events,
        artifact_name="legend-crop.png",
        crop_to_drawn_area=True,
        crop_padding_px=8,
        legend=MapLegend(
            items=[
                MapLegendItem(label="Low", color="royalblue"),
                MapLegendItem(label="High", color="#dc2626"),
            ]
        ),
        tool_context=artifact_context,
    )

    assert plain["source_map"] == "static/world_map_4x.png"
    assert with_legend["source_map"] == plain["source_map"]
    assert with_legend["width"] == plain["width"]
    assert with_legend["height"] == plain["height"]
    assert with_legend["bounds"] == plain["bounds"]
    assert with_legend["crop"] == plain["crop"]
    assert with_legend["crop"]["wraps_antimeridian"] is True
    assert with_legend["legend"] is not None


async def test_crop_uses_two_x_map_when_it_reaches_minimum_long_edge(
    artifact_context,
) -> None:
    event = MapEvent(
        coord=[0, 0],
        label="",
        latitude_radius=50,
        color="#00aa00",
    )
    rendered = await render_events_on_world_map(
        [event],
        crop_to_drawn_area=True,
        tool_context=artifact_context,
    )

    assert rendered["status"] == "ok"
    assert rendered["source_map"] == "static/world_map_2x.png"
    assert rendered["crop"]["source_padding_px"] == 64
    assert max(rendered["width"], rendered["height"]) >= 1400
    assert rendered["crop"]["minimum_long_edge_satisfied"] is True


async def test_crop_uses_four_x_map_when_two_x_is_still_too_small(
    artifact_context,
) -> None:
    rendered = await render_events_on_world_map(
        [
            MapEvent(
                coord=[0, 0],
                label="",
                latitude_radius=35,
                color="#00aa00",
            )
        ],
        crop_to_drawn_area=True,
        tool_context=artifact_context,
    )

    assert rendered["status"] == "ok"
    assert rendered["source_map"] == "static/world_map_4x.png"
    assert max(rendered["width"], rendered["height"]) >= 1400
    assert rendered["crop"]["minimum_long_edge_satisfied"] is True


async def test_349_pixel_crop_uses_four_x_map_and_warns_target_is_unmet(
    artifact_context,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        map_tools,
        "_content_crop",
        lambda _overlay, _padding: {
            "source_x": 850,
            "source_y": 974,
            "width": 349,
            "height": 100,
            "wraps_antimeridian": False,
        },
    )
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
        tool_context=artifact_context,
    )

    assert rendered["status"] == "ok"
    assert rendered["source_map"] == "static/world_map_4x.png"
    assert (rendered["width"], rendered["height"]) == (1396, 400)
    assert rendered["crop"]["minimum_long_edge_satisfied"] is False
    assert any("1400-pixel target" in warning for warning in rendered["warnings"])


async def test_non_square_crop_uses_standard_map_when_long_edge_meets_minimum(
    artifact_context,
) -> None:
    rendered = await render_events_on_world_map(
        [
            MapEvent(
                coord=[longitude, 0],
                label="",
                latitude_radius=2,
                color="#00aa00",
            )
            for longitude in (-120, 0, 120)
        ],
        crop_to_drawn_area=True,
        tool_context=artifact_context,
    )

    assert rendered["status"] == "ok"
    assert rendered["source_map"] == "static/world_map.png"
    assert rendered["width"] >= MINIMUM_CROP_LONG_EDGE_PX
    assert rendered["height"] < MINIMUM_CROP_LONG_EDGE_PX
    assert rendered["crop"]["minimum_long_edge_satisfied"] is True
    assert not any("largest available" in warning for warning in rendered["warnings"])


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


@pytest.mark.parametrize(
    ("legend", "error_fragment"),
    [
        (MapLegend(items=[]), "at least one item"),
        (
            MapLegend(items=[MapLegendItem(label="   ", color="red")]),
            "non-blank label",
        ),
        (
            MapLegend(
                items=[
                    MapLegendItem(label="Invalid", color="definitely-not-a-color")
                ]
            ),
            "color 'definitely-not-a-color' is invalid",
        ),
        (
            MapLegend(items=[MapLegendItem(label="x" * 1000, color="red")]),
            "does not fit within the rendered map",
        ),
    ],
)
async def test_invalid_or_oversized_legend_does_not_save_artifacts(
    artifact_context,
    legend: MapLegend,
    error_fragment: str,
) -> None:
    rendered = await render_events_on_world_map(
        [MapEvent(coord=[0, 0], label="", latitude_radius=2, color="red")],
        legend=legend,
        tool_context=artifact_context,
    )

    assert rendered["status"] == "error"
    assert error_fragment in rendered["error"]
    assert await artifact_context.list_versions("earthquake-map.png") == []
    assert await artifact_context.list_versions("earthquake-map-spec.json") == []
