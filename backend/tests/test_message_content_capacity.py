"""Storage contract for real long normal-chat messages."""

from sqlalchemy.dialects import mysql

from app.models.message import Message


def test_message_content_uses_mediumtext_on_mysql() -> None:
    """A normal chat turn must not fail before Context Engine preflight.

    The live waterline scenario legitimately sends a roughly 50k-character
    user turn.  MySQL ``TEXT`` is byte-limited to 64 KiB, which is too small
    for multilingual input; ``MEDIUMTEXT`` leaves an explicit, production
    sized margin while retaining generic ``Text`` on other dialects.
    """
    dialect_type = Message.__table__.c.content.type.dialect_impl(mysql.dialect())

    assert isinstance(dialect_type, mysql.MEDIUMTEXT)
