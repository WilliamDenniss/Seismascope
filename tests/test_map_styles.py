"""Catalog color scales, matching legends, and reproducible revisions."""

from io import BytesIO
import json

from google.adk.tools import FunctionTool
from google.genai import types
from PIL import Image
import pytest

from quake_agent.tools import map_tools
from quake_agent.tools.data_tools import _magnitude_summary
from quake_agent.tools.map_styles import (
    CatalogMapStyle,
    band_legend_items,
    magnitude_color,
    resolve_catalog_style,
)


def test_default_gradient_keeps_changing_above_magnitude_four():
    style = resolve_catalog_style(None)
    assert len({magnitude_color(m, style) for m in [4, 5, 6, 7]}) == 4
    assert magnitude_color(-2, style) == magnitude_color(0, style)
    assert magnitude_color(12, style) == magnitude_color(9, style)
    assert magnitude_color(None, style) == "#6b7280"
    assert magnitude_color(float("nan"), style) == "#6b7280"


def test_custom_gradient_interpolates_and_preserves_stops():
    style = resolve_catalog_style({"color_stops": [
        {"magnitude": 4, "color": "black"},
        {"magnitude": 6, "color": "white"},
        {"magnitude": 9, "color": "red"},
    ]})
    assert magnitude_color(4, style) == "#000000"
    assert magnitude_color(5, style) == "#808080"
    assert magnitude_color(6, style) == "#ffffff"
    assert magnitude_color(9, style) == "#ff0000"
    assert resolve_catalog_style(style) == style


def test_band_boundaries_and_legend_share_exact_intervals():
    style = resolve_catalog_style({"mode": "bands", "bands": [
        {"upper_bound": 4, "color": "blue"},
        {"upper_bound": 6, "color": "orange"},
        {"upper_bound": None, "color": "red"},
    ]})
    items = band_legend_items(style)
    assert [item["label"] for item in items] == ["M < 4", "4 <= M < 6", "M >= 6"]
    for magnitude, index in [(-3, 0), (3.9999, 0), (4, 1), (5.9999, 1), (6, 2), (12, 2)]:
        assert magnitude_color(magnitude, style) == items[index]["color"]
    assert resolve_catalog_style(style) == style


@pytest.mark.parametrize("style", [
    {"min_magnitude": 4, "max_magnitude": 4},
    {"min_magnitude": float("nan")},
    {"max_magnitude": float("inf")},
    {"min_magnitude": True},
    {"palette": "not-a-palette"},
    {"unknown_color": "not-a-color"},
    {"unknown_color": "#ffffff00"},
    {"out_of_range": "discard"},
    {"typo": "heat"},
    {"color_stops": [{"magnitude": 4, "color": "red"}]},
    {"color_stops": [{"magnitude": 4, "color": "red"}, {"magnitude": 4, "color": "blue"}]},
    {"color_stops": [{"magnitude": 5, "color": "red"}, {"magnitude": 4, "color": "blue"}]},
    {"palette": "heat", "color_stops": [{"magnitude": 4, "color": "red"}, {"magnitude": 6, "color": "blue"}]},
    {"max_magnitude": 7, "color_stops": [{"magnitude": 4, "color": "red"}, {"magnitude": 6, "color": "blue"}]},
    {"mode": "bands"},
    {"mode": "bands", "bands": [{"upper_bound": 4, "color": "blue"}, {"upper_bound": 6, "color": "red"}]},
    {"mode": "bands", "bands": [{"upper_bound": None, "color": "blue"}, {"upper_bound": None, "color": "red"}]},
    {"mode": "bands", "bands": [{"upper_bound": 6, "color": "blue"}, {"upper_bound": 4, "color": "orange"}, {"color": "red"}]},
    {"mode": "bands", "palette": "heat", "bands": [{"upper_bound": 4, "color": "blue"}, {"color": "red"}]},
])
async def test_invalid_styles_fail_before_saving_artifacts(artifact_context, style):
    for renderer, args in [
        (map_tools.plot_usgs_feed_on_map, {"feed": "monthly"}),
        (map_tools.plot_usgs_search_on_map, {}),
    ]:
        result = await renderer(**args, style=style, tool_context=artifact_context)
        assert result["status"] == "error"
    assert await artifact_context.list_versions("earthquake-map.png") == []
    assert await artifact_context.list_versions("earthquake-map-spec.json") == []


@pytest.mark.parametrize("renderer", [map_tools.plot_usgs_feed_on_map, map_tools.plot_usgs_search_on_map])
def test_catalog_tool_schema_exposes_optional_style(renderer):
    schema = FunctionTool(renderer)._get_declaration().parameters_json_schema
    assert "style" in schema["properties"]
    assert "style" not in schema.get("required", [])
    assert "CatalogMapStyle" in schema["$defs"]
    assert "events" not in schema["properties"]


def test_magnitude_summary_counts_unknown_negative_and_large_values():
    summary = _magnitude_summary([{"magnitude": m} for m in [-1, 0, 4, 4.9, 9, None]])
    assert summary["event_count"] == 6
    assert summary["known_count"] == 5
    assert summary["unknown_count"] == 1
    assert summary["min_magnitude"] == -1
    assert summary["max_magnitude"] == 9
    assert summary["histogram"][0]["count"] == 1
    assert summary["histogram"][5]["count"] == 2
    assert summary["histogram"][-1]["count"] == 1
    assert sum(item["count"] for item in summary["histogram"]) == 5
    empty = _magnitude_summary([])
    assert empty["min_magnitude"] is None and empty["max_magnitude"] is None
    unknown = _magnitude_summary([{"magnitude": None}])
    assert unknown["known_count"] == 0 and unknown["unknown_count"] == 1


@pytest.mark.parametrize("source", ["feed", "search"])
@pytest.mark.parametrize("style", [
    {"palette": "viridis", "min_magnitude": 4, "max_magnitude": 8},
    {"mode": "bands", "bands": [{"upper_bound": 6, "color": "orange"}, {"color": "red"}]},
])
async def test_catalog_styles_and_legends_survive_filtered_revision(
    artifact_context, monkeypatch, tmp_path, source, style,
):
    base = tmp_path / "base.png"
    Image.new("RGB", (512, 512), "#e0e4e8").save(base)
    monkeypatch.setattr(map_tools, "MAP_SOURCES", [map_tools.MapSource(
        artifact="base.png", size=(512, 512),
        tiles=(map_tools.MapTile("world", base, (0, 0, 512, 512)),),
    )])
    catalog = {"type": "FeatureCollection", "metadata": {}, "features": [
        {"type": "Feature", "id": str(i),
         "properties": {"mag": magnitude, "time": 1786000000000 + i, "place": "Sample"},
         "geometry": {"type": "Point", "coordinates": [-120 + i * 50, 10, 0]}}
        for i, magnitude in enumerate([4, 5, 6, 7, None])
    ]}
    if source == "search":
        catalog["quake_agent"] = {
            "kind": "usgs_event_search", "query_signature": "style-test", "query": {},
            "count_url": "https://earthquake.usgs.gov/fdsnws/event/1/count?test",
            "query_url": "https://earthquake.usgs.gov/fdsnws/event/1/query?test",
            "fetched_at": "2026-09-01T00:00:00Z", "total_matched": 5,
            "stored_count": 5, "truncated": False,
        }
    name = "usgs-monthly.geojson" if source == "feed" else "usgs-search.geojson"
    version = await artifact_context.save_artifact(name, types.Part.from_bytes(
        data=json.dumps(catalog).encode(), mime_type="application/geo+json",
    ))
    renderer = map_tools.plot_usgs_feed_on_map if source == "feed" else map_tools.plot_usgs_search_on_map
    args = {"feed": "monthly"} if source == "feed" else {}
    captured = []
    original_render = map_tools._render_map

    def capture(events, **kwargs):
        captured.append(events)
        return original_render(events, **kwargs)

    monkeypatch.setattr(map_tools, "_render_map", capture)
    first = await renderer(**args, artifact_version=version, style=CatalogMapStyle(**style),
                           marker_scale=0.7, tool_context=artifact_context)
    assert first["status"] == "ok", first
    loaded = await map_tools.load_current_map_spec(tool_context=artifact_context)
    spec = loaded["spec"]
    saved = spec["event_source"]["style"]
    assert saved["color"] == first["style"] == resolve_catalog_style(style)
    assert spec["events"] == []
    for event, magnitude in zip(captured[0], [None, 7, 6, 5, 4]):
        assert event.color == magnitude_color(magnitude, saved["color"])
    assert first["legend"]["items"][-1] == {"label": "Unknown", "color": "#6b7280"}
    if saved["color"]["mode"] == "continuous":
        assert first["legend"]["scale"] == saved["color"]
    else:
        assert first["legend"]["items"][:-1] == band_legend_items(saved["color"])
    revised = await renderer(**args, artifact_version=version, min_magnitude=6,
                             style=CatalogMapStyle(**saved["color"]),
                             marker_scale=saved["radius"]["scale"], tool_context=artifact_context)
    assert revised["status"] == "ok", revised
    assert revised["style"] == first["style"]
    assert revised["marker_scale"] == first["marker_scale"]
    assert revised["magnitude_summary"]["event_count"] == 2
    for event, magnitude in zip(captured[1], [7, 6]):
        assert event.color == magnitude_color(magnitude, first["style"])
    part = await artifact_context.load_artifact(first["map_artifact_name"], first["map_artifact_version"])
    with Image.open(BytesIO(part.inline_data.data)) as image:
        assert image.size == (512, 512)
