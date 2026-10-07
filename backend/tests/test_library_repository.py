from unittest.mock import MagicMock

from sqlalchemy import select
from sqlalchemy.dialects import mysql

from app.repositories.library_repository import LibraryRepository


def test_library_metadata_union_selects_only_list_overview_columns():
    repository = LibraryRepository(MagicMock())

    listing = repository._source_union(7, scope="active", source="all")
    statement = select(listing)
    sql = str(statement.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True})).lower()

    assert "union all" in sql
    assert "metadata_json" not in sql
    assert "storage_path" not in sql
    assert "stored_name" not in sql
    assert "original_name" in sql
    assert "file_name" in sql
