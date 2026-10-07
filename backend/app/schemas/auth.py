"""Auth schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, model_validator


class LoginRequest(BaseModel):
    account: str = Field("", alias="username", description="Username or email")
    password: str = Field(..., description="Password")


class RegisterRequest(BaseModel):
    email: str = Field(..., description="Email address")
    password: str = Field(..., min_length=6, description="Password")
    confirm_password: str = Field(..., min_length=6, description="Confirm password")
    agree_terms: bool = Field(..., description="Must agree to terms")

    @model_validator(mode="after")
    def validate_passwords_match(self):
        if self.password != self.confirm_password:
            raise ValueError("两次输入的密码不一致")
        if not self.agree_terms:
            raise ValueError("请先同意服务条款和隐私政策")
        return self


class UpdateProfileRequest(BaseModel):
    display_name: Optional[str] = Field(None, description="Display name")
    username: Optional[str] = Field(None, min_length=3, max_length=32, description="Username")
    avatar_url: Optional[str] = Field(None, max_length=512, description="Avatar URL")


class UserResponse(BaseModel):
    """Full user response — used by register and update profile."""
    user_id: str
    username: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: str
    status: str
    avatar_url: Optional[str] = None
    updated_at: Optional[str] = None


class AuthTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 86400
    user: UserResponse


class UserProfile(BaseModel):
    """Server-side user holder — populated by ``deps.get_current_user``.

    ``internal_id`` is the integer DB primary key used by services for
    ownership checks.  It MUST NOT be serialized into any API response.
    Use ``UserResponse`` for outbound payloads, and pass
    ``current_user.internal_id`` directly between backend services.

    The field is annotated with ``exclude=True`` so any
    ``.model_dump()`` / ``.model_dump_json()`` call omits it.  FastAPI
    will still include it when serialising a UserProfile directly — to
    prevent that, route handlers must return ``UserResponse`` (which has
    no internal_id) for endpoints that expose user data.
    """

    id: str  # public_id — external-facing identifier
    internal_id: int = Field(default=0, exclude=True)
    name: str
    role: str
    username: Optional[str] = None
    email: Optional[str] = None
    status: Optional[str] = None
    avatar_url: Optional[str] = None


class LoginResponse(BaseModel):
    """Login response — ``user`` is the safe ``UserResponse`` shape (no integer DB id)."""

    token: str
    user: "UserResponse"


class LoginResponseData(BaseModel):
    token: str
    user: UserResponse
