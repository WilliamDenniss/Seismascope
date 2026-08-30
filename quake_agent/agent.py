"""Google ADK entry point for the seismic analyst."""

from __future__ import annotations

import os
from pathlib import Path

from google.adk import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools.skill_toolset import SkillToolset

from .tools.data_tools import download_usgs_feed
from .tools.data_tools import query_usgs_feed
from .tools.map_tools import load_current_map_spec
from .tools.map_tools import render_events_on_world_map


_PACKAGE_DIR = Path(__file__).resolve().parent
_SKILLS_DIR = _PACKAGE_DIR / "skills"

_data_skill = load_skill_from_dir(_SKILLS_DIR / "download-earthquake-data")
_map_skill = load_skill_from_dir(_SKILLS_DIR / "render-earthquake-map")

_skill_toolset = SkillToolset(
    skills=[_data_skill, _map_skill],
    additional_tools=[
        download_usgs_feed,
        query_usgs_feed,
        render_events_on_world_map,
        load_current_map_spec,
    ],
)

root_agent = Agent(
    name="seismic_analyst",
    model=os.getenv("QUAKE_AGENT_MODEL", "gemini-flash-latest"),
    description=(
        "Downloads official USGS earthquake catalogs and creates reproducible, "
        "versioned world-map visualizations."
    ),
    instruction="""
You are a careful seismic-data analyst for a curious general audience.

Use the available filesystem skills for earthquake data and map rendering. Load
the relevant skill before trying to use its tools, and follow the skill's
workflow. Prefer saved, fresh artifacts for follow-up work. Report the feed,
source generation time, fetch time, cache/staleness status, and output artifact
whenever you create a map.

Treat the monthly feed as a 30-day comparison window, not a historical baseline.
Do not predict earthquakes, make hazard claims, or infer tectonic causation from
catalog patterns. Distinguish observations from interpretations and state data
limitations plainly.
""".strip(),
    tools=[_skill_toolset],
)

