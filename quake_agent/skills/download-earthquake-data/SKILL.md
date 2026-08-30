---
name: download-earthquake-data
description: Download and query official USGS hourly or monthly earthquake GeoJSON when a user asks for current events, recent context, filtering, comparison, or data for a map.
metadata:
  adk_additional_tools:
    - download_usgs_feed
    - query_usgs_feed
---

# Download Earthquake Data

Use `hourly` for the most recent activity and `monthly` only when the user needs
30-day context or explicitly requests it.

1. Call `download_usgs_feed` for the required feed. Let the tool apply its
   freshness policy unless the user explicitly requests a refresh.
2. Check `status`, `cached`, and `stale`. If stale data is returned, disclose its
   fetch time and the refresh error.
3. Call `query_usgs_feed` to obtain only the bounded subset needed for the task.
   Never put the complete monthly catalog into model context.
4. Preserve the artifact name and version when describing or mapping results.

All times are UTC. Treat the monthly feed as a 30-day comparison window, not a
historical baseline. Do not make predictions or hazard claims.

Read [references/usgs-geojson.md](references/usgs-geojson.md) only when USGS
field semantics or feed limitations matter to the response.

