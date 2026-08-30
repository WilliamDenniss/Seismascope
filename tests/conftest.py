from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from google.adk.artifacts import FileArtifactService


class ArtifactContext:
    """Small ToolContext-compatible adapter backed by ADK's real file store."""

    def __init__(
        self,
        root: Path,
        *,
        state: dict[str, Any] | None = None,
        app_name: str = "quake-agent-test",
        user_id: str = "test-user",
        session_id: str = "test-session",
    ) -> None:
        self.state = state if state is not None else {}
        self._service = FileArtifactService(root_dir=root)
        self._app_name = app_name
        self._user_id = user_id
        self._session_id = session_id

    async def save_artifact(
        self,
        filename: str,
        artifact: Any,
        custom_metadata: dict[str, Any] | None = None,
    ) -> int:
        return await self._service.save_artifact(
            app_name=self._app_name,
            user_id=self._user_id,
            session_id=self._session_id,
            filename=filename,
            artifact=artifact,
            custom_metadata=custom_metadata,
        )

    async def load_artifact(self, filename: str, version: int | None = None) -> Any:
        return await self._service.load_artifact(
            app_name=self._app_name,
            user_id=self._user_id,
            session_id=self._session_id,
            filename=filename,
            version=version,
        )

    async def list_versions(self, filename: str) -> list[int]:
        return await self._service.list_versions(
            app_name=self._app_name,
            user_id=self._user_id,
            session_id=self._session_id,
            filename=filename,
        )


@pytest.fixture
def artifact_context(tmp_path: Path) -> ArtifactContext:
    return ArtifactContext(tmp_path / "artifacts")

