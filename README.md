# Quake Agent

A local, stateful Google ADK agent that downloads the official USGS hourly and
monthly earthquake feeds, stores versioned snapshots, queries them, and renders
event overlays on `static/world_map.png`.

## Run locally

```bash
python3 -m uv sync
cp .env.example .env
python3 -m uv run adk web .
```

ADK's local launcher stores SQLite sessions and filesystem artifacts beneath
the agent's `.adk/` directory. Reuse the same user and session in the ADK web UI
to continue an investigation after restarting the server.

Example prompts:

- Download the hourly feed and map earthquakes of magnitude 2 or greater.
- Use the monthly feed and show the ten largest events.
- Keep the same circles, make the deepest events blue, and remove other labels.
- Crop the current map to the area containing the circles, with 48 pixels of padding.
- Refresh the hourly data and tell me whether any mapped events changed.

## Test

```bash
python3 -m uv run pytest
```
