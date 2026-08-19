from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, field_validator


class ConnectorPayload(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    method: str = "POST"
    url: str = Field(min_length=4, max_length=2048)
    headers: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    body_template: Any = None
    auth_type: Literal["none", "bearer", "dynamic_bearer", "raw", "basic", "api_key"] = "none"
    auth_name: str | None = None
    auth_secret: SecretStr | None = None
    success_statuses: list[int] = Field(default_factory=lambda: [200])
    success_path: str | None = None
    success_value: Any = None
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    enabled: bool = True

    @field_validator("method")
    @classmethod
    def validate_method(cls, value: str) -> str:
        method = value.upper().strip()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("不支持的 HTTP 方法。")
        return method

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value: dict[str, Any]) -> dict[str, Any]:
        sensitive = {"authorization", "proxy-authorization", "cookie", "set-cookie"}
        if any(str(key).strip().lower() in sensitive for key in value):
            raise ValueError("认证信息必须使用认证配置，不能直接写入 Header 模板。")
        return value


class SqlApiPayload(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    slug: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    connection_id: UUID
    database: str | None = None
    sql_text: str = Field(min_length=1)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["read", "write"] = "read"
    max_rows: int = Field(default=200, ge=1, le=5000)
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    enabled: bool = True


class WorkflowPayload(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)


class InvokePayload(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)
