"""Render declarative event overlays onto the supplied world map."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import json
import math
from pathlib import Path
import re
from typing import Any

from google.adk.tools.tool_context import ToolContext
from google.genai import types
from PIL import Image
from PIL import ImageColor
from PIL import ImageDraw
from PIL import ImageFont
from pydantic import BaseModel
from pydantic import Field

from .data_tools import ARTIFACT_NAMES
from .data_tools import SEARCH_ARTIFACT_NAME
from .data_tools import USGS_FEED_URLS
from .data_tools import FeedDownloadError
from .data_tools import FeedName
from .data_tools import GeoBounds
from .data_tools import _epoch_ms_to_utc
from .data_tools import _load_catalog
from .data_tools import _parse_utc
from .data_tools import _search_provenance
from .data_tools import _select_catalog_events
from .data_tools import _state_prefix


WEB_MERCATOR_MAX_LAT = 85.05112878
_STATIC_MAP_DIR = Path(__file__).resolve().parents[2] / "static"


@dataclass(frozen=True)
class MapTile:
    """One rectangular image within a full-world map source."""

    name: str
    path: Path
    box: tuple[int, int, int, int]


@dataclass(frozen=True)
class MapSource:
    """A full-world basemap backed by one image or a fixed tile grid."""

    artifact: str
    size: tuple[int, int]
    tiles: tuple[MapTile, ...]


def _single_image_source(filename: str, size: int) -> MapSource:
    return MapSource(
        artifact=f"static/{filename}",
        size=(size, size),
        tiles=(
            MapTile(
                name="full_world",
                path=_STATIC_MAP_DIR / filename,
                box=(0, 0, size, size),
            ),
        ),
    )


MAP_SOURCES: list[MapSource] = [
    _single_image_source("world_map.png", 2048),
    _single_image_source("world_map_2x.png", 4096),
    _single_image_source("world_map_4x.png", 8192),
    MapSource(
        artifact="static/world_map_8x",
        size=(16384, 16384),
        tiles=(
            MapTile(
                "nw",
                _STATIC_MAP_DIR / "world_map_8x_nw.png",
                (0, 0, 8192, 8192),
            ),
            MapTile(
                "ne",
                _STATIC_MAP_DIR / "world_map_8x_ne.png",
                (8192, 0, 16384, 8192),
            ),
            MapTile(
                "sw",
                _STATIC_MAP_DIR / "world_map_8x_sw.png",
                (0, 8192, 8192, 16384),
            ),
            MapTile(
                "se",
                _STATIC_MAP_DIR / "world_map_8x_se.png",
                (8192, 8192, 16384, 16384),
            ),
        ),
    ),
]
MINIMUM_CROP_LONG_EDGE_PX = 1400
MINIMUM_AUTOMATIC_CROP_PADDING_PX = 16
MAXIMUM_AUTOMATIC_CROP_PADDING_PX = 96
AUTOMATIC_CROP_PADDING_RATIO = 0.5
MAXIMUM_CROP_UPSCALE_FACTOR = 4.0
DEFAULT_MAP_ARTIFACT = "earthquake-map.png"
_SAFE_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*\.png$")
_LEGEND_TEXT_TRANSLATION = str.maketrans(
    {
        "\u00a0": " ",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2026": "...",
    }
)


class MapEvent(BaseModel):
    """A declarative map circle whose coordinate order is longitude, latitude."""

    coord: list[float] = Field(min_length=2, max_length=2)
    label: str
    latitude_radius: float
    color: str


class MapLegendItem(BaseModel):
    """One ordered color key in an optional map legend."""

    label: str
    color: str


class MapLegend(BaseModel):
    """Agent-supplied semantics for colors used on a rendered map."""

    title: str | None = None
    items: list[MapLegendItem]


class MapCaption(BaseModel):
    """Agent-supplied title and date displayed above a rendered map."""

    title: str = Field(min_length=1, max_length=120)
    date: str = Field(min_length=1, max_length=64)


DENSE_MAP_LABEL_MIN_MAGNITUDE = 6.0
DENSE_MAP_MAGNITUDE_LEGEND = MapLegend(
    title="Magnitude",
    items=[
        MapLegendItem(label="Below 1.0", color="#3b82f6"),
        MapLegendItem(label="1.0-1.9", color="#22c55e"),
        MapLegendItem(label="2.0-2.9", color="#eab308"),
        MapLegendItem(label="3.0-3.9", color="#f97316"),
        MapLegendItem(label="4.0+", color="#dc2626"),
        MapLegendItem(label="Unknown", color="#6b7280"),
    ],
)


def _normalize_longitude(longitude: float) -> tuple[float, bool]:
    if -180 <= longitude <= 180:
        return longitude, False
    normalized = ((longitude + 180) % 360) - 180
    return normalized, True


def _clamp_latitude(latitude: float) -> tuple[float, bool]:
    clamped = max(-WEB_MERCATOR_MAX_LAT, min(WEB_MERCATOR_MAX_LAT, latitude))
    return clamped, clamped != latitude


def project_web_mercator(
    longitude: float,
    latitude: float,
    width: int,
    height: int,
) -> tuple[float, float]:
    """Project longitude/latitude onto a canonical full-world Web Mercator PNG."""
    longitude, _ = _normalize_longitude(longitude)
    latitude, _ = _clamp_latitude(latitude)
    x = (longitude + 180.0) / 360.0 * width
    latitude_radians = math.radians(latitude)
    mercator = math.asinh(math.tan(latitude_radians))
    y = (1.0 - mercator / math.pi) / 2.0 * height
    y = max(0.0, min(float(height), y))
    return x, y


def latitude_radius_to_pixels(
    latitude: float,
    latitude_radius: float,
    height: int,
) -> float:
    """Convert a north/south latitude span to an averaged screen-space radius."""
    top_latitude, _ = _clamp_latitude(latitude + latitude_radius)
    bottom_latitude, _ = _clamp_latitude(latitude - latitude_radius)
    _, top_y = project_web_mercator(0, top_latitude, height, height)
    _, bottom_y = project_web_mercator(0, bottom_latitude, height, height)
    return abs(bottom_y - top_y) / 2.0


def _wrapped_circle_centers(x: float, radius: float, width: int) -> list[float]:
    centers = [x]
    if x - radius < 0:
        centers.append(x + width)
    if x + radius > width:
        centers.append(x - width)
    return centers


def _artifact_names(artifact_name: str) -> tuple[str, str] | None:
    if (
        not _SAFE_ARTIFACT_NAME.fullmatch(artifact_name)
        or ".." in Path(artifact_name).parts
        or Path(artifact_name).is_absolute()
    ):
        return None
    path = Path(artifact_name)
    spec_name = str(path.with_name(f"{path.stem}-spec.json"))
    return artifact_name, spec_name


def _parse_color(value: str) -> tuple[int, int, int, int]:
    return ImageColor.getcolor(value, "RGBA")


def _marker_colors(
    rgba: tuple[int, int, int, int],
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    fill_alpha = rgba[3] if rgba[3] < 255 else 96
    return (
        (rgba[0], rgba[1], rgba[2], fill_alpha),
        (rgba[0], rgba[1], rgba[2], 255),
    )


def _dense_event_color(magnitude: float | None) -> str:
    if magnitude is None:
        return "#6b7280"
    if magnitude < 1:
        return "#3b82f6"
    if magnitude < 2:
        return "#22c55e"
    if magnitude < 3:
        return "#eab308"
    if magnitude < 4:
        return "#f97316"
    return "#dc2626"


def _dense_event_radius(magnitude: float | None) -> float:
    """Return a bounded angular marker radius that grows with magnitude."""
    if magnitude is None:
        return 0.1
    return max(0.1, min(1.0, 0.1 * 2 ** (magnitude / 2)))


def _dense_event_label(event: dict[str, Any]) -> str:
    magnitude = event["magnitude"]
    if magnitude is None or magnitude < DENSE_MAP_LABEL_MIN_MAGNITUDE:
        return ""
    place = event["place"] or "Location unavailable"
    return f"M{magnitude:.1f} {place}"


def _prepare_legend(legend: MapLegend | None) -> dict[str, Any] | None:
    if legend is None:
        return None
    if not legend.items:
        raise ValueError("legend.items must contain at least one item.")

    title = legend.title.strip() if legend.title is not None else None
    prepared_items: list[dict[str, Any]] = []
    for index, item in enumerate(legend.items):
        label = item.label.strip()
        if not label:
            raise ValueError(f"Legend item {index} must have a non-blank label.")
        try:
            rgba = _parse_color(item.color)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Legend item {index} color {item.color!r} is invalid."
            ) from exc
        fill, outline = _marker_colors(rgba)
        prepared_items.append(
            {
                "label": label,
                "color": item.color,
                "fill": fill,
                "outline": outline,
            }
        )
    return {"title": title or None, "items": prepared_items}


def _prepare_caption(caption: MapCaption | None) -> dict[str, str] | None:
    if caption is None:
        return None
    title = caption.title.strip()
    date = caption.date.strip()
    if not title:
        raise ValueError("caption.title must be non-blank.")
    if not date:
        raise ValueError("caption.date must be non-blank.")
    if any(character in title or character in date for character in "\r\n\t"):
        raise ValueError("Caption title and date must each be a single line.")
    return {"title": title, "date": date}


def _legend_candidate_boxes(
    image_size: tuple[int, int],
    panel_size: tuple[int, int],
    margin: int,
    bottom_clearance: int = 0,
) -> list[tuple[str, tuple[int, int, int, int]]]:
    width, height = image_size
    panel_width, panel_height = panel_size
    left = margin
    top = margin
    right = width - margin - panel_width
    bottom = height - margin - bottom_clearance - panel_height
    candidates: list[tuple[str, tuple[int, int, int, int]]] = []
    if bottom >= top:
        candidates.extend(
            [
                (
                    "bottom_right",
                    (right, bottom, right + panel_width, bottom + panel_height),
                ),
                (
                    "bottom_left",
                    (left, bottom, left + panel_width, bottom + panel_height),
                ),
            ]
        )
    candidates.extend(
        [
            ("top_right", (right, top, right + panel_width, top + panel_height)),
            ("top_left", (left, top, left + panel_width, top + panel_height)),
        ]
    )
    return candidates


def _occupied_pixel_count(
    content_overlay: Image.Image,
    box: tuple[int, int, int, int],
) -> int:
    histogram = content_overlay.getchannel("A").crop(box).histogram()
    return sum(histogram[1:])


def _choose_legend_corner(
    content_overlay: Image.Image,
    panel_size: tuple[int, int],
    margin: int,
    bottom_clearance: int = 0,
) -> tuple[str, tuple[int, int, int, int]]:
    candidates = _legend_candidate_boxes(
        content_overlay.size,
        panel_size,
        margin,
        bottom_clearance,
    )
    return min(
        candidates,
        key=lambda candidate: _occupied_pixel_count(content_overlay, candidate[1]),
    )


def _load_legend_font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size=size)
    except OSError:
        return ImageFont.load_default(size=size)


def _annotation_font_size(image_size: tuple[int, int]) -> int:
    return max(10, round(min(image_size) * 0.018))


def _load_label_font(size: int = 10) -> ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _legend_display_text(value: str) -> str:
    """Replace common unsupported punctuation in Pillow's bundled font."""
    return value.translate(_LEGEND_TEXT_TRANSLATION)


def _load_caption_font(size: int, *, bold: bool) -> ImageFont.ImageFont:
    font_name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(font_name, size=size)
    except OSError:
        return ImageFont.load_default(size=size)


def _fit_caption_font(
    text: str,
    max_width: int,
    preferred_size: int,
    *,
    bold: bool,
) -> tuple[ImageFont.ImageFont, tuple[int, int, int, int]]:
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for size in range(preferred_size, 9, -1):
        font = _load_caption_font(size, bold=bold)
        box = measure.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= max_width:
            return font, box
    raise ValueError(
        "Caption does not fit above the rendered map; shorten its title or date."
    )


def _crop_contains_source_bottom_right(
    crop: dict[str, int | bool],
    source_size: tuple[int, int],
) -> bool:
    source_width, source_height = source_size
    source_x = int(crop["source_x"])
    source_y = int(crop["source_y"])
    crop_width = int(crop["width"])
    crop_height = int(crop["height"])
    contains_bottom = source_y + crop_height >= source_height
    contains_right = (
        crop_width == source_width
        or bool(crop["wraps_antimeridian"])
        or source_x + crop_width >= source_width
    )
    return contains_bottom and contains_right


def _label_candidates(
    x: float,
    y: float,
    radius: float,
    text_width: int,
    text_height: int,
    gap: int = 7,
) -> list[tuple[float, float]]:
    return [
        (x + radius + gap, y - text_height - gap),
        (x + radius + gap, y + gap),
        (x - radius - gap - text_width, y - text_height - gap),
        (x - radius - gap - text_width, y + gap),
        (x - text_width / 2, y - radius - gap - text_height),
        (x - text_width / 2, y + radius + gap),
        (x + gap, y - text_height / 2),
        (x - gap - text_width, y - text_height / 2),
    ]


def _boxes_overlap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    return not (
        left[2] <= right[0]
        or right[2] <= left[0]
        or left[3] <= right[1]
        or right[3] <= left[1]
    )


def _choose_label_position(
    x_positions: list[float],
    y: float,
    radius: float,
    text_size: tuple[int, int],
    image_size: tuple[int, int],
    occupied: list[tuple[float, float, float, float]],
    gap: int = 7,
) -> tuple[
    tuple[float, float] | None,
    tuple[float, float, float, float] | None,
]:
    text_width, text_height = text_size
    image_width, image_height = image_size
    for x_position in x_positions:
        for candidate in _label_candidates(
            x_position,
            y,
            radius,
            text_width,
            text_height,
            gap,
        ):
            x, candidate_y = candidate
            box = (x, candidate_y, x + text_width, candidate_y + text_height)
            if (
                x < 0
                or candidate_y < 0
                or box[2] > image_width
                or box[3] > image_height
            ):
                continue
            if any(_boxes_overlap(box, prior) for prior in occupied):
                continue
            return candidate, box
    return None, None


def _content_crop(
    overlay: Image.Image,
    padding: int,
) -> dict[str, int | bool] | None:
    """Return the smallest padded crop, treating the map's X axis as circular."""
    width, height = overlay.size
    x_projection, y_projection = overlay.getchannel("A").getprojection()
    occupied_x = [index for index, value in enumerate(x_projection) if value]
    occupied_y = [index for index, value in enumerate(y_projection) if value]
    if not occupied_x or not occupied_y:
        return None

    if len(occupied_x) == width:
        crop_x = 0
        crop_width = width
        wraps_antimeridian = False
    else:
        largest_gap = -1
        arc_start = occupied_x[0]
        arc_end = occupied_x[-1]
        for index, current in enumerate(occupied_x):
            following = occupied_x[(index + 1) % len(occupied_x)]
            gap = (following - current - 1) % width
            if gap > largest_gap:
                largest_gap = gap
                arc_start = following
                arc_end = arc_start + ((current - arc_start) % width)

        unwrapped_start = arc_start - padding
        unwrapped_end = arc_end + 1 + padding
        crop_width = unwrapped_end - unwrapped_start
        if crop_width >= width:
            crop_x = 0
            crop_width = width
            wraps_antimeridian = False
        else:
            crop_x = unwrapped_start % width
            wraps_antimeridian = crop_x + crop_width > width

    crop_y = max(0, occupied_y[0] - padding)
    crop_bottom = min(height, occupied_y[-1] + 1 + padding)
    return {
        "source_x": crop_x,
        "source_y": crop_y,
        "width": crop_width,
        "height": crop_bottom - crop_y,
        "wraps_antimeridian": wraps_antimeridian,
    }


def _automatic_crop_padding(
    marker_crop: dict[str, int | bool] | None,
) -> tuple[int, int]:
    """Return deterministic context padding from the marker-only extent."""
    if marker_crop is None:
        return MINIMUM_AUTOMATIC_CROP_PADDING_PX, 0
    marker_long_edge = max(
        int(marker_crop["width"]),
        int(marker_crop["height"]),
    )
    padding = round(marker_long_edge * AUTOMATIC_CROP_PADDING_RATIO)
    return (
        max(
            MINIMUM_AUTOMATIC_CROP_PADDING_PX,
            min(MAXIMUM_AUTOMATIC_CROP_PADDING_PX, padding),
        ),
        marker_long_edge,
    )


def _crop_wrapped_image(
    image: Image.Image,
    crop: dict[str, int | bool],
) -> Image.Image:
    """Crop an image, stitching its right and left edges when necessary."""
    source_x = int(crop["source_x"])
    source_y = int(crop["source_y"])
    crop_width = int(crop["width"])
    crop_height = int(crop["height"])
    source_width = image.width
    bottom = source_y + crop_height

    if not crop["wraps_antimeridian"]:
        return image.crop(
            (source_x, source_y, source_x + crop_width, bottom)
        )

    right_width = source_width - source_x
    cropped = Image.new(image.mode, (crop_width, crop_height))
    cropped.paste(
        image.crop((source_x, source_y, source_width, bottom)),
        (0, 0),
    )
    cropped.paste(
        image.crop((0, source_y, crop_width - right_width, bottom)),
        (right_width, 0),
    )
    return cropped


def _latitude_at_pixel(y: int, height: int) -> float:
    mercator = math.pi * (1.0 - 2.0 * y / height)
    return math.degrees(math.atan(math.sinh(mercator)))


def _visible_bounds(
    crop: dict[str, int | bool],
    source_width: int,
    source_height: int,
) -> dict[str, float]:
    source_x = int(crop["source_x"])
    source_y = int(crop["source_y"])
    crop_width = int(crop["width"])
    crop_height = int(crop["height"])
    if source_x == 0 and crop_width == source_width:
        west = -180.0
        east = 180.0
    else:
        west = source_x / source_width * 360.0 - 180.0
        east = (source_x + crop_width) / source_width * 360.0 - 180.0
        if east > 180.0:
            east -= 360.0
    return {
        "west": west,
        "east": east,
        "north": _latitude_at_pixel(source_y, source_height),
        "south": _latitude_at_pixel(source_y + crop_height, source_height),
    }


def _map_source_scale(source: MapSource) -> int:
    standard_width, standard_height = MAP_SOURCES[0].size
    source_width, source_height = source.size
    width_scale, width_remainder = divmod(source_width, standard_width)
    height_scale, height_remainder = divmod(source_height, standard_height)
    if width_remainder or height_remainder or width_scale != height_scale:
        raise ValueError(
            "Map source dimensions must scale uniformly from the standard map."
        )
    return width_scale


def _select_map_source(crop: dict[str, int | bool]) -> MapSource:
    crop_long_edge = max(int(crop["width"]), int(crop["height"]))
    for source in MAP_SOURCES:
        if crop_long_edge * _map_source_scale(source) >= MINIMUM_CROP_LONG_EDGE_PX:
            return source
    return MAP_SOURCES[-1]


def _scaled_crop(
    crop: dict[str, int | bool],
    scale: int,
) -> dict[str, int | bool]:
    return {
        "source_x": int(crop["source_x"]) * scale,
        "source_y": int(crop["source_y"]) * scale,
        "width": int(crop["width"]) * scale,
        "height": int(crop["height"]) * scale,
        "wraps_antimeridian": crop["wraps_antimeridian"],
    }


def _crop_map_source(
    source: MapSource,
    crop: dict[str, int | bool],
) -> Image.Image:
    """Crop a full-world source, loading only tiles that intersect the crop."""
    source_width, source_height = source.size
    source_x = int(crop["source_x"])
    source_y = int(crop["source_y"])
    crop_width = int(crop["width"])
    crop_height = int(crop["height"])
    crop_bottom = source_y + crop_height
    if source_y < 0 or crop_bottom > source_height:
        raise ValueError("Map crop exceeds the source's vertical bounds.")

    if crop["wraps_antimeridian"]:
        right_width = source_width - source_x
        horizontal_spans = (
            (source_x, source_width, 0),
            (0, crop_width - right_width, right_width),
        )
    else:
        horizontal_spans = ((source_x, source_x + crop_width, 0),)

    cropped = Image.new("RGBA", (crop_width, crop_height), (0, 0, 0, 0))
    for tile in source.tiles:
        tile_left, tile_top, tile_right, tile_bottom = tile.box
        intersections: list[tuple[int, int, int, int, int]] = []
        for span_left, span_right, destination_x in horizontal_spans:
            left = max(tile_left, span_left)
            top = max(tile_top, source_y)
            right = min(tile_right, span_right)
            bottom = min(tile_bottom, crop_bottom)
            if left < right and top < bottom:
                output_x = destination_x + left - span_left
                intersections.append((left, top, right, bottom, output_x))
        if not intersections:
            continue
        if not tile.path.is_file():
            raise FileNotFoundError(f"Base map tile not found at {tile.path}.")
        expected_size = (tile_right - tile_left, tile_bottom - tile_top)
        with Image.open(tile.path) as tile_image:
            if tile_image.size != expected_size:
                raise ValueError(
                    f"Base map tile {tile.name} must be "
                    f"{expected_size[0]}x{expected_size[1]}, not "
                    f"{tile_image.width}x{tile_image.height}."
                )
            for left, top, right, bottom, output_x in intersections:
                tile_crop = tile_image.crop(
                    (
                        left - tile_left,
                        top - tile_top,
                        right - tile_left,
                        bottom - tile_top,
                    )
                ).convert("RGBA")
                cropped.paste(tile_crop, (output_x, top - source_y))
    return cropped


def _viewport_x_positions(
    full_x: float,
    radius: float,
    crop_x: int,
    crop_width: int,
    source_width: int,
) -> list[float]:
    relative_x = full_x - crop_x
    return [
        candidate
        for candidate in (
            relative_x - source_width,
            relative_x,
            relative_x + source_width,
        )
        if candidate + radius >= 0 and candidate - radius <= crop_width
    ]


def _render_scaled_crop(
    prepared: list[dict[str, Any]],
    placed_labels: list[dict[str, Any]],
    standard_crop: dict[str, int | bool],
    source: MapSource,
    warnings: list[str],
    upscale_factor: float = 1.0,
) -> tuple[Image.Image, dict[str, int | bool], Image.Image]:
    source_size = source.size
    scale = _map_source_scale(source)
    scaled_crop = _scaled_crop(standard_crop, scale)
    base = _crop_map_source(source, scaled_crop)

    if upscale_factor > 1.0:
        base = base.resize(
            (
                max(1, round(base.width * upscale_factor)),
                max(1, round(base.height * upscale_factor)),
            ),
            Image.Resampling.LANCZOS,
        )

    crop_x = int(scaled_crop["source_x"])
    crop_y = int(scaled_crop["source_y"])
    crop_width = int(scaled_crop["width"])
    content_overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(content_overlay, "RGBA")
    maximum_outline_width = max(2, round(source_size[0] / 1024))
    for item in prepared:
        # Crop enlargement interpolates the basemap to a useful output size;
        # it must not also inflate earthquake symbols into overlapping blobs.
        radius = item["radius_px"] * scale
        outline_width = min(
            maximum_outline_width,
            max(1, round(radius * 0.25)),
        )
        y = (item["y"] * scale - crop_y) * upscale_factor
        native_x_positions = _viewport_x_positions(
            item["x"] * scale,
            item["radius_px"] * scale,
            crop_x,
            crop_width,
            source_size[0],
        )
        for x in (position * upscale_factor for position in native_x_positions):
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                fill=item["fill"],
                outline=item["outline"],
                width=outline_width,
            )

    font_size = _annotation_font_size(base.size)
    font = _load_label_font(font_size)
    label_stroke_width = max(2, round(font_size / 10))
    label_gap = max(7, round(font_size * 0.7))
    occupied_labels: list[tuple[float, float, float, float]] = []
    prepared_by_index = {item["index"]: item for item in prepared}
    for placed in placed_labels:
        item = prepared_by_index[placed["index"]]
        text_box = draw.textbbox(
            (0, 0), placed["label"], font=font, stroke_width=label_stroke_width
        )
        text_width = text_box[2] - text_box[0]
        text_height = text_box[3] - text_box[1]
        center_y = (item["y"] * scale - crop_y) * upscale_factor
        centers = [
            position * upscale_factor
            for position in _viewport_x_positions(
                item["x"] * scale,
                item["radius_px"] * scale,
                crop_x,
                crop_width,
                source_size[0],
            )
        ]
        chosen, chosen_box = _choose_label_position(
            centers,
            center_y,
            item["radius_px"] * scale,
            (text_width, text_height),
            base.size,
            occupied_labels,
            label_gap,
        )
        if chosen is None or chosen_box is None:
            warnings.append(
                f"Label for event {item['index']} could not be placed at final resolution."
            )
            continue
        draw.text(
            chosen,
            placed["label"],
            font=font,
            fill=(20, 20, 20, 255),
            stroke_width=label_stroke_width,
            stroke_fill=(255, 255, 255, 235),
        )
        occupied_labels.append(chosen_box)
    rendered = Image.alpha_composite(base, content_overlay).convert("RGB")
    return rendered, scaled_crop, content_overlay


def _render_legend(
    rendered: Image.Image,
    content_overlay: Image.Image,
    prepared_legend: dict[str, Any] | None,
    reserve_bottom_attribution: bool,
) -> tuple[Image.Image, dict[str, Any] | None]:
    if prepared_legend is None:
        return rendered, None

    width, height = rendered.size
    font_size = _annotation_font_size(rendered.size)
    font = _load_legend_font(font_size)
    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    padding = max(4, round(font_size * 0.5))
    margin = max(4, round(font_size * 0.5))
    swatch_size = font_size
    column_gap = max(3, round(font_size * 0.4))
    row_gap = max(2, round(font_size * 0.25))
    section_gap = max(3, round(font_size * 0.4))

    title = prepared_legend["title"]
    display_title = _legend_display_text(title) if title is not None else None
    title_size = (0, 0)
    title_top = 0
    if display_title is not None:
        title_box = measure.textbbox((0, 0), display_title, font=font)
        title_size = (title_box[2] - title_box[0], title_box[3] - title_box[1])
        title_top = title_box[1]

    item_layout: list[dict[str, Any]] = []
    content_width = title_size[0]
    content_height = title_size[1]
    if title is not None:
        content_height += section_gap
    for index, item in enumerate(prepared_legend["items"]):
        display_label = _legend_display_text(item["label"])
        text_box = measure.textbbox((0, 0), display_label, font=font)
        text_width = text_box[2] - text_box[0]
        text_height = text_box[3] - text_box[1]
        row_height = max(swatch_size, text_height)
        row_width = swatch_size + column_gap + text_width
        content_width = max(content_width, row_width)
        if index:
            content_height += row_gap
        content_height += row_height
        item_layout.append(
            {
                **item,
                "display_label": display_label,
                "text_top": text_box[1],
                "text_height": text_height,
                "row_height": row_height,
            }
        )

    panel_size = (
        math.ceil(content_width + 2 * padding),
        math.ceil(content_height + 2 * padding),
    )
    if panel_size[0] + 2 * margin > width or panel_size[1] + 2 * margin > height:
        raise ValueError(
            "Legend does not fit within the rendered map; shorten its labels "
            "or use fewer items."
        )

    bottom_clearance = (
        max(24, round(font_size * 1.25))
        if reserve_bottom_attribution
        else 0
    )
    corner, panel_box = _choose_legend_corner(
        content_overlay,
        panel_size,
        margin,
        bottom_clearance,
    )
    legend_overlay = Image.new("RGBA", rendered.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(legend_overlay, "RGBA")
    border_width = max(1, round(font_size / 10))
    radius = max(4, round(font_size * 0.4))
    draw_panel_box = (
        panel_box[0],
        panel_box[1],
        panel_box[2] - 1,
        panel_box[3] - 1,
    )
    draw.rounded_rectangle(
        draw_panel_box,
        radius=radius,
        fill=(255, 255, 255, 224),
        outline=(20, 20, 20, 210),
        width=border_width,
    )

    x = panel_box[0] + padding
    y = panel_box[1] + padding
    if title is not None:
        draw.text(
            (x, y - title_top),
            display_title,
            font=font,
            fill=(20, 20, 20, 255),
        )
        y += title_size[1] + section_gap
    swatch_outline_width = max(2, round(font_size / 10))
    for index, item in enumerate(item_layout):
        if index:
            y += row_gap
        swatch_top = y + (item["row_height"] - swatch_size) / 2
        draw.ellipse(
            (x, swatch_top, x + swatch_size, swatch_top + swatch_size),
            fill=item["fill"],
            outline=item["outline"],
            width=swatch_outline_width,
        )
        text_y = y + (item["row_height"] - item["text_height"]) / 2
        draw.text(
            (x + swatch_size + column_gap, text_y - item["text_top"]),
            item["display_label"],
            font=font,
            fill=(20, 20, 20, 255),
        )
        y += item["row_height"]

    composited = Image.alpha_composite(
        rendered.convert("RGBA"), legend_overlay
    ).convert("RGB")
    spec = {
        "title": title,
        "items": [
            {"label": item["label"], "color": item["color"]}
            for item in prepared_legend["items"]
        ],
        "corner": corner,
    }
    return composited, spec


def _render_caption(
    rendered: Image.Image,
    prepared_caption: dict[str, str] | None,
) -> tuple[Image.Image, dict[str, Any] | None]:
    if prepared_caption is None:
        return rendered, None

    width, height = rendered.size
    preferred_title_size = max(14, round(min(width, height) * 0.026))
    padding = max(8, round(preferred_title_size * 0.6))
    available_width = width - 2 * padding
    if available_width <= 0:
        raise ValueError("Caption does not fit above the rendered map.")

    display_title = _legend_display_text(prepared_caption["title"])
    display_date = _legend_display_text(prepared_caption["date"])
    title_font, title_box = _fit_caption_font(
        display_title,
        available_width,
        preferred_title_size,
        bold=True,
    )
    date_font, date_box = _fit_caption_font(
        display_date,
        available_width,
        max(10, round(preferred_title_size * 0.62)),
        bold=False,
    )
    title_height = title_box[3] - title_box[1]
    date_height = date_box[3] - date_box[1]
    line_gap = max(4, round(preferred_title_size * 0.25))
    caption_height = padding + title_height + line_gap + date_height + padding

    captioned = Image.new("RGB", (width, height + caption_height), (250, 250, 248))
    captioned.paste(rendered.convert("RGB"), (0, caption_height))
    draw = ImageDraw.Draw(captioned)
    title_y = padding - title_box[1]
    draw.text(
        (padding - title_box[0], title_y),
        display_title,
        font=title_font,
        fill=(20, 20, 20),
    )
    date_y = padding + title_height + line_gap - date_box[1]
    draw.text(
        (padding - date_box[0], date_y),
        display_date,
        font=date_font,
        fill=(80, 80, 80),
    )
    draw.line(
        (0, caption_height - 1, width, caption_height - 1),
        fill=(190, 190, 185),
        width=1,
    )
    return captioned, {
        "title": prepared_caption["title"],
        "date": prepared_caption["date"],
        "placement": "top",
        "height_px": caption_height,
    }


def _render_map(
    events: list[MapEvent],
    crop_to_drawn_area: bool = False,
    legend: MapLegend | None = None,
    caption: MapCaption | None = None,
) -> tuple[bytes, dict[str, Any], list[str], int]:
    prepared_legend = _prepare_legend(legend)
    prepared_caption = _prepare_caption(caption)
    standard_source = MAP_SOURCES[0]
    standard_size = standard_source.size
    image = _crop_map_source(
        standard_source,
        {
            "source_x": 0,
            "source_y": 0,
            "width": standard_size[0],
            "height": standard_size[1],
            "wraps_antimeridian": False,
        },
    )

    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _load_label_font()
    warnings: list[str] = []
    prepared: list[dict[str, Any]] = []
    skipped = 0

    for index, event in enumerate(events):
        longitude, latitude = event.coord
        if not all(math.isfinite(value) for value in (longitude, latitude)):
            warnings.append(f"Event {index} was skipped because its coordinate is not finite.")
            skipped += 1
            continue
        if not math.isfinite(event.latitude_radius) or event.latitude_radius <= 0:
            warnings.append(f"Event {index} was skipped because latitude_radius must be positive.")
            skipped += 1
            continue
        try:
            rgba = _parse_color(event.color)
        except (TypeError, ValueError):
            warnings.append(f"Event {index} was skipped because color {event.color!r} is invalid.")
            skipped += 1
            continue

        normalized_longitude, longitude_changed = _normalize_longitude(longitude)
        clamped_latitude, latitude_changed = _clamp_latitude(latitude)
        if longitude_changed:
            warnings.append(
                f"Event {index} longitude was normalized from {longitude} to "
                f"{normalized_longitude}."
            )
        if latitude_changed:
            warnings.append(
                f"Event {index} latitude was clamped from {latitude} to "
                f"{clamped_latitude} for Web Mercator."
            )
        if abs(latitude + event.latitude_radius) > WEB_MERCATOR_MAX_LAT or abs(
            latitude - event.latitude_radius
        ) > WEB_MERCATOR_MAX_LAT:
            warnings.append(f"Event {index} radius was clipped at a Mercator latitude limit.")

        x, y = project_web_mercator(
            normalized_longitude, clamped_latitude, width, height
        )
        radius = max(
            1.0,
            latitude_radius_to_pixels(
                clamped_latitude, event.latitude_radius, height
            ),
        )
        fill, outline = _marker_colors(rgba)
        prepared.append(
            {
                "index": index,
                "coord": [normalized_longitude, clamped_latitude],
                "label": event.label,
                "latitude_radius": event.latitude_radius,
                "color": event.color,
                "rgba": rgba,
                "fill": fill,
                "outline": outline,
                "x": x,
                "y": y,
                "radius_px": radius,
            }
        )

    prepared.sort(key=lambda item: (item["radius_px"], item["index"]))
    outline_width = max(2, round(width / 1024))
    for item in prepared:
        for center_x in _wrapped_circle_centers(item["x"], item["radius_px"], width):
            box = (
                center_x - item["radius_px"],
                item["y"] - item["radius_px"],
                center_x + item["radius_px"],
                item["y"] + item["radius_px"],
            )
            draw.ellipse(
                box,
                fill=item["fill"],
                outline=item["outline"],
                width=outline_width,
            )

    marker_crop = _content_crop(overlay, 0)
    crop_padding_px, marker_long_edge_px = _automatic_crop_padding(marker_crop)

    crop: dict[str, int | bool] = {
        "source_x": 0,
        "source_y": 0,
        "width": width,
        "height": height,
        "wraps_antimeridian": False,
    }
    crop_applied = False
    if crop_to_drawn_area:
        # Geographic framing is marker-only. Labels are reflowed within the
        # chosen viewport so their text length cannot zoom the map out.
        content_crop = _content_crop(overlay, crop_padding_px)
        if content_crop is None:
            warnings.append(
                "Crop was requested but there was no drawable content; the full map was retained."
            )
        else:
            crop = content_crop
            crop_applied = crop["width"] != width or crop["height"] != height

    requested_labels = [
        {"index": item["index"], "label": item["label"].strip()}
        for item in sorted(prepared, key=lambda value: value["index"])
        if item["label"].strip()
    ]
    if not crop_applied:
        occupied_labels: list[tuple[float, float, float, float]] = []
        prepared_by_index = {item["index"]: item for item in prepared}
        for requested in requested_labels:
            item = prepared_by_index[requested["index"]]
            label = requested["label"]
            label_x = item["x"] % width
            text_box = draw.textbbox((0, 0), label, font=font, stroke_width=2)
            text_width = text_box[2] - text_box[0]
            text_height = text_box[3] - text_box[1]
            chosen, chosen_box = _choose_label_position(
                [label_x],
                item["y"],
                item["radius_px"],
                (text_width, text_height),
                (width, height),
                occupied_labels,
            )
            if chosen is None or chosen_box is None:
                warnings.append(
                    f"Label for event {item['index']} could not be placed."
                )
                continue
            draw.text(
                chosen,
                label,
                font=font,
                fill=(20, 20, 20, 255),
                stroke_width=2,
                stroke_fill=(255, 255, 255, 235),
            )
            occupied_labels.append(chosen_box)

    rendered = Image.alpha_composite(image, overlay).convert("RGB")
    content_overlay = overlay

    selected_source = MAP_SOURCES[0]
    if crop_applied:
        selected_source = _select_map_source(crop)
    source_size = selected_source.size
    source_scale = _map_source_scale(selected_source)
    source_padding_px = crop_padding_px * source_scale
    native_width = int(crop["width"]) * source_scale
    native_height = int(crop["height"]) * source_scale
    upscale_factor = 1.0
    if (
        crop_applied
        and selected_source == MAP_SOURCES[-1]
        and max(native_width, native_height) < MINIMUM_CROP_LONG_EDGE_PX
    ):
        upscale_factor = min(
            MAXIMUM_CROP_UPSCALE_FACTOR,
            MINIMUM_CROP_LONG_EDGE_PX / max(native_width, native_height),
        )
    if crop_applied:
        rendered, crop, content_overlay = _render_scaled_crop(
            prepared,
            requested_labels,
            crop,
            selected_source,
            warnings,
            upscale_factor,
        )
        width, height = source_size

    rendered, legend_spec = _render_legend(
        rendered,
        content_overlay,
        prepared_legend,
        _crop_contains_source_bottom_right(crop, source_size),
    )

    map_width, map_height = rendered.size
    minimum_long_edge_satisfied = (
        max(map_width, map_height) >= MINIMUM_CROP_LONG_EDGE_PX
    )
    if crop_applied and not minimum_long_edge_satisfied:
        warnings.append(
            f"Crop long edge is {max(map_width, map_height)} pixels after "
            f"{upscale_factor:g}x enlargement, below the "
            f"{MINIMUM_CROP_LONG_EDGE_PX}-pixel target."
        )

    rendered, caption_spec = _render_caption(rendered, prepared_caption)
    caption_height = caption_spec["height_px"] if caption_spec is not None else 0
    output = BytesIO()
    rendered.save(output, format="PNG", optimize=True)
    spec = {
        "projection": "web_mercator",
        "bounds": _visible_bounds(crop, width, height),
        "source": {
            "artifact": selected_source.artifact,
            "width": width,
            "height": height,
            "bounds": {
                "west": -180,
                "east": 180,
                "north": WEB_MERCATOR_MAX_LAT,
                "south": -WEB_MERCATOR_MAX_LAT,
            },
            "scale": source_scale,
            "tiles": [
                {
                    "name": tile.name,
                    "artifact": str(tile.path.relative_to(_STATIC_MAP_DIR.parent)),
                    "box": {
                        "source_x": tile.box[0],
                        "source_y": tile.box[1],
                        "width": tile.box[2] - tile.box[0],
                        "height": tile.box[3] - tile.box[1],
                    },
                }
                for tile in selected_source.tiles
            ],
        },
        "crop": {
            "requested": crop_to_drawn_area,
            "applied": crop_applied,
            "padding_mode": "automatic",
            "padding_px": crop_padding_px,
            "source_padding_px": source_padding_px,
            "marker_long_edge_px": marker_long_edge_px,
            "native_width": native_width,
            "native_height": native_height,
            "upscale_factor": round(upscale_factor, 6),
            "minimum_long_edge_px": MINIMUM_CROP_LONG_EDGE_PX,
            "minimum_long_edge_satisfied": minimum_long_edge_satisfied,
            **crop,
        },
        "width": rendered.width,
        "height": rendered.height,
        "map_viewport": {
            "x": 0,
            "y": caption_height,
            "width": map_width,
            "height": map_height,
        },
        "caption": caption_spec,
        "legend": legend_spec,
        "events": [
            {
                "coord": item["coord"],
                "label": item["label"],
                "latitude_radius": item["latitude_radius"],
                "color": item["color"],
            }
            for item in sorted(prepared, key=lambda value: value["index"])
        ],
    }
    return output.getvalue(), spec, warnings, skipped


async def plot_usgs_feed_on_map(
    feed: FeedName,
    artifact_version: int | None = None,
    min_magnitude: float | None = None,
    bounds: GeoBounds | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    artifact_name: str = DEFAULT_MAP_ARTIFACT,
    crop_to_drawn_area: bool = False,
    caption: MapCaption | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Plot a stored USGS catalog without putting its events in model context.

    Args:
        feed: The stored "hourly" or "monthly" catalog.
        artifact_version: Exact catalog artifact version; omit for the active one.
        min_magnitude: Optional inclusive minimum magnitude.
        bounds: Optional geographic bounds. West greater than east crosses the
            antimeridian.
        start_time: Optional inclusive ISO-8601 UTC lower bound.
        end_time: Optional inclusive ISO-8601 UTC upper bound.
        artifact_name: Relative session-scoped PNG artifact name.
        crop_to_drawn_area: Crop to the filtered events instead of keeping the
            full-world view.
        caption: Optional title and date rendered above the map.

    Returns:
        Compact catalog provenance and versioned map artifact handles. Event
        coordinates stay inside the tool and are never returned to the model.
    """
    if tool_context is None:
        return {"status": "error", "error": "Tool context is unavailable."}
    if feed not in ARTIFACT_NAMES:
        return {"status": "error", "error": f"Unsupported feed: {feed!r}."}
    names = _artifact_names(artifact_name)
    if names is None:
        return {
            "status": "error",
            "error": "artifact_name must be a safe relative .png path.",
        }
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
    selected, skipped_invalid, duplicates = _select_catalog_events(
        catalog,
        min_magnitude=min_magnitude,
        bounds=bounds,
        start=start,
        end=end,
        sort="time_desc",
    )
    metadata = catalog.get("metadata", {})
    provenance = {
        "feed": feed,
        "source_url": USGS_FEED_URLS[feed],
        "catalog_artifact_name": ARTIFACT_NAMES[feed],
        "catalog_artifact_version": selected_version,
        "source_generated_at": _epoch_ms_to_utc(metadata.get("generated")),
        "total_catalog_events": len(catalog["features"]),
        "total_matched": len(selected),
        "skipped_invalid": skipped_invalid,
        "deduplicated_count": duplicates,
    }
    if not selected:
        return {"status": "empty", **provenance}

    map_events = [
        MapEvent(
            coord=event["coord"],
            label=_dense_event_label(event),
            latitude_radius=_dense_event_radius(event["magnitude"]),
            color=_dense_event_color(event["magnitude"]),
        )
        for event in selected
    ]
    try:
        image_bytes, spec, warnings, render_skipped = _render_map(
            map_events,
            crop_to_drawn_area=crop_to_drawn_area,
            legend=DENSE_MAP_MAGNITUDE_LEGEND,
            caption=caption,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        return {"status": "error", "error": str(exc), **provenance}

    rendered_count = len(spec["events"])
    spec["event_count"] = rendered_count
    spec["event_source"] = {
        "kind": "usgs_catalog_artifact",
        **provenance,
        "filters": {
            "min_magnitude": min_magnitude,
            "bounds": bounds.model_dump() if bounds is not None else None,
            "start_time": start_time,
            "end_time": end_time,
        },
        "style": {
            "color": "magnitude_bins",
            "radius": "magnitude_scaled",
            "labels": f"magnitude >= {DENSE_MAP_LABEL_MIN_MAGNITUDE:g}",
        },
    }
    # The catalog artifact plus the deterministic style above is the source of
    # truth. Avoid duplicating thousands of markers in the map spec, where a
    # later load could put the full catalog back into model context.
    spec["events"] = []

    image_name, spec_name = names
    spec_bytes = json.dumps(spec, indent=2, sort_keys=True).encode("utf-8")
    try:
        image_version = await tool_context.save_artifact(
            image_name,
            types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
            custom_metadata={
                "projection": "web_mercator",
                "event_count": rendered_count,
                "cropped": spec["crop"]["applied"],
                "legend_item_count": len(spec["legend"]["items"]),
                "captioned": spec["caption"] is not None,
                "catalog_artifact": ARTIFACT_NAMES[feed],
                "catalog_version": selected_version,
            },
        )
        spec_version = await tool_context.save_artifact(
            spec_name,
            types.Part.from_bytes(data=spec_bytes, mime_type="application/json"),
            custom_metadata={"map_artifact": image_name, "map_version": image_version},
        )
    except (OSError, ValueError) as exc:
        return {
            "status": "error",
            "error": f"Map was rendered but artifacts could not be saved: {exc}",
            **provenance,
        }

    tool_context.state["current_map_artifact"] = image_name
    tool_context.state["current_map_version"] = image_version
    tool_context.state["current_map_spec_artifact"] = spec_name
    tool_context.state["current_map_spec_version"] = spec_version
    return {
        "status": "ok",
        **provenance,
        "map_artifact_name": image_name,
        "map_artifact_version": image_version,
        "spec_artifact_name": spec_name,
        "spec_artifact_version": spec_version,
        "width": spec["width"],
        "height": spec["height"],
        "crop": spec["crop"],
        "bounds": spec["bounds"],
        "source_map": spec["source"]["artifact"],
        "map_viewport": spec["map_viewport"],
        "caption": spec["caption"],
        "legend": spec["legend"],
        "rendered_count": rendered_count,
        "skipped_count": render_skipped,
        "warnings": warnings,
    }


async def plot_usgs_search_on_map(
    artifact_version: int | None = None,
    min_magnitude: float | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    artifact_name: str = DEFAULT_MAP_ARTIFACT,
    crop_to_drawn_area: bool = False,
    caption: MapCaption | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Plot a stored USGS historical search without exposing its event array.

    Args:
        artifact_version: Exact search artifact version; omit for the active one.
        min_magnitude: Optional additional inclusive minimum magnitude.
        start_time: Optional additional inclusive ISO-8601 UTC lower bound.
        end_time: Optional additional inclusive ISO-8601 UTC upper bound.
        artifact_name: Relative session-scoped PNG artifact name.
        crop_to_drawn_area: Crop to the filtered events instead of keeping the
            full-world view.
        caption: Optional title and date rendered above the map.

    Returns:
        Compact historical-search provenance and versioned map artifact handles.
        Event coordinates stay inside the tool and are never returned to the model.
    """
    if tool_context is None:
        return {"status": "error", "error": "Tool context is unavailable."}
    if min_magnitude is not None and (
        isinstance(min_magnitude, bool)
        or not isinstance(min_magnitude, (int, float))
        or not math.isfinite(min_magnitude)
    ):
        return {"status": "error", "error": "min_magnitude must be finite."}
    names = _artifact_names(artifact_name)
    if names is None:
        return {
            "status": "error",
            "error": "artifact_name must be a safe relative .png path.",
        }

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
    search_provenance = _search_provenance(catalog)
    if search_provenance is None:
        return {"status": "error", "error": "Search provenance is invalid."}

    selected, skipped_invalid, duplicates = _select_catalog_events(
        catalog,
        min_magnitude=(
            float(min_magnitude) if min_magnitude is not None else None
        ),
        bounds=None,
        start=start,
        end=end,
        sort="time_desc",
    )
    metadata = catalog.get("metadata", {})
    provenance: dict[str, Any] = {
        "catalog": "search",
        "source_url": search_provenance["query_url"],
        "count_url": search_provenance["count_url"],
        "catalog_artifact_name": SEARCH_ARTIFACT_NAME,
        "catalog_artifact_version": selected_version,
        "source_generated_at": _epoch_ms_to_utc(metadata.get("generated")),
        "fetched_at": search_provenance["fetched_at"],
        "query": search_provenance["query"],
        "search_total_matched": search_provenance["total_matched"],
        "search_stored_count": search_provenance["stored_count"],
        "truncated": search_provenance["truncated"],
        "total_catalog_events": len(catalog["features"]),
        "total_matched": len(selected),
        "skipped_invalid": skipped_invalid,
        "deduplicated_count": duplicates,
    }
    notice = search_provenance.get("truncation_notice")
    if isinstance(notice, str):
        provenance["truncation_notice"] = notice
    if not selected:
        return {"status": "empty", **provenance}

    map_events = [
        MapEvent(
            coord=event["coord"],
            label=_dense_event_label(event),
            latitude_radius=_dense_event_radius(event["magnitude"]),
            color=_dense_event_color(event["magnitude"]),
        )
        for event in selected
    ]
    try:
        image_bytes, spec, warnings, render_skipped = _render_map(
            map_events,
            crop_to_drawn_area=crop_to_drawn_area,
            legend=DENSE_MAP_MAGNITUDE_LEGEND,
            caption=caption,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        return {"status": "error", "error": str(exc), **provenance}
    if isinstance(notice, str):
        warnings.insert(0, notice)

    rendered_count = len(spec["events"])
    spec["event_count"] = rendered_count
    spec["event_source"] = {
        "kind": "usgs_historical_search_artifact",
        **provenance,
        "filters": {
            "min_magnitude": min_magnitude,
            "start_time": start_time,
            "end_time": end_time,
        },
        "style": {
            "color": "magnitude_bins",
            "radius": "magnitude_scaled",
            "labels": f"magnitude >= {DENSE_MAP_LABEL_MIN_MAGNITUDE:g}",
        },
    }
    spec["events"] = []

    image_name, spec_name = names
    spec_bytes = json.dumps(spec, indent=2, sort_keys=True).encode("utf-8")
    try:
        image_version = await tool_context.save_artifact(
            image_name,
            types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
            custom_metadata={
                "projection": "web_mercator",
                "event_count": rendered_count,
                "cropped": spec["crop"]["applied"],
                "legend_item_count": len(spec["legend"]["items"]),
                "captioned": spec["caption"] is not None,
                "catalog_artifact": SEARCH_ARTIFACT_NAME,
                "catalog_version": selected_version,
                "truncated": search_provenance["truncated"],
            },
        )
        spec_version = await tool_context.save_artifact(
            spec_name,
            types.Part.from_bytes(data=spec_bytes, mime_type="application/json"),
            custom_metadata={"map_artifact": image_name, "map_version": image_version},
        )
    except (OSError, ValueError) as exc:
        return {
            "status": "error",
            "error": f"Map was rendered but artifacts could not be saved: {exc}",
            **provenance,
        }

    tool_context.state["current_map_artifact"] = image_name
    tool_context.state["current_map_version"] = image_version
    tool_context.state["current_map_spec_artifact"] = spec_name
    tool_context.state["current_map_spec_version"] = spec_version
    return {
        "status": "ok",
        **provenance,
        "map_artifact_name": image_name,
        "map_artifact_version": image_version,
        "spec_artifact_name": spec_name,
        "spec_artifact_version": spec_version,
        "width": spec["width"],
        "height": spec["height"],
        "crop": spec["crop"],
        "bounds": spec["bounds"],
        "source_map": spec["source"]["artifact"],
        "map_viewport": spec["map_viewport"],
        "caption": spec["caption"],
        "legend": spec["legend"],
        "rendered_count": rendered_count,
        "skipped_count": render_skipped,
        "warnings": warnings,
    }


async def plot_data_points_on_map(
    events: list[MapEvent],
    artifact_name: str = DEFAULT_MAP_ARTIFACT,
    crop_to_drawn_area: bool = False,
    legend: MapLegend | None = None,
    caption: MapCaption | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Render supplied event circles onto the immutable canonical world map.

    Args:
        events: Circles with [longitude, latitude], label, latitude radius in
            degrees, and a Pillow-compatible color.
        artifact_name: Relative session-scoped PNG artifact name.
        crop_to_drawn_area: Crop the output to the smallest area containing all
            rendered circles and labels. Crops automatically use the first map
            source whose output long edge reaches 1400 pixels, when available.
            The full-world map remains the default.
        legend: Optional ordered color keys and heading supplied by the agent.
        caption: Optional title and date rendered above the map.

    Returns:
        Versioned PNG and map-spec artifact handles, dimensions, counts, and
        rendering warnings. This tool draws but does not select events.
    """
    if tool_context is None:
        return {"status": "error", "error": "Tool context is unavailable."}
    names = _artifact_names(artifact_name)
    if names is None:
        return {
            "status": "error",
            "error": "artifact_name must be a safe relative .png path.",
        }
    image_name, spec_name = names
    try:
        image_bytes, spec, warnings, skipped = _render_map(
            events,
            crop_to_drawn_area=crop_to_drawn_area,
            legend=legend,
            caption=caption,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}

    spec_bytes = json.dumps(spec, indent=2, sort_keys=True).encode("utf-8")
    try:
        image_version = await tool_context.save_artifact(
            image_name,
            types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
            custom_metadata={
                "projection": "web_mercator",
                "event_count": len(spec["events"]),
                "cropped": spec["crop"]["applied"],
                "legend_item_count": len(spec["legend"]["items"])
                if spec["legend"] is not None
                else 0,
                "captioned": spec["caption"] is not None,
            },
        )
        spec_version = await tool_context.save_artifact(
            spec_name,
            types.Part.from_bytes(data=spec_bytes, mime_type="application/json"),
            custom_metadata={"map_artifact": image_name, "map_version": image_version},
        )
    except (OSError, ValueError) as exc:
        return {
            "status": "error",
            "error": f"Map was rendered but artifacts could not be saved: {exc}",
        }

    tool_context.state["current_map_artifact"] = image_name
    tool_context.state["current_map_version"] = image_version
    tool_context.state["current_map_spec_artifact"] = spec_name
    tool_context.state["current_map_spec_version"] = spec_version
    return {
        "status": "ok",
        "map_artifact_name": image_name,
        "map_artifact_version": image_version,
        "spec_artifact_name": spec_name,
        "spec_artifact_version": spec_version,
        "width": spec["width"],
        "height": spec["height"],
        "crop": spec["crop"],
        "bounds": spec["bounds"],
        "source_map": spec["source"]["artifact"],
        "map_viewport": spec["map_viewport"],
        "caption": spec["caption"],
        "legend": spec["legend"],
        "rendered_count": len(spec["events"]),
        "skipped_count": skipped,
        "warnings": warnings,
    }


async def load_current_map_spec(
    artifact_version: int | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Load the current or a historical map specification for a stateful revision.

    Args:
        artifact_version: Optional exact version of the current specification
            artifact. Omit it to load the version referenced by session state.
    """
    if tool_context is None:
        return {"status": "error", "error": "Tool context is unavailable."}
    artifact_name = tool_context.state.get("current_map_spec_artifact")
    state_version = tool_context.state.get("current_map_spec_version")
    version = artifact_version if artifact_version is not None else state_version
    if not isinstance(artifact_name, str) or not isinstance(version, int):
        return {"status": "error", "error": "No current map specification exists."}
    part = await tool_context.load_artifact(artifact_name, version=version)
    if part is None:
        return {
            "status": "error",
            "error": f"Map specification {artifact_name} version {version} was not found.",
        }
    if part.inline_data and part.inline_data.data is not None:
        raw = bytes(part.inline_data.data)
    elif part.text is not None:
        raw = part.text.encode("utf-8")
    else:
        return {"status": "error", "error": "Current map specification is empty."}
    try:
        spec = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"status": "error", "error": f"Map specification is invalid: {exc}"}
    return {
        "status": "ok",
        "artifact_name": artifact_name,
        "artifact_version": version,
        "spec": spec,
    }
