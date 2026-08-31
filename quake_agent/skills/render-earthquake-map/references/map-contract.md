# World-map rendering contract

The immutable standard base image is `static/world_map.png`, exactly 2048 by
2048 pixels. `static/world_map_2x.png` and `static/world_map_4x.png` are the
corresponding 4096 by 4096 and 8192 by 8192 pixel bases used for progressively
smaller crops. All three are canonical full-world Web Mercator maps spanning
longitudes −180° through 180° and latitudes −85.05112878° through 85.05112878°.

Each event has this shape:

```json
{
  "coord": [-122.31, 37.82],
  "label": "M4.2 near San Francisco",
  "latitude_radius": 1.5,
  "color": "#ef4444"
}
```

Colors may be Pillow-compatible names, hex values, or RGBA values. Circles are
translucent with opaque outlines. Larger circles are drawn first, circles wrap
across the antimeridian, and labels use deterministic collision-aware placement.

When colors communicate categories or ranges, the agent can supply an explicit
legend alongside the events:

```json
{
  "title": "Magnitude",
  "items": [
    {"label": "M 2.0–2.9", "color": "#facc15"},
    {"label": "M 3.0–3.9", "color": "#f97316"},
    {"label": "M 4.0+", "color": "#dc2626"}
  ]
}
```

The title is optional and item order is preserved. Every item must have a
non-blank label and a valid Pillow-compatible color. The renderer draws
matching translucent circular swatches in a semi-opaque inset after the final
map crop and resolution are selected. It scores the four corners against the
rendered circles and labels, chooses the corner with the least overlap, and
prefers the bottom-right when scores tie. If the complete legend cannot fit,
rendering fails instead of saving a partial or misleading legend. The legend
does not change the geographic crop, bounds, source selection, or output size.
The saved map specification records the legend title, ordered items, and chosen
corner so a later revision can preserve its semantics.

Set `crop_to_drawn_area=true` to crop the result to the smallest pixel area that
contains every rendered circle and placed label. `crop_padding_px` defaults to
32 pixels and may be set from 0 through 1024. Horizontal cropping treats the
world map as circular: a crop around the antimeridian stitches the map's right
and left edges and reports crossing bounds with `west > east`. The saved map
spec records the original map dimensions, output dimensions, visible bounds,
crop rectangle, padding, and whether antimeridian stitching was used.

For a requested crop, the renderer considers the 2048, 4096, and 8192 pixel
sources in that order and selects the first whose cropped output has a long edge
of at least 1400 pixels. If even the 8192 pixel source cannot meet the target,
the renderer preserves the requested crop, uses that largest source, and emits
a warning. `crop_padding_px` is expressed in standard-map pixels and is scaled
for the selected source; the saved spec records both padding values, the target
and whether it was met, and the selected source map.

The circle is a screen-space circle whose pixel radius comes from the projected
north/south latitude span. It is not a geodesic circle on Earth's surface.
