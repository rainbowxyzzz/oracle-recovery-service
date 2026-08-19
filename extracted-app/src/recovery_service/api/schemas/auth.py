from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr

UserRole = Literal["admin", "operator", "viewer"]
UserStatus = Literal["active", "disabled"]


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr


class UserResponse(BaseModel):
    id: UUID
    username: str
    display_name: str | None = None
    role: UserRole
    status: UserStatus
    permissions: dict[str, list[str]] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_login_at: datetime | None = None

    model_config = {"from_attributes": True}


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: UserResponse


class CurrentAuthResponse(BaseModel):
    auth_type: Literal["user", "api-key", "development"]
    user: UserResponse | None = None
    username: str | None = None
    role: str = "operator"
    is_admin: bool = False
    permissions: dict[str, list[str]] = Field(default_factory=dict)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1)
    display_name: str | None = Field(default=None, max_length=128)
    role: UserRole = "operator"
    status: UserStatus = "active"
    permissions: dict[str, list[str]] = Field(default_factory=dict)


class UserUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=128)
    role: UserRole | None = None
    status: UserStatus | None = None
    permissions: dict[str, list[str]] | None = None


class ResetPasswordRequest(BaseModel):
    password: SecretStr = Field(min_length=1)


class PermissionCatalogItem(BaseModel):
    id: str
    label: str
    actions: list[dict[str, str]] = Field(default_factory=list)


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    permissions: dict[str, list[str]] = Field(default_factory=dict)
    status: UserStatus = "active"


class ApiKeyUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    permissions: dict[str, list[str]] | None = None
    status: UserStatus | None = None


class ApiKeyResponse(BaseModel):
    id: UUID
    name: str
    key_prefix: str
    status: UserStatus
    permissions: dict[str, list[str]] = Field(default_factory=dict)
    created_by_username: str | None = None
    last_used_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class ApiKeyCreateResponse(ApiKeyResponse):
    api_key: str
