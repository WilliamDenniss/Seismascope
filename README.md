# Quake Agent

A local, stateful Google ADK agent that downloads the official USGS hourly and
monthly earthquake feeds, stores versioned snapshots, queries them, and renders
event overlays on `static/world_map.png`.

## Run locally with the ADK development UI

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

## Run the public web app

The production-facing app serves a small browser interface and ADK's API from
one FastAPI process. Model text streams into the active response over SSE, tool
names appear below the analyzing indicator until the response starts, and
generated map artifacts are displayed inline in the chat.

```bash
python3 -m uv sync
cp .env.example .env
python3 -m uv run python -m web.app
```

Open `http://localhost:8080`. Sessions and artifacts are intentionally kept in
memory for this public-demo configuration and can reset when the process is
restarted.

Runtime configuration:

- `QUAKE_AGENT_API_BASE_URL`: Browser-visible API base URL. Leave empty to use
  the same origin as the web page.
- `HOST` and `PORT`: FastAPI bind address and port. Cloud Run supplies `PORT`.
- `QUAKE_AGENT_ALLOWED_ORIGINS`: Optional comma-separated CORS origins when the
  browser UI and API are hosted separately.
- `QUAKE_AGENT_RATE_LIMIT` and `QUAKE_AGENT_RATE_WINDOW_SECONDS`: Per-process,
  per-IP prompt limit. Defaults to 10 prompts per 600 seconds.
- `GOOGLE_API_KEY` and `QUAKE_AGENT_MODEL`: Server-side Gemini credentials and
  model selection.

## Docker and Cloud Run

Build and run the same image used for deployment:

```bash
docker build -t quakeagent .
docker run --rm -p 8080:8080 --env-file .env quakeagent
```

The repository-root `Dockerfile` packages the agent, its skills, the canonical
world map, and the web app into one image. For a first public Cloud Run demo:

```bash
gcloud run deploy quake-agent \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --max-instances 1 \
  --concurrency 8 \
  --timeout 300 \
  --set-secrets GOOGLE_API_KEY=GOOGLE_API_KEY:latest
```

Leave `QUAKE_AGENT_API_BASE_URL` unset for this single-service deployment. Store
`GOOGLE_API_KEY` in Secret Manager rather than passing it as a browser-visible
or plain-text environment value.

## Test

```bash
python3 -m uv run pytest
```
