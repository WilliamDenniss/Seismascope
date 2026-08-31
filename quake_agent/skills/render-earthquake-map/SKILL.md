---
name: render-earthquake-map
description: Render a supplied set of labeled event circles on the canonical world-map PNG, or revise the latest saved map specification. Use only after the relevant events have been selected.
metadata:
  adk_additional_tools:
    - render_events_on_world_map
    - load_current_map_spec
---

# Render Earthquake Map

This skill draws events; it does not decide which earthquakes are relevant.

- For a new map, call `render_events_on_world_map` with objects containing
  `coord`, `label`, `latitude_radius`, and `color`.
- Infer framing from the requested geographic scope. When a map request names a
  subglobal region—such as the Pacific, Ring of Fire, Alaska, California, Japan,
  or the Mediterranean—set `crop_to_drawn_area=true` unless the user explicitly
  requests a full-world view or worldwide comparison. Do not ask for
  confirmation.
- Use enough `crop_padding_px` to preserve geographic context: approximately 96
  pixels for broad regions and 48 pixels for smaller regions. Pacific and other
  antimeridian-crossing regions should still be cropped; the renderer handles
  antimeridian stitching.
- For a crop occupying less than `high_resolution_crop_threshold_percent` of
  the global map's pixel area, use `static/world_map_4x.png`. The renderer makes
  this selection automatically and defaults the threshold to 6.25%. Preserve
  the saved threshold on follow-up revisions unless the user asks to change it.
- Keep the full-world default for global requests or when the selected events
  intentionally span most of the world.
- Coordinates are always `[longitude, latitude]`.
- `latitude_radius` is a positive angular latitude span in degrees. It controls
  visual marker size and is not a geodesic distance or hazard radius.
- Choose colors and labels to match the user's request. Do not invent a
  magnitude-to-radius rule unless the user asks for one; if you choose a visual
  encoding, explain it.
- For a follow-up modification, call `load_current_map_spec`, edit its event
  array, and pass the complete revised array back to the renderer. Preserve the
  saved crop request and padding unless the user asks to change the framing.
- Report both the PNG artifact and its source-specification artifact, including
  their versions and any renderer warnings.

Read [references/map-contract.md](references/map-contract.md) only when you need
projection, color, wrapping, or label-placement details.
