import uuid

from pydantic import BaseModel, Field


class KnowledgeSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    root_path: str = Field(default=".", max_length=512)
    enabled: bool = True


class KnowledgeSourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    root_path: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=8, ge=1, le=20)


class KnowledgeChatRequest(KnowledgeSearchRequest):
    pass


class KnowledgeSourceResponse(BaseModel):
    id: uuid.UUID
    name: str
    root_path: str
    enabled: bool
