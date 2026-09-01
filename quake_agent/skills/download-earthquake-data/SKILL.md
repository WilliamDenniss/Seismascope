---
name: download-earthquake-data
description: Download and query official USGS earthquake GeoJSON from realtime feeds or bounded historical Event API searches for analysis and maps.
metadata:
  adk_additional_tools:
    - download_usgs_feed
    - query_usgs_feed
    - search_usgs_events
    - query_usgs_search
---

# Download Earthquake Data

Use `hourly` for the most recent activity and `monthly` only when the user needs
30-day context or explicitly requests it. Use `search_usgs_events` for longer or
explicitly dated periods.

1. Call `download_usgs_feed` for the required feed. Let the tool apply its
   freshness policy unless the user explicitly requests a refresh.
2. Check `status`, `cached`, and `stale`. If stale data is returned, disclose its
   fetch time and the refresh error.
3. Call `query_usgs_feed` to obtain only the bounded subset needed for analysis
   or a small selected-event map. Never put the complete monthly catalog into
   model context. When the user wants the whole feed mapped, skip the query and
   give the compact feed artifact handle to `plot_usgs_feed_on_map`.
4. Preserve the artifact name and version when describing or mapping results.

For a historical search:

1. Resolve relative periods such as "last five years" to explicit UTC
   `start_time` and `end_time` values. Supply a complete `circle` for a radius
   search, or omit it for a worldwide search.
2. Call `search_usgs_events`. The tool count-checks the request, stores the
   strongest 20,000 events when more than 20,000 match, and returns compact
   provenance rather than an event array.
3. Use `query_usgs_search` only for a bounded listing or small selected-event
   map. Use the exact search artifact version with `plot_usgs_search_on_map`
   when mapping the full stored result.
4. Disclose the exact UTC period and, for a named place, the coordinate and
   radius supplied to the tool. If `truncated` is true, repeat the tool's
   truncation notice and never call the stored set all matching events.

All times are UTC. Treat the monthly feed as a 30-day comparison window, not a
historical baseline. Historical catalog completeness and magnitude consistency
vary by era and region. Do not make predictions or hazard claims.

Read [references/usgs-geojson.md](references/usgs-geojson.md) only when USGS
field semantics or feed limitations matter to the response.
