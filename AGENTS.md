# Project

Seismascope is a Python 3.12 Google ADK application with a FastAPI public web
interface. Agent behavior lives under `quake_agent/`, the browser application
lives under `web/`, and automated tests live under `tests/`.

# Development

- Keep agent tools focused on official USGS earthquake data and map rendering.
- Preserve the public API restrictions, prompt limits, and safe error handling
  in `web/app.py` when changing the web surface.
- Keep browser behavior in `web/static/index.html` dependency-free unless a new
  dependency is clearly justified.
- Add or update tests for behavior changes.

# Validation

Run the test suite with:

```bash
python3 -m uv run pytest
```

# Source control

Never stage or unstage anything unless asked.

When prompted to generate a commit message, provide it in the following format:

```text
Commit title

- Changed feature 1
- Fix 2

Prompts:

> Prompt 1
> Prompt 2
```
