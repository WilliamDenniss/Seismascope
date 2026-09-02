from __future__ import annotations

import hashlib
import inspect
from io import BytesIO
import json
from pathlib import Path

from google.adk.tools import FunctionTool
from google.genai import types
from PIL import Image
from PIL import ImageDraw
import pytest

import quake_agent.tools.map_tools as map_tools
from quake_agent.tools.map_tools import DENSE_MAP_MAGNITUDE_LEGEND
from quake_agent.tools.map_tools import MAP_SOURCES
from quake_agent.tools.map_tools import MINIMUM_CROP_LONG_EDGE_PX
from quake_agent.tools.map_tools import MapCaption
from quake_agent.tools.map_tools import MapEvent
from quake_agent.tools.map_tools import MapLegend
from quake_agent.tools.map_tools import MapLegendItem
from quake_agent.tools.map_tools import MapSource
from quake_agent.tools.map_tools import MapTile
from quake_agent.tools.map_tools import WEB_MERCATOR_MAX_LAT
from quake_agent.tools.map_tools import _choose_legend_corner
from quake_agent.tools.map_tools import _select_map_source
from quake_agent.tools.map_tools import _wrapped_circle_centers
from quake_agent.tools.map_tools import latitude_radius_to_pixels
from quake_agent.tools.map_tools import load_current_map_spec
from quake_agent.tools.map_tools import project_web_mercator
from quake_agent.tools.map_tools import plot_data_points_on_map
from quake_agent.tools.map_tools import plot_usgs_feed_on_map
from quake_agent.tools.map_tools import plot_usgs_search_on_map
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
        (349, 100, 3),
        (175, 100, 3),
        (174, 100, 3),
    ],
)
def test_map_source_selection_uses_long_edge_boundaries(
    width: int,
    height: int,
    source_index: int,
) -> None:
    assert [source.size for source in MAP_SOURCES] == [
        (2048, 2048),
        (4096, 4096),
        (8192, 8192),
        (16384, 16384),
    ]
    assert _select_map_source({"width": width, "height": height}) == MAP_SOURCES[
        source_index
    ]


def test_eight_x_source_uses_four_named_quadrants() -> None:
    source = MAP_SOURCES[-1]

    assert source.artifact == "static/world_map_8x"
    assert {
        tile.name: (tile.path.name, tile.box)
        for tile in source.tiles
    } == {
        "nw": ("world_map_8x_nw.png", (0, 0, 8192, 8192)),
        "ne": ("world_map_8x_ne.png", (8192, 0, 16384, 8192)),
        "sw": ("world_map_8x_sw.png", (0, 8192, 8192, 16384)),
        "se": ("world_map_8x_se.png", (8192, 8192, 16384, 16384)),
    }
    for tile in source.tiles:
        with Image.open(tile.path) as image:
            assert image.size == (8192, 8192)


def _quadrant_test_source(tmp_path: Path) -> MapSource:
    definitions = (
        ("nw", (0, 0, 4, 4), "red"),
        ("ne", (4, 0, 8, 4), "green"),
        ("sw", (0, 4, 4, 8), "blue"),
        ("se", (4, 4, 8, 8), "yellow"),
    )
    tiles = []
    for name, box, color in definitions:
        path = tmp_path / f"{name}.png"
        Image.new("RGB", (4, 4), color).save(path)
        tiles.append(MapTile(name, path, box))
    return MapSource("test/quadrants", (8, 8), tuple(tiles))


def test_tiled_crop_stitches_all_four_quadrants(tmp_path: Path) -> None:
    cropped = map_tools._crop_map_source(
        _quadrant_test_source(tmp_path),
        {
            "source_x": 3,
            "source_y": 3,
            "width": 2,
            "height": 2,
            "wraps_antimeridian": False,
        },
    ).convert("RGB")

    assert cropped.getpixel((0, 0)) == (255, 0, 0)
    assert cropped.getpixel((1, 0)) == (0, 128, 0)
    assert cropped.getpixel((0, 1)) == (0, 0, 255)
    assert cropped.getpixel((1, 1)) == (255, 255, 0)


def test_tiled_crop_stitches_antimeridian_in_viewport_order(tmp_path: Path) -> None:
    cropped = map_tools._crop_map_source(
        _quadrant_test_source(tmp_path),
        {
            "source_x": 7,
            "source_y": 1,
            "width": 2,
            "height": 2,
            "wraps_antimeridian": True,
        },
    ).convert("RGB")

    assert cropped.getpixel((0, 0)) == (0, 128, 0)
    assert cropped.getpixel((1, 0)) == (255, 0, 0)


def test_tiled_crop_does_not_require_nonintersecting_tiles(tmp_path: Path) -> None:
    nw_path = tmp_path / "nw.png"
    Image.new("RGB", (4, 4), "red").save(nw_path)
    source = MapSource(
        "test/lazy-quadrants",
        (8, 8),
        (
            MapTile("nw", nw_path, (0, 0, 4, 4)),
            MapTile("ne", tmp_path / "missing-ne.png", (4, 0, 8, 4)),
            MapTile("sw", tmp_path / "missing-sw.png", (0, 4, 4, 8)),
            MapTile("se", tmp_path / "missing-se.png", (4, 4, 8, 8)),
        ),
    )

    cropped = map_tools._crop_map_source(
        source,
        {
            "source_x": 1,
            "source_y": 1,
            "width": 2,
            "height": 2,
            "wraps_antimeridian": False,
        },
    ).convert("RGB")

    assert cropped.getpixel((0, 0)) == (255, 0, 0)


@pytest.mark.parametrize(
    ("marker_long_edge", "expected_padding"),
    [
        (0, 16),
        (8, 16),
        (32, 16),
        (100, 50),
        (192, 96),
        (400, 96),
    ],
)
def test_automatic_crop_padding_scales_with_marker_extent(
    marker_long_edge: int,
    expected_padding: int,
) -> None:
    marker_crop = (
        None
        if marker_long_edge == 0
        else {"width": marker_long_edge, "height": 1}
    )

    assert map_tools._automatic_crop_padding(marker_crop) == (
        expected_padding,
        marker_long_edge,
    )


def test_crop_upscaling_does_not_inflate_earthquake_symbols() -> None:
    image_bytes, spec, _warnings, _skipped = map_tools._render_map(
        [
            MapEvent(
                coord=[-122.24, 37.48],
                label="",
                latitude_radius=0.1,
                color="#ff00ff",
            )
        ],
        crop_to_drawn_area=True,
    )

    assert spec["crop"]["upscale_factor"] == 4
    with Image.open(BytesIO(image_bytes)) as image:
        rgb = image.convert("RGB")
        marker_pixels = [
            (x, y)
            for y in range(rgb.height)
            for x in range(rgb.width)
            if (
                rgb.getpixel((x, y))[0] > 240
                and rgb.getpixel((x, y))[1] < 20
                and rgb.getpixel((x, y))[2] > 240
            )
        ]

    assert marker_pixels
    marker_width = max(x for x, _ in marker_pixels) - min(
        x for x, _ in marker_pixels
    ) + 1
    marker_height = max(y for _, y in marker_pixels) - min(
        y for _, y in marker_pixels
    ) + 1
    assert marker_width <= 18
    assert marker_height <= 18


def test_pixel_framing_arguments_are_not_exposed_by_renderers() -> None:
    for renderer in (
        plot_data_points_on_map,
        plot_usgs_feed_on_map,
        plot_usgs_search_on_map,
    ):
        parameters = inspect.signature(renderer).parameters
        assert "crop_padding_px" not in parameters
        assert "high_resolution_crop_threshold_percent" not in parameters
        assert "two_x_crop_threshold_percent" not in parameters

        declaration = FunctionTool(renderer)._get_declaration()
        schema = declaration.parameters_json_schema
        assert schema is not None
        assert "crop_padding_px" not in schema["properties"]


def test_renderer_schema_exposes_optional_agent_defined_legend_and_caption() -> None:
    declaration = FunctionTool(plot_data_points_on_map)._get_declaration()
    schema = declaration.parameters_json_schema

    assert schema is not None
    assert "legend" not in schema["required"]
    assert schema["properties"]["legend"]["default"] is None
    assert schema["$defs"]["MapLegend"]["required"] == ["items"]
    assert schema["$defs"]["MapLegendItem"]["required"] == ["label", "color"]
    assert "caption" not in schema["required"]
    assert schema["properties"]["caption"]["default"] is None
    assert schema["$defs"]["MapCaption"]["required"] == ["title", "date"]


def test_catalog_renderer_schema_never_accepts_model_supplied_events() -> None:
    declaration = FunctionTool(plot_usgs_feed_on_map)._get_declaration()
    schema = declaration.parameters_json_schema

    assert schema is not None
    assert "feed" in schema["properties"]
    assert "artifact_version" in schema["properties"]
    assert "events" not in schema["properties"]
    assert "legend" not in schema["properties"]
    assert "caption" in schema["properties"]

    search_declaration = FunctionTool(plot_usgs_search_on_map)._get_declaration()
    search_schema = search_declaration.parameters_json_schema
    assert search_schema is not None
    assert "artifact_version" in search_schema["properties"]
    assert "events" not in search_schema["properties"]


async def test_historical_search_renderer_preserves_provenance_and_warning(
    artifact_context,
) -> None:
    catalog = json.loads(
        (Path(__file__).parent / "fixtures" / "usgs-sample.geojson").read_text()
    )
    notice = "Showing the strongest 7 of 20,001 matches; the result is truncated."
    catalog["quake_agent"] = {
        "kind": "usgs_event_search",
        "query_signature": "test-signature",
        "query": {
            "start_time": "2021-08-31T00:00:00Z",
            "end_time": "2026-08-31T00:00:00Z",
            "min_magnitude": 3.0,
            "circle": {
                "latitude": 35.6762,
                "longitude": 139.6503,
                "max_radius_km": 100.0,
            },
            "event_type": "earthquake",
        },
        "count_url": "https://earthquake.usgs.gov/fdsnws/event/1/count?test",
        "query_url": "https://earthquake.usgs.gov/fdsnws/event/1/query?test",
        "fetched_at": "2026-08-31T01:00:00Z",
        "total_matched": 20_001,
        "stored_count": 7,
        "truncated": True,
        "truncation_order": "magnitude_desc",
        "truncation_notice": notice,
    }
    version = await artifact_context.save_artifact(
        "usgs-search.geojson",
        types.Part.from_bytes(
            data=json.dumps(catalog).encode(), mime_type="application/geo+json"
        ),
    )
    artifact_context.state["catalog_search_version"] = version

    rendered = await plot_usgs_search_on_map(
        artifact_version=version,
        min_magnitude=3,
        crop_to_drawn_area=True,
        caption=MapCaption(title="Tokyo M5+", date="2021-08-31 to 2026-08-31"),
        tool_context=artifact_context,
    )

    assert rendered["status"] == "ok"
    assert rendered["truncated"] is True
    assert rendered["truncation_notice"] == notice
    assert notice in rendered["warnings"]
    spec_part = await artifact_context.load_artifact(
        rendered["spec_artifact_name"], version=rendered["spec_artifact_version"]
    )
    assert spec_part.inline_data is not None
    spec = json.loads(bytes(spec_part.inline_data.data))
    assert spec["events"] == []
    assert spec["event_source"]["kind"] == "usgs_historical_search_artifact"
    assert spec["event_source"]["query"] == catalog["quake_agent"]["query"]
    assert spec["event_source"]["truncation_notice"] == notice


async def test_catalog_renderer_maps_ten_thousand_events_from_artifact(
    artifact_context,
) -> None:
    event_count = 10_000
    generated = 1_786_000_000_000
    catalog = {
        "type": "FeatureCollection",
        "metadata": {"generated": generated, "count": event_count},
        "features": [
            {
                "type": "Feature",
                "id": f"event-{index}",
                "properties": {
                    "mag": (index % 60) / 10,
                    "place": f"Synthetic event {index}",
                    "time": generated - index * 1000,
                },
                "geometry": {
                    "type": "Point",
                    "coordinates": [
                        -179.75 + (index % 720) * 0.5,
                        -70 + (index % 281) * 0.5,
                        10,
                    ],
                },
            }
            for index in range(event_count)
        ],
    }
    catalog_version = await artifact_context.save_artifact(
        "usgs-monthly.geojson",
        types.Part.from_bytes(
            data=json.dumps(catalog).encode("utf-8"),
            mime_type="application/geo+json",
        ),
    )
    artifact_context.state["catalog_monthly_version"] = catalog_version

    rendered = await plot_usgs_feed_on_map(
        "monthly",
        artifact_version=catalog_version,
        caption=MapCaption(
            title="Worldwide Earthquakes",
            date="As of August 31, 2026",
        ),
        tool_context=artifact_context,
    )

    assert rendered["status"] == "ok"
    assert rendered["total_catalog_events"] == event_count
    assert rendered["total_matched"] == event_count
    assert rendered["rendered_count"] == event_count
    assert rendered["skipped_count"] == 0

    map_part = await artifact_context.load_artifact(
        rendered["map_artifact_name"],
        version=rendered["map_artifact_version"],
    )
    assert map_part.inline_data is not None
    with Image.open(BytesIO(bytes(map_part.inline_data.data))) as image:
        assert image.size == (rendered["width"], rendered["height"])
        assert rendered["width"] == 2048
        assert rendered["height"] > 2048
        assert rendered["map_viewport"] == {
            "x": 0,
            "y": rendered["caption"]["height_px"],
            "width": 2048,
            "height": 2048,
        }

    spec_part = await artifact_context.load_artifact(
        rendered["spec_artifact_name"],
        version=rendered["spec_artifact_version"],
    )
    assert spec_part.inline_data is not None
    spec_bytes = bytes(spec_part.inline_data.data)
    spec = json.loads(spec_bytes)
    assert len(spec_bytes) < 20_000
    assert spec["event_count"] == event_count
    assert spec["events"] == []
    assert spec["caption"] == rendered["caption"]
    assert spec["event_source"]["catalog_artifact_version"] == catalog_version
    assert spec["event_source"]["style"] == {
        "color": "magnitude_bins",
        "labels": "magnitude >= 6",
        "radius": "magnitude_scaled",
    }


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
    standard_map_path = MAP_SOURCES[0].tiles[0].path
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

    first = await plot_data_points_on_map(events, tool_context=artifact_context)
    second = await plot_data_points_on_map(events[:2], tool_context=artifact_context)

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


async def test_render_draws_larger_circles_after_smaller_circles(
    artifact_context,
    monkeypatch,
) -> None:
    radii: list[float] = []
    wrapped_circle_centers = map_tools._wrapped_circle_centers

    def record_draw_order(x: float, radius: float, width: int) -> list[float]:
        radii.append(radius)
        return wrapped_circle_centers(x, radius, width)

    monkeypatch.setattr(map_tools, "_wrapped_circle_centers", record_draw_order)

    rendered = await plot_data_points_on_map(
        [
            MapEvent(
                coord=[0, 0],
                label="Large",
                latitude_radius=4,
                color="red",
            ),
            MapEvent(
                coord=[10, 0],
                label="Small",
                latitude_radius=1,
                color="blue",
            ),
        ],
        tool_context=artifact_context,
    )

    assert rendered["status"] == "ok"
    assert len(radii) == 2
    assert radii[0] < radii[1]


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
    plain = await plot_data_points_on_map(
        events,
        artifact_name="legend-map.png",
        tool_context=artifact_context,
    )
    with_legend = await plot_data_points_on_map(
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
    first = await plot_data_points_on_map(
        events,
        legend=MapLegend(
            title="Magnitude",
            items=[MapLegendItem(label="M 3.0–3.9", color="#f97316")],
        ),
        tool_context=artifact_context,
    )
    loaded = await load_current_map_spec(tool_context=artifact_context)
    saved_legend = loaded["spec"]["legend"]

    revised = await plot_data_points_on_map(
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


async def test_render_embeds_and_persists_caption_without_changing_map_viewport(
    artifact_context,
) -> None:
    events = [
        MapEvent(
            coord=[-122.3, 37.8],
            label="California",
            latitude_radius=2,
            color="#f97316",
        )
    ]
    plain = await plot_data_points_on_map(
        events,
        artifact_name="caption-map.png",
        crop_to_drawn_area=True,
        tool_context=artifact_context,
    )
    captioned = await plot_data_points_on_map(
        events,
        artifact_name="caption-map.png",
        crop_to_drawn_area=True,
        caption=MapCaption(
            title="Northern California Earthquakes",
            date="August 31, 2026",
        ),
        tool_context=artifact_context,
    )

    assert plain["caption"] is None
    assert plain["map_viewport"] == {
        "x": 0,
        "y": 0,
        "width": plain["width"],
        "height": plain["height"],
    }
    assert captioned["caption"] == {
        "title": "Northern California Earthquakes",
        "date": "August 31, 2026",
        "placement": "top",
        "height_px": captioned["map_viewport"]["y"],
    }
    assert captioned["width"] == plain["width"]
    assert captioned["height"] == (
        plain["height"] + captioned["caption"]["height_px"]
    )
    assert captioned["map_viewport"] == {
        "x": 0,
        "y": captioned["caption"]["height_px"],
        "width": plain["width"],
        "height": plain["height"],
    }
    assert captioned["bounds"] == plain["bounds"]
    assert captioned["crop"] == plain["crop"]

    loaded = await load_current_map_spec(tool_context=artifact_context)
    assert loaded["spec"]["caption"] == captioned["caption"]
    assert loaded["spec"]["map_viewport"] == captioned["map_viewport"]
    map_part = await artifact_context.load_artifact("caption-map.png", version=1)
    assert map_part.inline_data is not None
    with Image.open(BytesIO(bytes(map_part.inline_data.data))) as image:
        assert image.size == (captioned["width"], captioned["height"])


async def test_saved_caption_can_be_preserved_in_a_follow_up_revision(
    artifact_context,
) -> None:
    first = await plot_data_points_on_map(
        [MapEvent(coord=[0, 0], label="Equator", latitude_radius=2, color="red")],
        caption=MapCaption(title="Equatorial Earthquakes", date="August 2026"),
        tool_context=artifact_context,
    )
    loaded = await load_current_map_spec(tool_context=artifact_context)
    saved_caption = loaded["spec"]["caption"]

    revised = await plot_data_points_on_map(
        [MapEvent(**loaded["spec"]["events"][0])],
        caption=MapCaption(
            title=saved_caption["title"],
            date=saved_caption["date"],
        ),
        tool_context=artifact_context,
    )

    assert revised["caption"]["title"] == first["caption"]["title"]
    assert revised["caption"]["date"] == first["caption"]["date"]


async def test_state_and_artifacts_survive_new_file_service_instance(tmp_path: Path) -> None:
    root = tmp_path / "persistent-artifacts"
    first_context = ArtifactContext(root)
    rendered = await plot_data_points_on_map(
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
    rendered = await plot_data_points_on_map(
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
    assert rendered["crop"]["requested"] is True
    assert rendered["crop"]["applied"] is True
    assert rendered["crop"]["padding_mode"] == "automatic"
    assert rendered["crop"]["padding_px"] == 16
    assert rendered["crop"]["source_padding_px"] == 128
    assert rendered["crop"]["marker_long_edge_px"] == 24
    assert rendered["crop"]["native_width"] == 448
    assert rendered["crop"]["native_height"] == 448
    assert rendered["crop"]["upscale_factor"] == pytest.approx(1400 / 448)
    assert rendered["crop"]["minimum_long_edge_px"] == 1400
    assert rendered["crop"]["minimum_long_edge_satisfied"] is True
    assert rendered["source_map"] == "static/world_map_8x"
    assert rendered["width"] == 1400
    assert rendered["height"] == 1400
    assert not any("1400-pixel target" in warning for warning in rendered["warnings"])
    assert rendered["bounds"]["west"] < 0 < rendered["bounds"]["east"]
    assert rendered["bounds"]["south"] < 0 < rendered["bounds"]["north"]

    map_part = await artifact_context.load_artifact("earthquake-map.png", version=0)
    assert map_part.inline_data is not None
    with Image.open(BytesIO(bytes(map_part.inline_data.data))) as image:
        assert image.size == (rendered["width"], rendered["height"])

    loaded = await load_current_map_spec(tool_context=artifact_context)
    assert loaded["spec"]["source"]["width"] == 16384
    assert loaded["spec"]["source"]["height"] == 16384
    assert loaded["spec"]["source"]["artifact"] == "static/world_map_8x"
    assert loaded["spec"]["source"]["scale"] == 8
    assert [tile["name"] for tile in loaded["spec"]["source"]["tiles"]] == [
        "nw",
        "ne",
        "sw",
        "se",
    ]
    assert "area_percent_of_world" not in loaded["spec"]["crop"]
    assert "high_resolution_threshold_percent" not in loaded["spec"]["crop"]
    assert "two_x_threshold_percent" not in loaded["spec"]["crop"]
    assert loaded["spec"]["crop"] == rendered["crop"]


async def test_labels_do_not_change_automatic_geographic_framing(
    artifact_context,
) -> None:
    unlabeled = await plot_data_points_on_map(
        [MapEvent(coord=[0, 0], label="", latitude_radius=2, color="red")],
        artifact_name="unlabeled-map.png",
        crop_to_drawn_area=True,
        tool_context=artifact_context,
    )
    labeled = await plot_data_points_on_map(
        [
            MapEvent(
                coord=[0, 0],
                label="A deliberately longer event label",
                latitude_radius=2,
                color="red",
            )
        ],
        artifact_name="labeled-map.png",
        crop_to_drawn_area=True,
        tool_context=artifact_context,
    )

    assert labeled["status"] == "ok"
    assert labeled["crop"]["marker_long_edge_px"] == unlabeled["crop"][
        "marker_long_edge_px"
    ]
    assert labeled["crop"]["padding_px"] == unlabeled["crop"]["padding_px"]
    assert labeled["crop"] == unlabeled["crop"]
    assert labeled["bounds"] == unlabeled["bounds"]


async def test_tight_redwood_city_extent_uses_local_automatic_framing(
    artifact_context,
) -> None:
    rendered = await plot_data_points_on_map(
        [
            MapEvent(
                coord=[-122.24, 37.48],
                label="Redwood City",
                latitude_radius=0.2,
                color="#06b6d4",
            ),
            MapEvent(
                coord=[-122.02, 37.26],
                label="M1.5 (Saratoga)",
                latitude_radius=0.1,
                color="#f97316",
            ),
            MapEvent(
                coord=[-122.5, 37.4],
                label="M1.3 (Coast)",
                latitude_radius=0.1,
                color="#f97316",
            ),
            MapEvent(
                coord=[-122.22, 37.38],
                label="M1.9 (Portola Valley)",
                latitude_radius=0.1,
                color="#f97316",
            ),
        ],
        crop_to_drawn_area=True,
        tool_context=artifact_context,
    )

    assert rendered["status"] == "ok"
    assert rendered["crop"]["padding_px"] == 16
    assert rendered["crop"]["upscale_factor"] == 4
    assert rendered["bounds"]["east"] - rendered["bounds"]["west"] < 7
    assert rendered["bounds"]["west"] < -122.24 < rendered["bounds"]["east"]
    assert not any(
        warning.startswith("Label for event") for warning in rendered["warnings"]
    )


async def test_crop_stitches_antimeridian_into_compact_output(artifact_context) -> None:
    rendered = await plot_data_points_on_map(
        [
            MapEvent(
                coord=[179.5, 0],
                label="",
                latitude_radius=4,
                color="royalblue",
            )
        ],
        crop_to_drawn_area=True,
        tool_context=artifact_context,
    )

    assert rendered["status"] == "ok"
    assert rendered["width"] == MINIMUM_CROP_LONG_EDGE_PX
    assert rendered["crop"]["padding_px"] == 23
    assert rendered["crop"]["marker_long_edge_px"] == 46
    assert rendered["crop"]["upscale_factor"] > 1
    assert rendered["crop"]["minimum_long_edge_satisfied"] is True
    assert rendered["crop"]["wraps_antimeridian"] is True
    assert rendered["source_map"] == "static/world_map_8x"
    assert rendered["bounds"]["west"] > rendered["bounds"]["east"]


async def test_legend_does_not_change_scaled_antimeridian_crop(
    artifact_context,
    monkeypatch,
) -> None:
    font_sizes: list[int] = []
    load_legend_font = map_tools._load_legend_font

    def track_legend_font_size(size: int):
        font_sizes.append(size)
        return load_legend_font(size)

    monkeypatch.setattr(map_tools, "_load_legend_font", track_legend_font_size)
    events = [
        MapEvent(
            coord=[179.5, 0],
            label="",
            latitude_radius=4,
            color="royalblue",
        )
    ]
    plain = await plot_data_points_on_map(
        events,
        artifact_name="plain-crop.png",
        crop_to_drawn_area=True,
        tool_context=artifact_context,
    )
    with_legend = await plot_data_points_on_map(
        events,
        artifact_name="legend-crop.png",
        crop_to_drawn_area=True,
        legend=DENSE_MAP_MAGNITUDE_LEGEND,
        tool_context=artifact_context,
    )

    assert plain["source_map"] == "static/world_map_8x"
    assert with_legend["source_map"] == plain["source_map"]
    assert with_legend["width"] == plain["width"]
    assert with_legend["height"] == plain["height"]
    assert with_legend["bounds"] == plain["bounds"]
    assert with_legend["crop"] == plain["crop"]
    assert with_legend["crop"]["wraps_antimeridian"] is True
    assert with_legend["legend"] is not None
    assert font_sizes == [map_tools._annotation_font_size((1400, 1400))]


async def test_scaled_crop_normalizes_label_size_to_final_canvas(
    artifact_context,
    monkeypatch,
) -> None:
    font_sizes: list[int] = []
    load_label_font = map_tools._load_label_font

    def track_label_font_size(size: int = 10):
        font_sizes.append(size)
        return load_label_font(size)

    monkeypatch.setattr(map_tools, "_load_label_font", track_label_font_size)
    rendered = await plot_data_points_on_map(
        [
            MapEvent(
                coord=[179.5, 0],
                label="Antimeridian event",
                latitude_radius=4,
                color="royalblue",
            )
        ],
        crop_to_drawn_area=True,
        tool_context=artifact_context,
    )

    expected_final_size = map_tools._annotation_font_size(
        (rendered["width"], rendered["height"])
    )
    assert rendered["source_map"] == "static/world_map_8x"
    assert rendered["crop"]["wraps_antimeridian"] is True
    assert font_sizes == [10, expected_final_size]
    assert expected_final_size < 40
    assert not any("final resolution" in warning for warning in rendered["warnings"])


async def test_crop_uses_two_x_map_when_it_reaches_minimum_long_edge(
    artifact_context,
) -> None:
    event = MapEvent(
        coord=[0, 0],
        label="",
        latitude_radius=50,
        color="#00aa00",
    )
    rendered = await plot_data_points_on_map(
        [event],
        crop_to_drawn_area=True,
        tool_context=artifact_context,
    )

    assert rendered["status"] == "ok"
    assert rendered["source_map"] == "static/world_map_2x.png"
    assert rendered["crop"]["padding_px"] == 96
    assert rendered["crop"]["source_padding_px"] == 192
    assert max(rendered["width"], rendered["height"]) >= 1400
    assert rendered["crop"]["minimum_long_edge_satisfied"] is True


async def test_crop_uses_four_x_map_when_two_x_is_still_too_small(
    artifact_context,
) -> None:
    rendered = await plot_data_points_on_map(
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


async def test_349_pixel_crop_uses_eight_x_source_without_enlargement(
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
    rendered = await plot_data_points_on_map(
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
    assert rendered["source_map"] == "static/world_map_8x"
    assert (rendered["width"], rendered["height"]) == (2792, 800)
    assert rendered["crop"]["native_width"] == 2792
    assert rendered["crop"]["native_height"] == 800
    assert rendered["crop"]["upscale_factor"] == 1
    assert rendered["crop"]["minimum_long_edge_satisfied"] is True
    assert not any("1400-pixel target" in warning for warning in rendered["warnings"])


async def test_non_square_crop_uses_standard_map_when_long_edge_meets_minimum(
    artifact_context,
) -> None:
    rendered = await plot_data_points_on_map(
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
    rendered = await plot_data_points_on_map(
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
    clamped = await plot_data_points_on_map(
        [MapEvent(coord=[190, 90], label="Clamped", latitude_radius=4, color="red")],
        tool_context=artifact_context,
    )
    unsafe = await plot_data_points_on_map(
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
    rendered = await plot_data_points_on_map(
        [MapEvent(coord=[0, 0], label="", latitude_radius=2, color="red")],
        legend=legend,
        tool_context=artifact_context,
    )

    assert rendered["status"] == "error"
    assert error_fragment in rendered["error"]
    assert await artifact_context.list_versions("earthquake-map.png") == []
    assert await artifact_context.list_versions("earthquake-map-spec.json") == []


@pytest.mark.parametrize(
    ("caption", "crop_to_drawn_area", "error_fragment"),
    [
        (
            MapCaption(title="   ", date="August 31, 2026"),
            False,
            "caption.title must be non-blank",
        ),
        (
            MapCaption(title="Earthquakes\nWorldwide", date="August 31, 2026"),
            False,
            "must each be a single line",
        ),
        (
            MapCaption(title="W" * 120, date="August 31, 2026"),
            True,
            "does not fit above the rendered map",
        ),
    ],
)
async def test_invalid_or_oversized_caption_does_not_save_artifacts(
    artifact_context,
    caption: MapCaption,
    crop_to_drawn_area: bool,
    error_fragment: str,
) -> None:
    rendered = await plot_data_points_on_map(
        [MapEvent(coord=[0, 0], label="", latitude_radius=0.1, color="red")],
        crop_to_drawn_area=crop_to_drawn_area,
        caption=caption,
        tool_context=artifact_context,
    )

    assert rendered["status"] == "error"
    assert error_fragment in rendered["error"]
    assert await artifact_context.list_versions("earthquake-map.png") == []
    assert await artifact_context.list_versions("earthquake-map-spec.json") == []
