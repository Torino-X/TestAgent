"""Regression coverage for memory-safe Project artifact ordering."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.repositories.artifact_repository import ArtifactRepository


class _ScalarResult:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def scalars(self) -> "_ScalarResult":
        return self

    def all(self) -> list[object]:
        return self._values


@pytest.mark.asyncio
async def test_list_by_project_sorts_only_narrow_ids_before_loading_full_rows() -> None:
    newest = MagicMock(id=91)
    oldest = MagicMock(id=37)
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[
        _ScalarResult([91, 37]),
        _ScalarResult([oldest, newest]),
    ])

    rows = await ArtifactRepository(session).list_by_project(7, 13, limit=20)

    assert rows == [newest, oldest]
    assert session.execute.await_count == 2
    first_statement = str(session.execute.await_args_list[0].args[0])
    second_statement = str(session.execute.await_args_list[1].args[0])
    assert "artifacts.metadata_json" not in first_statement
    assert "artifacts.storage_path" not in first_statement
    assert "ORDER BY artifacts.updated_at DESC, artifacts.id DESC" in first_statement
    assert "artifacts.id IN" in second_statement


@pytest.mark.asyncio
async def test_list_by_project_skips_full_row_query_when_project_has_no_artifacts() -> None:
    session = MagicMock()
    session.execute = AsyncMock(return_value=_ScalarResult([]))

    assert await ArtifactRepository(session).list_by_project(7, 13) == []
    assert session.execute.await_count == 1
