from __future__ import annotations

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

from google.adk.agents.llm_agent import LlmAgent
from google.adk.tools import FunctionTool
from google.adk.tools.load_memory_tool import LoadMemoryTool
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
import pytest

import quake_agent.agent as agent_module
from quake_agent.agent import _coordinate_distance_tool
from quake_agent.agent import _geocode_place_tool
from quake_agent.agent import _instruction_for_time
from quake_agent.agent import _load_memory_tool
from quake_agent.agent import _preload_memory_tool
from quake_agent.agent import _save_turn_to_memory
from quake_agent.agent import _skill_toolset
from quake_agent.agent import root_agent


_TEST_NOW = datetime(2026, 9, 1, 17, 45, 30, tzinfo=UTC)


def _resolved_instruction() -> str:
    return _instruction_for_time(_TEST_NOW)


def test_agent_identifies_as_seismascope() -> None:
    assert root_agent.name == "Seismascope"
    assert root_agent.model == ""
    assert root_agent.canonical_model.model == LlmAgent.DEFAULT_MODEL
    assert "You are Seismascope," in _resolved_instruction()


def test_agent_loads_both_filesystem_skills_and_public_tool_schemas() -> None:
    assert set(_skill_toolset._skills) == {
        "download-earthquake-data",
        "render-earthquake-map",
    }
    expected = {
        "download-earthquake-data": {
            "download_usgs_feed",
            "query_usgs_feed",
            "search_usgs_events",
            "query_usgs_search",
        },
        "render-earthquake-map": {
            "plot_usgs_feed_on_map",
            "plot_usgs_search_on_map",
            "plot_data_points_on_map",
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
    assert isinstance(_coordinate_distance_tool, FunctionTool)
    assert _coordinate_distance_tool.name == "calculate_coordinate_distance"
    assert _coordinate_distance_tool._get_declaration() is not None
    assert isinstance(_geocode_place_tool, FunctionTool)
    assert _geocode_place_tool.name == "geocode_place"
    assert _geocode_place_tool._get_declaration() is not None


def test_agent_requires_google_maps_links_for_displayed_coordinates() -> None:
    instruction = _resolved_instruction()
    assert (
        "make the displayed coordinate text a Markdown link" in instruction
    )
    assert "google_maps_url" in instruction
    assert "@<latitude>,<longitude>,6z/" in instruction


def test_agent_routes_known_coordinate_distances_to_deterministic_tool() -> None:
    instruction = _resolved_instruction()
    assert "Use `calculate_coordinate_distance`" in instruction
    assert "great-circle distance" in instruction
    assert _coordinate_distance_tool in root_agent.tools


def test_agent_resolves_named_places_with_geocoder_instead_of_guessing() -> None:
    instruction = _resolved_instruction()
    assert "call `geocode_place` instead of guessing coordinates" in instruction
    assert "ask the user for coordinates" in instruction
    assert "personal or confidential" in instruction
    assert _geocode_place_tool in root_agent.tools


def test_agent_routes_historical_queries_and_discloses_truncation() -> None:
    instruction = _resolved_instruction()
    assert "historical search workflow" in instruction
    assert "Resolve relative periods to explicit UTC timestamps" in instruction
    assert "disclose the exact search" in instruction
    assert "center and radius" in instruction
    assert "Never describe a truncated historical result as all" in instruction


def test_agent_classifies_dates_using_the_runtime_clock() -> None:
    instruction = _resolved_instruction()

    assert "`2026-09-01T17:45:30Z`" in instruction
    assert "not the model's training cutoff" in instruction
    assert "ending at\nor before the current timestamp is historical" in instruction
    assert "it is not an earthquake prediction" in instruction
    assert "search only through\nthe current timestamp" in instruction
    assert "entire range is in the future" in instruction


def test_agent_instruction_refreshes_time_without_recreating_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_time = datetime(2026, 9, 1, 17, 45, 30, tzinfo=UTC)
    second_time = datetime(2027, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert callable(root_agent.instruction)

    monkeypatch.setattr(agent_module, "_utc_now", lambda: first_time)
    first_instruction = root_agent.instruction(SimpleNamespace())
    monkeypatch.setattr(agent_module, "_utc_now", lambda: second_time)
    second_instruction = root_agent.instruction(SimpleNamespace())

    assert "`2026-09-01T17:45:30Z`" in first_instruction
    assert "`2027-01-02T03:04:05Z`" in second_instruction
    assert first_instruction != second_instruction


async def test_activated_skills_expose_only_their_dynamic_tools() -> None:
    data_context = SimpleNamespace(
        agent_name=root_agent.name,
        invocation_id="invocation-data",
        state={
            f"_adk_activated_skill_{root_agent.name}": ["download-earthquake-data"]
        },
    )
    map_context = SimpleNamespace(
        agent_name=root_agent.name,
        invocation_id="invocation-map",
        state={f"_adk_activated_skill_{root_agent.name}": ["render-earthquake-map"]},
    )

    data_tools = await _skill_toolset._resolve_additional_tools_from_state(data_context)
    map_tools = await _skill_toolset._resolve_additional_tools_from_state(map_context)

    assert {tool.name for tool in data_tools} == {
        "download_usgs_feed",
        "query_usgs_feed",
        "search_usgs_events",
        "query_usgs_search",
    }
    assert {tool.name for tool in map_tools} == {
        "plot_usgs_feed_on_map",
        "plot_usgs_search_on_map",
        "plot_data_points_on_map",
        "load_current_map_spec",
    }


def test_agent_requires_deliberate_catalog_color_mode():
    instruction = _resolved_instruction()
    assert "explicitly choose and pass `style`, including `mode`" in instruction
    assert "Choose bands or a continuous gradient" in instruction
    assert "Neither is a universal default" in instruction
    assert "reuse the saved color style" in instruction


def test_agent_wires_memory_tools_and_callback() -> None:
    instruction = _resolved_instruction()
    assert isinstance(_preload_memory_tool, PreloadMemoryTool)
    assert isinstance(_load_memory_tool, LoadMemoryTool)
    assert _load_memory_tool._get_declaration() is not None
    assert _preload_memory_tool in root_agent.tools
    assert _load_memory_tool in root_agent.tools
    assert root_agent.after_agent_callback is _save_turn_to_memory
    assert "`<PAST_CONVERSATIONS>`" in instruction
    assert "`load_memory`" in instruction
    assert "Never treat recalled past observations as current earthquake" in instruction


async def test_save_turn_to_memory_ingests_only_current_invocation_events() -> None:
    prior = SimpleNamespace(invocation_id="inv-1", id="e1")
    current_user = SimpleNamespace(invocation_id="inv-2", id="e2")
    current_model = SimpleNamespace(invocation_id="inv-2", id="e3")
    recorded: list[list[object]] = []

    async def add_events_to_memory(*, events):
        recorded.append(list(events))

    context = SimpleNamespace(
        invocation_id="inv-2",
        session=SimpleNamespace(events=[prior, current_user, current_model]),
        add_events_to_memory=add_events_to_memory,
    )

    await _save_turn_to_memory(context)

    assert recorded == [[current_user, current_model]]


async def test_save_turn_to_memory_ignores_missing_or_failing_memory_service() -> None:
    event = SimpleNamespace(invocation_id="inv-1", id="e1")

    async def missing_memory(*, events):
        raise ValueError("memory service is not available")

    async def failing_memory(*, events):
        raise RuntimeError("transient backend failure")

    for handler in (missing_memory, failing_memory):
        context = SimpleNamespace(
            invocation_id="inv-1",
            session=SimpleNamespace(events=[event]),
            add_events_to_memory=handler,
        )
        await _save_turn_to_memory(context)
