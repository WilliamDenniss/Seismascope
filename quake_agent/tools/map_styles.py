"""Validated, reusable magnitude color scales for catalog maps."""

from __future__ import annotations

import math
from typing import Any, Literal

from PIL import ImageColor
from pydantic import BaseModel, ConfigDict, Field


class ColorStop(BaseModel):
    model_config = ConfigDict(extra="forbid")
    magnitude: float = Field(strict=True, allow_inf_nan=False)
    color: str


class MagnitudeBand(BaseModel):
    """Exclusive upper bound; the final band must have no upper bound."""

    model_config = ConfigDict(extra="forbid")
    upper_bound: float | None = Field(default=None, strict=True, allow_inf_nan=False)
    color: str


class CatalogMapStyle(BaseModel):
    """Color encoding only; catalog marker sizing remains independently bounded."""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["continuous", "bands"] = "continuous"
    palette: Literal["heat", "viridis", "blue"] | None = None
    min_magnitude: float | None = Field(default=None, strict=True, allow_inf_nan=False)
    max_magnitude: float | None = Field(default=None, strict=True, allow_inf_nan=False)
    color_stops: list[ColorStop] | None = Field(default=None, min_length=2, max_length=12)
    bands: list[MagnitudeBand] | None = Field(default=None, min_length=2, max_length=12)
    unknown_color: str = "#6b7280"
    out_of_range: Literal["clamp"] = "clamp"


PALETTES = {
    "heat": ("#ffffb2", "#fecc5c", "#fd8d3c", "#f03b20", "#bd0026"),
    "viridis": ("#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"),
    "blue": ("#eff3ff", "#bdd7e7", "#6baed6", "#3182bd", "#08519c"),
}


def _opaque_color(value: str) -> str:
    try:
        rgba = ImageColor.getcolor(value, "RGBA")
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid style color: {value!r}.") from exc
    if rgba[3] != 255:
        raise ValueError("Style colors must be opaque; marker translucency is automatic.")
    return "#{:02x}{:02x}{:02x}".format(*rgba[:3])


def resolve_catalog_style(style: CatalogMapStyle | dict | None) -> dict[str, Any]:
    """Resolve defaults to explicit stops so later maps can reuse exactly this scale."""
    style = CatalogMapStyle.model_validate(style if style is not None else {})
    result = {
        "mode": style.mode,
        "unknown_color": _opaque_color(style.unknown_color),
        "out_of_range": style.out_of_range,
    }
    if style.mode == "bands":
        if (style.bands is None or style.color_stops is not None
                or style.palette is not None or style.min_magnitude is not None
                or style.max_magnitude is not None):
            raise ValueError("Band styles require bands and cannot include gradient settings.")
        bounds = [band.upper_bound for band in style.bands]
        if bounds[-1] is not None or any(bound is None for bound in bounds[:-1]):
            raise ValueError("Only the final band must have a null upper_bound.")
        if any(a >= b for a, b in zip(bounds[:-2], bounds[1:-1])):
            raise ValueError("Band upper bounds must be strictly increasing.")
        result["bands"] = [
            {"upper_bound": band.upper_bound, "color": _opaque_color(band.color)}
            for band in style.bands
        ]
        return result
    if style.bands is not None:
        raise ValueError("Continuous styles cannot include bands.")
    if style.color_stops is not None:
        if style.palette is not None:
            raise ValueError("Choose a palette or custom color_stops, not both.")
        stops = [stop.model_dump() for stop in style.color_stops]
        low, high = stops[0]["magnitude"], stops[-1]["magnitude"]
        if ((style.min_magnitude is not None and style.min_magnitude != low)
                or (style.max_magnitude is not None and style.max_magnitude != high)):
            raise ValueError("Magnitude range must match the first and last color stops.")
    else:
        low = style.min_magnitude if style.min_magnitude is not None else 0.0
        high = style.max_magnitude if style.max_magnitude is not None else 9.0
        colors = PALETTES[style.palette or "heat"]
        stops = [
            {"magnitude": low + (high - low) * i / (len(colors) - 1), "color": color}
            for i, color in enumerate(colors)
        ]
    if not math.isfinite(high - low) or high <= low:
        raise ValueError("Magnitude range must be finite and strictly increasing.")
    if any(a["magnitude"] >= b["magnitude"] for a, b in zip(stops, stops[1:])):
        raise ValueError("Color stop magnitudes must be strictly increasing.")
    result.update(
        min_magnitude=low,
        max_magnitude=high,
        color_stops=[{**stop, "color": _opaque_color(stop["color"])} for stop in stops],
    )
    return result


def magnitude_color(magnitude: float | None, style: dict[str, Any]) -> str:
    if magnitude is None or not math.isfinite(magnitude):
        return style["unknown_color"]
    if style["mode"] == "bands":
        for band in style["bands"]:
            if band["upper_bound"] is None or magnitude < band["upper_bound"]:
                return band["color"]
    stops = style["color_stops"]
    if magnitude <= stops[0]["magnitude"]:
        return stops[0]["color"]
    for left, right in zip(stops, stops[1:]):
        if magnitude <= right["magnitude"]:
            fraction = ((magnitude - left["magnitude"])
                        / (right["magnitude"] - left["magnitude"]))
            rgb = [round(a + (b - a) * fraction) for a, b in zip(
                ImageColor.getrgb(left["color"]), ImageColor.getrgb(right["color"])
            )]
            return "#{:02x}{:02x}{:02x}".format(*rgb)
    return stops[-1]["color"]


def band_legend_items(style: dict[str, Any]) -> list[dict[str, str]]:
    items = []
    lower = None
    for band in style["bands"]:
        upper = band["upper_bound"]
        if lower is None:
            label = f"M < {upper:g}"
        elif upper is None:
            label = f"M >= {lower:g}"
        else:
            label = f"{lower:g} <= M < {upper:g}"
        items.append({"label": label, "color": band["color"]})
        lower = upper
    return items
