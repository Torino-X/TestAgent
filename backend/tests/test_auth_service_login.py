from types import SimpleNamespace

import pytest

from app.core.security import hash_password
from app.services.auth_service import AuthService


class DummySession:
    def __init__(self) -> None:
        self.flushed = False

    async def flush(self) -> None:
        self.flushed = True


class FakeUserRepository:
    def __init__(self, user) -> None:
        self.user = user

    async def get_by_username(self, username: str):
        if username == self.user.username:
            return self.user
        return None

    async def get_by_email(self, email: str):
        if email == self.user.email:
            return self.user
        return None


def make_auth_service(user):
    session = DummySession()
    service = AuthService.__new__(AuthService)
    service._session = session
    service._user_repo = FakeUserRepository(user)
    return service, session


@pytest.mark.asyncio
async def test_login_accepts_email_as_account():
    user = SimpleNamespace(
        id=1,
        public_id="user_test",
        username="xiaoliux",
        email="xiaoliux@qq.com",
        password_hash=hash_password("correct-password"),
        display_name="xiaoliux",
        role="user",
        status="active",
        avatar_url=None,
        last_login_at=None,
    )
    service, session = make_auth_service(user)

    result = await service.login("xiaoliux@qq.com", "correct-password")

    assert result.user.email == "xiaoliux@qq.com"
    assert session.flushed is True
