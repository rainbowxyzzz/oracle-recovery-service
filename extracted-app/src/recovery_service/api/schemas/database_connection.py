from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr

DatabaseEngine = Literal["mysql", "sqlserver", "oracle", "doris", "ftp"]


class DatabaseConnectionBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    engine: DatabaseEngine
    host: str = Field(min_length=1, max_length=255)
    port: int | None = None
    username: str = Field(min_length=1, max_length=128)
    database: str | None = None
    service_name: str | None = None
    dsn: str | None = None

    ssh_host: str | None = None
    ssh_port: int = 22
    ssh_user: str | None = None
    container_name: str | None = None
    is_default: bool = False


class DatabaseConnectionCreate(DatabaseConnectionBase):
    password: SecretStr | None = None
    ssh_password: SecretStr | None = None


class DatabaseConnectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    engine: DatabaseEngine | None = None
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = None
    username: str | None = Field(default=None, min_length=1, max_length=128)
    password: SecretStr | None = None
    database: str | None = None
    service_name: str | None = None
    dsn: str | None = None

    ssh_host: str | None = None
    ssh_port: int | None = None
    ssh_user: str | None = None
    ssh_password: SecretStr | None = None
    container_name: str | None = None
    is_default: bool | None = None


class DatabaseConnectionResponse(DatabaseConnectionBase):
    id: UUID
    has_password: bool = False
    has_ssh_password: bool = False
    last_test_ok: bool | None = None
    last_test_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}
