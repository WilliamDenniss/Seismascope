# USGS GeoJSON reference

The skill uses only these allowlisted summary feeds:

- Hourly: `https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson`
- Monthly: `https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_month.geojson`

Normalized event fields:

- `id`: stable USGS event identifier within the catalog
- `coord`: `[longitude, latitude]`
- `depth_km`: third GeoJSON coordinate, in kilometers
- `magnitude`: USGS `mag`, which may be absent
- `place`: USGS human-readable location, which may be absent
- `time`: event origin time converted from epoch milliseconds to UTC
- `significance`: USGS `sig`, which is not a probability or forecast
- `detail_url`: link to the fuller per-event USGS GeoJSON record

USGS can revise an event's magnitude, position, metadata, or status. Artifact
versions preserve the exact summary feed used for each answer.

