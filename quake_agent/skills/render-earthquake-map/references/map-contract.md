# World-map rendering contract

The immutable standard base image is `static/world_map.png`, exactly 2048 by
2048 pixels. `static/world_map_4x.png` is the corresponding 8192 by 8192 pixel
base used for small crops. Both are canonical full-world Web Mercator maps
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

Colors may be Pillow-compatible names, hex values, or RGBA values. Circles are
translucent with opaque outlines. Larger circles are drawn first, circles wrap
across the antimeridian, and labels use deterministic collision-aware placement.

Set `crop_to_drawn_area=true` to crop the result to the smallest pixel area that
contains every rendered circle and placed label. `crop_padding_px` defaults to
32 pixels and may be set from 0 through 1024. Horizontal cropping treats the
world map as circular: a crop around the antimeridian stitches the map's right
and left edges and reports crossing bounds with `west > east`. The saved map
spec records the original map dimensions, output dimensions, visible bounds,
crop rectangle, padding, and whether antimeridian stitching was used.

The renderer measures the padded crop area as a percentage of the full map. If
that percentage is strictly below `high_resolution_crop_threshold_percent`, it
renders the crop from `static/world_map_4x.png`; the default threshold is 6.25%.
Set the threshold to 0 to disable high-resolution selection. `crop_padding_px`
is expressed in standard-map pixels and is scaled by four for the 4x source;
the saved spec records both values and the selected source map.

The circle is a screen-space circle whose pixel radius comes from the projected
north/south latitude span. It is not a geodesic circle on Earth's surface.
