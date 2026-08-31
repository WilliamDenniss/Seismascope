from __future__ import annotations

from types import SimpleNamespace

from google.adk.tools import FunctionTool
import pytest

from quake_agent.agent import _skill_toolset
from quake_agent.agent import root_agent


def test_agent_loads_both_filesystem_skills_and_public_tool_schemas() -> None:
    assert root_agent.name == "seismic_analyst"
    assert set(_skill_toolset._skills) == {
        "download-earthquake-data",
        "render-earthquake-map",
    }
    expected = {
        "download-earthquake-data": {
            "download_usgs_feed",
            "query_usgs_feed",
        },
        "render-earthquake-map": {
            "render_events_on_world_map",
            "load_current_map_spec",
        },
    }
    for name, tools in expected.items():
        assert set(
            _skill_toolset._skills[name].frontmatter.metadata["adk_additional_tools"]
        ) == tools
    for tool in _skill_toolset._provided_tools_by_name.values():
        assert isinstance(tool, FunctionTool)
        assert tool._get_declaration() is not None


def test_agent_requires_google_maps_links_for_displayed_coordinates() -> None:
    assert isinstance(root_agent.instruction, str)
    assert (
        "make the displayed coordinate text a Markdown link" in root_agent.instruction
    )
    assert "google_maps_url" in root_agent.instruction
    assert "@<latitude>,<longitude>,6z/" in root_agent.instruction


async def test_activated_skills_expose_only_their_dynamic_tools() -> None:
    data_context = SimpleNamespace(
        agent_name="seismic_analyst",
        invocation_id="invocation-data",
        state={
            "_adk_activated_skill_seismic_analyst": ["download-earthquake-data"]
        },
    )
    map_context = SimpleNamespace(
        agent_name="seismic_analyst",
        invocation_id="invocation-map",
        state={"_adk_activated_skill_seismic_analyst": ["render-earthquake-map"]},
    )

    data_tools = await _skill_toolset._resolve_additional_tools_from_state(data_context)
    map_tools = await _skill_toolset._resolve_additional_tools_from_state(map_context)

    assert {tool.name for tool in data_tools} == {
        "download_usgs_feed",
        "query_usgs_feed",
    }
    assert {tool.name for tool in map_tools} == {
        "render_events_on_world_map",
        "load_current_map_spec",
    }
