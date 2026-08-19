from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class DorisSm3DecryptItem(BaseModel):
    encrypted_value: str = Field(min_length=1)
    client_ref: str | None = None


class DorisSm3DecryptRequest(BaseModel):
    connection_id: UUID
    field_category: str = Field(min_length=1, max_length=128)
    items: list[DorisSm3DecryptItem] = Field(default_factory=list)
    encrypted_values: list[str] = Field(default_factory=list)
    field_aliases: list[str] = Field(default_factory=list)

    field_mapping_database: str | None = None
    field_mapping_table: str | None = None
    mapping_database: str | None = None
    mapping_table: str | None = None

    source_database: str | None = None
    source_table: str | None = None
    masked_database: str | None = None
    masked_table: str | None = None

    @model_validator(mode="after")
    def validate_values(self) -> "DorisSm3DecryptRequest":
        if not self.items and not self.encrypted_values:
            raise ValueError("items or encrypted_values is required")
        if self.items and self.encrypted_values:
            raise ValueError("items and encrypted_values cannot be used at the same time")
        return self


class DorisSm3MappingSource(BaseModel):
    mapping_database: str
    mapping_table: str
    original_column: str = "original_value"
    encrypted_column: str = "sm3_value"
    source_database: str | None = None
    source_table: str | None = None
    source_column: str | None = None
    masked_database: str | None = None
    masked_table: str | None = None
    masked_column: str | None = None
    updated_at: str | None = None


class DorisSm3DecryptResult(BaseModel):
    index: int
    encrypted_value: str
    original_value: str | None = None
    found: bool
    ambiguous: bool = False
    client_ref: str | None = None
    mapping_database: str | None = None
    mapping_table: str | None = None
    error: str | None = None


class DorisSm3DecryptResponse(BaseModel):
    field_category: str
    total: int
    found: int
    not_found: int
    ambiguous: int = 0
    mapping_sources: list[DorisSm3MappingSource] = Field(default_factory=list)
    results: list[DorisSm3DecryptResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
