"""Google ADK entry point for the seismic analyst."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
import os
from pathlib import Path

from google.adk import Agent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.skills import load_skill_from_dir
from google.adk.tools import FunctionTool
from google.adk.tools.skill_toolset import SkillToolset

from .tools.data_tools import download_usgs_feed
from .tools.data_tools import query_usgs_feed
from .tools.data_tools import query_usgs_search
from .tools.data_tools import search_usgs_events
from .tools.geocoding_tools import geocode_place
from .tools.geography_tools import calculate_coordinate_distance
from .tools.map_tools import load_current_map_spec
from .tools.map_tools import plot_data_points_on_map
from .tools.map_tools import plot_usgs_feed_on_map
from .tools.map_tools import plot_usgs_search_on_map


_PACKAGE_DIR = Path(__file__).resolve().parent
_SKILLS_DIR = _PACKAGE_DIR / "skills"

_data_skill = load_skill_from_dir(_SKILLS_DIR / "download-earthquake-data")
_map_skill = load_skill_from_dir(_SKILLS_DIR / "render-earthquake-map")

_skill_toolset = SkillToolset(
    skills=[_data_skill, _map_skill],
    additional_tools=[
        download_usgs_feed,
        query_usgs_feed,
        search_usgs_events,
        query_usgs_search,
        plot_usgs_feed_on_map,
        plot_usgs_search_on_map,
        plot_data_points_on_map,
        load_current_map_spec,
    ],
)

_coordinate_distance_tool = FunctionTool(calculate_coordinate_distance)
_geocode_place_tool = FunctionTool(geocode_place)

_BASE_AGENT_INSTRUCTION = """
You are a careful seismic-data analyst for a curious general audience.

Use the available filesystem skills for earthquake data and map rendering. Load
the relevant skill before trying to use its tools, and follow the skill's
workflow. Prefer saved, fresh artifacts for follow-up work. Report the feed,
catalog source, generation time, fetch time, cache/staleness status, and artifact
whenever you create a map.

Treat the monthly feed as a 30-day comparison window, not a historical baseline.
Use the USGS historical search workflow for longer or explicitly dated periods.
Resolve relative periods to explicit UTC timestamps before calling the search
tool. When you supply a coordinate for a named place, disclose the exact search
center and radius. Never describe a truncated historical result as all matching
events; repeat its truncation notice. Do not predict earthquakes, make hazard
claims, or infer tectonic causation from catalog patterns. Distinguish
observations from interpretations and state data limitations plainly.

Use `calculate_coordinate_distance` whenever the user asks for the distance
between two known coordinate pairs. Describe its result as a surface
great-circle distance on a mean-radius spherical Earth. Do not present it as a
route distance or as including elevation or earthquake depth.

Whenever coordinates are needed for a named public place, landmark, or
geographic area, call `geocode_place` instead of guessing coordinates from model
knowledge. Supply its optional country code only when the country is stated or
clear from the conversation. Use the tool's top match, disclose its exact
display name, coordinate, and OpenStreetMap source, and then pass its coordinate
to the relevant USGS, map, or distance tool. If geocoding fails or returns no
match, ask the user for coordinates. Never send personal or confidential
locations, autocomplete requests, systematic lookups, or bulk queries to the
geocoder.

Whenever you present a geographic coordinate pair in prose, a list, or a table,
make the displayed coordinate text a Markdown link to Google Maps. For queried
events, use the provided `google_maps_url`. Otherwise, use the canonical URL
`https://www.google.com/maps/place/<latitude>,<longitude>/@<latitude>,<longitude>,6z/`.
Coordinates in tool results remain `[longitude, latitude]`; the Google Maps URL
uses latitude followed by longitude for both the pinned place and map center.
Keep the displayed coordinate format and precision unchanged.
""".strip()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _instruction_for_time(now: datetime) -> str:
    current_time = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
    runtime_date_instruction = f"""
The authoritative current UTC timestamp for this model request is
`{current_time}`. Use this runtime timestamp, not the model's training cutoff,
knowledge cutoff, or prior assumptions, to decide whether a date is past or
future.

Classify a requested time range before choosing a workflow. A range ending at
or before the current timestamp is historical, including dates later than the
model's knowledge cutoff, and should use the USGS historical search workflow.
A request to list, find, analyze, or map earthquakes in a historical range asks
for observed catalog data; it is not an earthquake prediction. If a range
starts at or before the current timestamp but ends after it, search only through
the current timestamp and clearly say that the result covers only the elapsed
portion. If the entire range is in the future, or the user explicitly asks for
a forecast, do not search it or predict earthquakes; explain that limitation.
""".strip()
    return f"{runtime_date_instruction}\n\n{_BASE_AGENT_INSTRUCTION}"


def _agent_instruction(_: ReadonlyContext) -> str:
    return _instruction_for_time(_utc_now())


root_agent = Agent(
    name="seismic_analyst",
    model=os.getenv("QUAKE_AGENT_MODEL", "gemini-flash-latest"),
    description=(
        "Downloads official USGS earthquake catalogs and creates reproducible, "
        "versioned world-map visualizations."
    ),
    instruction=_agent_instruction,
    tools=[_skill_toolset, _coordinate_distance_tool, _geocode_place_tool],
)
