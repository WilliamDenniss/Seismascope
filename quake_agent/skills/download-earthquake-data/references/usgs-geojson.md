# USGS GeoJSON reference

The skill uses only these allowlisted summary feeds:

- Hourly: `https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson`
- Monthly: `https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_month.geojson`

Historical searches use only these fixed USGS FDSN Event API endpoints:

- Count: `https://earthquake.usgs.gov/fdsnws/event/1/count`
- Query: `https://earthquake.usgs.gov/fdsnws/event/1/query`

Historical searches always provide explicit UTC start and end times and may
provide a latitude/longitude circle in kilometers. USGS limits one query to
20,000 events; larger matches are stored as the strongest 20,000 and must be
reported as truncated. Catalog completeness and magnitude consistency vary by
era and region.

Normalized event fields:

- `id`: stable USGS event identifier within the catalog
- `coord`: `[longitude, latitude]`
- `google_maps_url`: Google Maps link with a pin at the coordinate and zoom level 6
- `depth_km`: third GeoJSON coordinate, in kilometers
- `magnitude`: USGS `mag`, which may be absent
- `place`: USGS human-readable location, which may be absent
- `time`: event origin time converted from epoch milliseconds to UTC
- `significance`: USGS `sig`, which is not a probability or forecast
- `detail_url`: link to the fuller per-event USGS GeoJSON record

USGS can revise an event's magnitude, position, metadata, or status. Artifact
versions preserve the exact summary feed used for each answer.
