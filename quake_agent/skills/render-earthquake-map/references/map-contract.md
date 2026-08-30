# World-map rendering contract

The immutable base image is `static/world_map.png`, exactly 2048 by 2048 pixels.
It is treated as a canonical full-world Web Mercator map spanning longitudes
−180° through 180° and latitudes −85.05112878° through 85.05112878°.

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

The circle is a screen-space circle whose pixel radius comes from the projected
north/south latitude span. It is not a geodesic circle on Earth's surface.

