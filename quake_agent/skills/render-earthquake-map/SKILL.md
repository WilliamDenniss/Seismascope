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
- Before rendering a new map, check whether the selected event set is empty. If
  no events match the user's filters, do not call `render_events_on_world_map`
  or create map artifacts. Report the zero-result finding and catalog
  provenance instead. Render an empty base map only when the user explicitly
  requests one.
- Prefer a cropped presentation for every non-empty new map: set
  `crop_to_drawn_area=true` unless the user explicitly requests a full-world or
  uncropped view. Treat cropping as opt-out and do not ask for confirmation.
  A global or worldwide request, a comparison, or events spanning most of the
  world does not by itself request full-world framing; let the renderer produce
  a broad or nearly full-world crop when that is what the drawn content needs.
- Use enough `crop_padding_px` to preserve geographic context: approximately 96
  pixels for broad regions and 48 pixels for smaller regions. Pacific and other
  antimeridian-crossing regions should still be cropped; the renderer handles
  antimeridian stitching.
- Coordinates are always `[longitude, latitude]`.
- `latitude_radius` is a positive angular latitude span in degrees. It controls
  visual marker size and is not a geodesic distance or hazard radius.
- Choose colors and labels to match the user's request. Do not invent a
  magnitude-to-radius rule unless the user asks for one; if you choose a visual
  encoding, explain it.
- Whenever color communicates categories or ranges, pass a `legend` with an
  optional short `title` and ordered `items` containing `label` and `color`.
  Use the same Pillow-compatible color values as the corresponding events. A
  legend is not required for one-off highlighting unless the user requests it.
- For a follow-up modification, call `load_current_map_spec`, edit its event
  array, and pass the complete revised array back to the renderer. Preserve the
  saved crop request, padding, and legend semantics unless the user asks to
  change them. Update or remove the legend when its color encoding changes;
  when reusing a saved legend, pass its `title` and `items` rather than its
  renderer-selected `corner`.
- Report both the PNG artifact and its source-specification artifact, including
  their versions and any renderer warnings.

Read [references/map-contract.md](references/map-contract.md) only when you need
projection, color, wrapping, or label-placement details.
