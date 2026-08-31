"""Render declarative event overlays onto the supplied world map."""

from __future__ import annotations

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


WEB_MERCATOR_MAX_LAT = 85.05112878
EXPECTED_MAP_SIZE = (2048, 2048)
BASE_MAP_PATH = Path(__file__).resolve().parents[2] / "static" / "world_map.png"
TWO_X_MAP_SIZE = (4096, 4096)
TWO_X_BASE_MAP_PATH = (
    Path(__file__).resolve().parents[2] / "static" / "world_map_2x.png"
)
HIGH_RESOLUTION_MAP_SIZE = (8192, 8192)
HIGH_RESOLUTION_BASE_MAP_PATH = (
    Path(__file__).resolve().parents[2] / "static" / "world_map_4x.png"
)
DEFAULT_TWO_X_CROP_THRESHOLD_PERCENT = 25.0
DEFAULT_HIGH_RESOLUTION_CROP_THRESHOLD_PERCENT = 6.25
DEFAULT_MAP_ARTIFACT = "earthquake-map.png"
DEFAULT_CROP_PADDING_PX = 32
MAX_CROP_PADDING_PX = 1024
_SAFE_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*\.png$")


class MapEvent(BaseModel):
    """A declarative map circle whose coordinate order is longitude, latitude."""

    coord: list[float] = Field(min_length=2, max_length=2)
    label: str
    latitude_radius: float
    color: str


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


def _label_candidates(
    x: float,
    y: float,
    radius: float,
    text_width: int,
    text_height: int,
) -> list[tuple[float, float]]:
    gap = 7
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


def _crop_area_percent(
    crop: dict[str, int | bool],
    source_width: int,
    source_height: int,
) -> float:
    return (
        int(crop["width"])
        * int(crop["height"])
        / (source_width * source_height)
        * 100
    )


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
    source_path: Path,
    source_size: tuple[int, int],
) -> tuple[Image.Image, dict[str, int | bool]]:
    scale = source_size[0] // EXPECTED_MAP_SIZE[0]
    scaled_crop = _scaled_crop(standard_crop, scale)
    if not source_path.is_file():
        raise FileNotFoundError(f"Scaled base map not found at {source_path}.")
    with Image.open(source_path) as source:
        if source.size != source_size:
            raise ValueError(
                "Scaled base map must be "
                f"{source_size[0]}x{source_size[1]}, "
                f"not {source.width}x{source.height}."
            )
        rendered = _crop_wrapped_image(source, scaled_crop).convert("RGB")

    crop_x = int(scaled_crop["source_x"])
    crop_y = int(scaled_crop["source_y"])
    crop_width = int(scaled_crop["width"])
    draw = ImageDraw.Draw(rendered, "RGBA")
    outline_width = max(2, round(source_size[0] / 1024))
    for item in prepared:
        radius = item["radius_px"] * scale
        y = item["y"] * scale - crop_y
        for x in _viewport_x_positions(
            item["x"] * scale,
            radius,
            crop_x,
            crop_width,
            source_size[0],
        ):
            draw.ellipse(
                (x - radius, y - radius, x + radius, y + radius),
                fill=item["fill"],
                outline=item["outline"],
                width=outline_width,
            )

    font = ImageFont.load_default(size=10 * scale)
    for placed in placed_labels:
        text_box = draw.textbbox(
            (0, 0), placed["label"], font=font, stroke_width=outline_width
        )
        text_width = text_box[2] - text_box[0]
        full_x = placed["x"] * scale
        candidates = _viewport_x_positions(
            full_x,
            text_width,
            crop_x,
            crop_width,
            source_size[0],
        )
        if not candidates:
            continue
        draw.text(
            (candidates[0], placed["y"] * scale - crop_y),
            placed["label"],
            font=font,
            fill=(20, 20, 20, 255),
            stroke_width=outline_width,
            stroke_fill=(255, 255, 255, 235),
        )
    return rendered, scaled_crop


def _render_map(
    events: list[MapEvent],
    crop_to_drawn_area: bool = False,
    crop_padding_px: int = DEFAULT_CROP_PADDING_PX,
    high_resolution_crop_threshold_percent: float = (
        DEFAULT_HIGH_RESOLUTION_CROP_THRESHOLD_PERCENT
    ),
    two_x_crop_threshold_percent: float = DEFAULT_TWO_X_CROP_THRESHOLD_PERCENT,
) -> tuple[bytes, dict[str, Any], list[str], int]:
    if not BASE_MAP_PATH.is_file():
        raise FileNotFoundError(f"Base map not found at {BASE_MAP_PATH}.")
    with Image.open(BASE_MAP_PATH) as source:
        if source.size != EXPECTED_MAP_SIZE:
            raise ValueError(
                f"Base map must be {EXPECTED_MAP_SIZE[0]}x{EXPECTED_MAP_SIZE[1]}, "
                f"not {source.width}x{source.height}."
            )
        image = source.convert("RGBA")

    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = ImageFont.load_default()
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
        fill_alpha = rgba[3] if rgba[3] < 255 else 96
        prepared.append(
            {
                "index": index,
                "coord": [normalized_longitude, clamped_latitude],
                "label": event.label,
                "latitude_radius": event.latitude_radius,
                "color": event.color,
                "rgba": rgba,
                "fill": (rgba[0], rgba[1], rgba[2], fill_alpha),
                "outline": (rgba[0], rgba[1], rgba[2], 255),
                "x": x,
                "y": y,
                "radius_px": radius,
            }
        )

    prepared.sort(key=lambda item: (-item["radius_px"], item["index"]))
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

    occupied_labels: list[tuple[float, float, float, float]] = []
    placed_labels: list[dict[str, Any]] = []
    for item in sorted(prepared, key=lambda value: value["index"]):
        label = item["label"].strip()
        if not label:
            continue
        label_x = item["x"] % width
        text_box = draw.textbbox((0, 0), label, font=font, stroke_width=2)
        text_width = text_box[2] - text_box[0]
        text_height = text_box[3] - text_box[1]
        chosen: tuple[float, float] | None = None
        chosen_box: tuple[float, float, float, float] | None = None
        for candidate in _label_candidates(
            label_x,
            item["y"],
            item["radius_px"],
            text_width,
            text_height,
        ):
            x, y = candidate
            candidate_box = (x, y, x + text_width, y + text_height)
            if x < 0 or y < 0 or candidate_box[2] > width or candidate_box[3] > height:
                continue
            if any(_boxes_overlap(candidate_box, prior) for prior in occupied_labels):
                continue
            chosen = candidate
            chosen_box = candidate_box
            break
        if chosen is None or chosen_box is None:
            warnings.append(f"Label for event {item['index']} could not be placed.")
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
        placed_labels.append({"label": label, "x": chosen[0], "y": chosen[1]})

    rendered = Image.alpha_composite(image, overlay).convert("RGB")
    crop: dict[str, int | bool] = {
        "source_x": 0,
        "source_y": 0,
        "width": width,
        "height": height,
        "wraps_antimeridian": False,
    }
    crop_applied = False
    if crop_to_drawn_area:
        content_crop = _content_crop(overlay, crop_padding_px)
        if content_crop is None:
            warnings.append(
                "Crop was requested but there was no drawable content; the full map was retained."
            )
        else:
            crop = content_crop
            crop_applied = crop["width"] != width or crop["height"] != height
            if crop_applied:
                rendered = _crop_wrapped_image(rendered, crop)

    crop_area_percent = _crop_area_percent(crop, width, height)
    source_path = BASE_MAP_PATH
    source_size = EXPECTED_MAP_SIZE
    source_padding_px = crop_padding_px
    if (
        crop_applied
        and crop_area_percent < high_resolution_crop_threshold_percent
    ):
        source_path = HIGH_RESOLUTION_BASE_MAP_PATH
        source_size = HIGH_RESOLUTION_MAP_SIZE
    elif crop_applied and crop_area_percent < two_x_crop_threshold_percent:
        source_path = TWO_X_BASE_MAP_PATH
        source_size = TWO_X_MAP_SIZE

    if source_path != BASE_MAP_PATH:
        rendered, crop = _render_scaled_crop(
            prepared,
            placed_labels,
            crop,
            source_path,
            source_size,
        )
        width, height = source_size
        source_padding_px = crop_padding_px * (width // EXPECTED_MAP_SIZE[0])

    output = BytesIO()
    rendered.save(output, format="PNG", optimize=True)
    spec = {
        "projection": "web_mercator",
        "bounds": _visible_bounds(crop, width, height),
        "source": {
            "artifact": str(source_path.relative_to(source_path.parents[1])),
            "width": width,
            "height": height,
            "bounds": {
                "west": -180,
                "east": 180,
                "north": WEB_MERCATOR_MAX_LAT,
                "south": -WEB_MERCATOR_MAX_LAT,
            },
            "scale": width // EXPECTED_MAP_SIZE[0],
        },
        "crop": {
            "requested": crop_to_drawn_area,
            "applied": crop_applied,
            "padding_px": crop_padding_px,
            "source_padding_px": source_padding_px,
            "area_percent_of_world": crop_area_percent,
            "high_resolution_threshold_percent": (
                high_resolution_crop_threshold_percent
            ),
            "two_x_threshold_percent": two_x_crop_threshold_percent,
            "used_high_resolution_source": source_path != BASE_MAP_PATH,
            **crop,
        },
        "width": rendered.width,
        "height": rendered.height,
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


async def render_events_on_world_map(
    events: list[MapEvent],
    artifact_name: str = DEFAULT_MAP_ARTIFACT,
    crop_to_drawn_area: bool = False,
    crop_padding_px: int = DEFAULT_CROP_PADDING_PX,
    high_resolution_crop_threshold_percent: float = (
        DEFAULT_HIGH_RESOLUTION_CROP_THRESHOLD_PERCENT
    ),
    two_x_crop_threshold_percent: float = DEFAULT_TWO_X_CROP_THRESHOLD_PERCENT,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Render supplied event circles onto the immutable canonical world map.

    Args:
        events: Circles with [longitude, latitude], label, latitude radius in
            degrees, and a Pillow-compatible color.
        artifact_name: Relative session-scoped PNG artifact name.
        crop_to_drawn_area: Crop the output to the smallest area containing all
            rendered circles and labels. The full-world map remains the default.
        crop_padding_px: Context to retain around drawn content when cropping.
        high_resolution_crop_threshold_percent: Use the 4x base map when a
            requested crop occupies less than this percentage of the world.
        two_x_crop_threshold_percent: Use the 2x base map when a requested crop
            is not eligible for 4x and occupies less than this percentage.

    Returns:
        Versioned PNG and map-spec artifact handles, dimensions, counts, and
        rendering warnings. This tool draws but does not select events.
    """
    if tool_context is None:
        return {"status": "error", "error": "Tool context is unavailable."}
    if (
        isinstance(crop_padding_px, bool)
        or not isinstance(crop_padding_px, int)
        or not 0 <= crop_padding_px <= MAX_CROP_PADDING_PX
    ):
        return {
            "status": "error",
            "error": (
                f"crop_padding_px must be an integer from 0 through "
                f"{MAX_CROP_PADDING_PX}."
            ),
        }
    if (
        isinstance(two_x_crop_threshold_percent, bool)
        or not isinstance(two_x_crop_threshold_percent, (int, float))
        or not math.isfinite(two_x_crop_threshold_percent)
        or not 0 <= two_x_crop_threshold_percent <= 100
    ):
        return {
            "status": "error",
            "error": (
                "two_x_crop_threshold_percent must be a finite number from "
                "0 through 100."
            ),
        }
    if (
        isinstance(high_resolution_crop_threshold_percent, bool)
        or not isinstance(high_resolution_crop_threshold_percent, (int, float))
        or not math.isfinite(high_resolution_crop_threshold_percent)
        or not 0 <= high_resolution_crop_threshold_percent <= 100
    ):
        return {
            "status": "error",
            "error": (
                "high_resolution_crop_threshold_percent must be a finite "
                "number from 0 through 100."
            ),
        }
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
            crop_padding_px=crop_padding_px,
            high_resolution_crop_threshold_percent=(
                high_resolution_crop_threshold_percent
            ),
            two_x_crop_threshold_percent=two_x_crop_threshold_percent,
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
