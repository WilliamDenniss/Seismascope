# World-map rendering contract

The immutable standard base image is `static/world_map.png`, exactly 2048 by
2048 pixels. `static/world_map_2x.png` and `static/world_map_4x.png` are the
corresponding 4096 by 4096 and 8192 by 8192 pixel bases used for progressively
smaller crops. Four quadrant images named `static/world_map_8x_{nw,ne,sw,se}.png`
form an effective 16384 by 16384 source. Sixteen images named
`static/world_map_16x_x{0-3}_y{0-3}.png` form an effective 32768 by 32768 source,
with X increasing west to east and Y increasing north to south. Every tile is
8192 by 8192 pixels. All levels are canonical full-world Web Mercator maps
spanning longitudes −180° through 180° and latitudes −85.05112878° through
85.05112878°.

Each event has this shape:

```json
{
  "coord": [-122.31, 37.82],
  "label": "M4.2 near San Francisco",
  "latitude_radius": 1.5,
  "color": "#ef4444"
}
```

Catalog-scale maps use the same projection and marker renderer but store an
`event_source` reference to the exact USGS catalog artifact and resolved
magnitude styling instead of copying every marker into the map specification.
This keeps both the model tool call and later specification loads bounded. The
renderer defaults to a continuous heat gradient from M0 through M9; uses gray
when magnitude is unavailable; scales a default 5–18 pixel circle radius by
magnitude; and labels only magnitude 6+ events. The agent may apply a bounded
`marker_scale` from 0.6 through 1.5, with an absolute 3–24 pixel safety range.
The default is 1.0. Catalog marker radius and its 1–4 pixel outline are
screen-space styling: basemap resolution and crop-only enlargement do not
change them, and their visual radius does not expand the geographic event-center
extent used for framing. The saved `event_source.style.radius` records the
chosen scale and the effective sizing formula.

Both catalog tools accept `style` for colors. It is optional for API backward
compatibility, but the agent must explicitly choose and pass a mode for new maps.
Bands support distinguishable magnitude groups; continuous scales support fine
ordered variation. Choose using the question and magnitude summary. The fallback
gradient is not an agent design recommendation. Examples:

```json
{"mode": "continuous", "palette": "heat", "min_magnitude": 4, "max_magnitude": 9}
```

```json
{"mode": "continuous", "color_stops": [{"magnitude": 4, "color": "#ffffb2"}, {"magnitude": 6, "color": "#fd8d3c"}, {"magnitude": 9, "color": "#bd0026"}]}
```

```json
{"mode": "bands", "bands": [{"upper_bound": 4, "color": "#ffffb2"}, {"upper_bound": 6, "color": "#fd8d3c"}, {"upper_bound": null, "color": "#bd0026"}]}
```

Curated palettes are `heat`, `viridis`, and `blue`. Continuous scales interpolate
RGB channels between 2-12 strictly increasing finite magnitude stops. A palette
uses an explicit range (default 0-9); custom stops define their own range, and
any supplied range must match their endpoints. Do not mix palettes and stops.
`out_of_range` is `clamp`: values below/above the scale use its endpoint colors.
The color bar marks its endpoints with <= and >= to disclose this behavior.
Bands use 2-12 strictly increasing exclusive upper bounds; only the final bound
is null. The first band extends down without limit and the final band extends
up without limit. An earthquake exactly on a threshold belongs to the next band.
Labels describing these intervals and matching swatches are generated from the
bands, so there are no uncovered values or overlapping intervals.

`unknown_color` defaults to #6b7280 and is always shown separately. Catalog
style colors must be opaque Pillow-compatible color strings; the renderer adds
its existing marker translucency. Invalid colors, nonfinite or unordered stops,
conflicting settings, and unsupported fields fail before artifacts are saved.
Marker sizing, opacity, label thresholds, and highlighting are outside this
first color-style interface.

The compact `magnitude_summary` in feed/search handles covers valid, deduplicated
stored events; filtered queries and renderer results summarize the complete
matching selection before any listing limit. Its fixed histogram intervals are
lower-inclusive and upper-exclusive; null endpoints extend without limit.
For truncated historical searches, it describes only the stored subset.

The saved `event_source.style.color` and returned `style` contain explicit
normalized stops or band thresholds, colors, range, unknown color, and clamping
policy. Pass that object as `style` on a revision or comparison, preserving
`event_source.style.radius.scale` separately as `marker_scale`. A continuous
legend saves the same object in `legend.scale`; bands save generated ordered
items. A revision may move the legend corner while retaining its semantics.
Catalog color bars and swatches use the same collision-aware panel placement
and fit checks as explicit legends. They do not change the map viewport.

Colors may be Pillow-compatible names, hex values, or RGBA values. Circles are
translucent with opaque outlines. Smaller circles are drawn first so larger
circles appear on top; circles wrap across the antimeridian, and labels use
deterministic collision-aware placement. Label typography and spacing are
sized from the final rendered canvas rather than the selected base-map scale.

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

The agent may also supply an optional caption that is embedded in the PNG:

```json
{
  "title": "Magnitude 4+ Earthquakes near New Zealand",
  "date": "August 1-31, 2026"
}
```

Both values must be non-blank, single-line display strings. The date is
deliberately not restricted to one date format so it can represent a day,
range, month, or "as of" timestamp. The renderer adds the caption in a header
above the completed map so it does not cover event markers, labels, the legend,
or base-map attribution. A caption does not affect geographic cropping, bounds,
source-map selection, or the minimum-resolution calculation. The saved map
specification records the caption and a `map_viewport` rectangle that locates
the geographic map within the taller captioned PNG.

Set `crop_to_drawn_area=true` to crop the result around the rendered circles.
The renderer calculates context padding from the marker-only long edge: 50
percent per side, clamped from 16 through 96 standard-map pixels. Labels do not
affect geographic bounds;
the renderer places them within the selected viewport and warns when one cannot
fit without overlap. Horizontal cropping treats the world map as circular: a
crop around the antimeridian stitches the map's right and left edges and reports
crossing bounds with `west > east`. The saved map spec records the marker extent,
calculated padding, crop rectangle, dimensions, visible bounds, and wrapping
behavior.

For a requested crop, the renderer considers the 2048, 4096, 8192, effective
16384, and effective 32768 pixel sources in that order and selects the first
whose cropped output has a long edge of at least 1400 pixels. Tiled sources load
and stitch only intersecting images, including across the antimeridian. If even
the 32768 source cannot meet the target, the renderer enlarges only the cropped
base image with Lanczos resampling by up to 4x, then redraws markers and labels
at the output resolution. Marker centers follow the enlarged viewport, but
their radii and outlines remain at the selected source's native scale so raster
interpolation cannot turn nearby earthquakes into overlapping blobs. The
renderer warns when the enlarged result still cannot reach the target. The
saved spec records the standard and source padding, native crop dimensions,
enlargement factor, target, whether it was met, selected source map, and tile
artifacts.

For agent-supplied events, the circle's pixel radius comes from the projected
north/south `latitude_radius` span. It is not a geodesic circle on Earth's
surface. Catalog-backed maps instead use the bounded screen-space magnitude
styling described above.
