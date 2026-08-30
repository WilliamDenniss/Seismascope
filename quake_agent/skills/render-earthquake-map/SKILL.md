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
- Coordinates are always `[longitude, latitude]`.
- `latitude_radius` is a positive angular latitude span in degrees. It controls
  visual marker size and is not a geodesic distance or hazard radius.
- Choose colors and labels to match the user's request. Do not invent a
  magnitude-to-radius rule unless the user asks for one; if you choose a visual
  encoding, explain it.
- For a follow-up modification, call `load_current_map_spec`, edit its event
  array, and pass the complete revised array back to the renderer.
- Report both the PNG artifact and its source-specification artifact, including
  their versions and any renderer warnings.

Read [references/map-contract.md](references/map-contract.md) only when you need
projection, color, wrapping, or label-placement details.

