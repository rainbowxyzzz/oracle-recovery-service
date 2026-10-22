import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx

from recovery_service.api.v1.knowledge import router
from recovery_service.core.models.knowledge import KnowledgeDocument, KnowledgeSource
from recovery_service.services import knowledge_base
from recovery_service.services.permissions import ALL_ACTION_IDS, ALL_MODULE_IDS


def _settings(root: Path):
    return SimpleNamespace(
        knowledge_enabled=True,
        knowledge_vault_root=root,
        knowledge_max_files=20,
        knowledge_max_file_bytes=1024 * 1024,
        knowledge_chunk_chars=500,
        knowledge_chunk_overlap_chars=50,
        knowledge_qdrant_collection="test_knowledge",
        knowledge_score_threshold=0.25,
    )


def test_resolve_source_path_blocks_escape(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "team").mkdir(parents=True)
    monkeypatch.setattr(knowledge_base, "get_settings", lambda: _settings(vault))

    assert knowledge_base.resolve_source_path("team") == (vault / "team").resolve()
    try:
        knowledge_base.resolve_source_path("../outside")
    except knowledge_base.KnowledgeServiceError as exc:
        assert "超出" in str(exc)
    else:
        raise AssertionError("path escape must be rejected")


def test_parse_and_chunk_published_markdown():
    metadata, body = knowledge_base.parse_markdown(
        """---
title: 审批流授权
status: published
module: approval
tags: [审批, Doris]
---
# 使用方式
先保存配置，再启动监听。

## 注意事项
任务状态应当在列表中可见。
""",
        "fallback",
    )
    chunks = knowledge_base.chunk_markdown(body, metadata["title"], 500, 50)
    assert metadata == {
        "title": "审批流授权",
        "status": "published",
        "module": "approval",
        "tags": ["审批", "Doris"],
    }
    assert [item["heading"] for item in chunks] == ["使用方式", "注意事项"]
    assert "启动监听" in chunks[0]["text"]


class _Scalars:
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values


class _FakeDb:
    def __init__(self, documents):
        self.documents = documents
        self.added = []

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        return None

    async def refresh(self, value):
        return None

    async def scalars(self, _statement):
        return _Scalars(self.documents)


def test_incremental_index_updates_new_document_and_deletes_stale(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "guide.md").write_text(
        "---\ntitle: 操作手册\nstatus: published\n---\n# 开始\n执行环境检查。\n",
        encoding="utf-8",
    )
    settings = _settings(vault)
    monkeypatch.setattr(knowledge_base, "get_settings", lambda: settings)
    monkeypatch.setattr(knowledge_base, "_embed", lambda texts: asyncio.sleep(0, result=[[0.1, 0.2] for _ in texts]))
    monkeypatch.setattr(knowledge_base, "_ensure_collection", lambda size: asyncio.sleep(0))
    upserted = []
    deleted = []

    async def upsert(points):
        upserted.extend(points)

    async def delete(points):
        deleted.extend(points)

    monkeypatch.setattr(knowledge_base, "_upsert_points", upsert)
    monkeypatch.setattr(knowledge_base, "_delete_points", delete)
    source = KnowledgeSource(id=uuid.uuid4(), name="测试知识", root_path=".", enabled=True)
    stale = KnowledgeDocument(
        id=uuid.uuid4(),
        source_id=source.id,
        relative_path="removed.md",
        path_hash="removed",
        title="旧文档",
        content_sha256="old",
        point_ids=["old-point"],
        chunk_count=1,
    )
    db = _FakeDb([stale])

    result = asyncio.run(knowledge_base.run_index(db, source, "tester"))

    assert result["state"] == "succeeded"
    assert result["indexed_count"] == 1
    assert result["deleted_count"] == 1
    assert upserted[0]["payload"]["relative_path"] == "guide.md"
    assert "old-point" in deleted
    assert stale.deleted_at is not None


def test_empty_search_result_does_not_call_chat_model(monkeypatch):
    async def no_hits(_query, _top_k):
        return []

    monkeypatch.setattr(knowledge_base, "search_knowledge", no_hits)
    result = asyncio.run(knowledge_base.chat_knowledge("不存在的问题", 8))
    assert result["citations"] == []
    assert "没有找到足够依据" in result["answer"]


def test_search_applies_threshold_and_chat_returns_citations(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.knowledge_chat_base_url = "http://chat.test/v1"
    settings.knowledge_chat_api_key = ""
    settings.knowledge_chat_model = "qwen-test"
    settings.knowledge_request_timeout_seconds = 10
    monkeypatch.setattr(knowledge_base, "get_settings", lambda: settings)
    monkeypatch.setattr(knowledge_base, "_embed", lambda _texts: asyncio.sleep(0, result=[[0.1, 0.2]]))
    captured = {}

    async def qdrant(_method, _path, *, json_body=None):
        captured.update(json_body or {})
        return {"result": [{"score": 0.88, "payload": {
            "source_name": "治理知识",
            "document_id": "doc-1",
            "title": "监听说明",
            "relative_path": "审批/监听.md",
            "heading": "启动方式",
            "document_updated_at": "2026-09-18",
            "text": "保存配置后启用监听。",
        }}]}

    monkeypatch.setattr(knowledge_base, "_qdrant_request", qdrant)
    hits = asyncio.run(knowledge_base.search_knowledge("如何监听", 5))
    assert captured["score_threshold"] == 0.25
    assert hits[0]["relative_path"] == "审批/监听.md"

    real_client = httpx.AsyncClient

    def handler(request):
        payload = json.loads(request.content)
        assert "[1]" in payload["messages"][1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "保存后启用监听。[1]"}}]})

    monkeypatch.setattr(
        knowledge_base.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    result = asyncio.run(knowledge_base.chat_knowledge("如何监听", 5))
    assert result["answer"].endswith("[1]")
    assert result["citations"][0]["title"] == "监听说明"


def test_knowledge_permissions_and_routes_are_registered():
    assert "knowledgeBase" in ALL_MODULE_IDS
    assert {
        "knowledgeBase:read",
        "knowledgeBase:chat",
        "knowledgeBase:manage",
        "knowledgeBase:index",
    }.issubset(set(ALL_ACTION_IDS))
    paths = {route.path for route in router.routes}
    assert {
        "/knowledge/status",
        "/knowledge/sources",
        "/knowledge/sources/{source_id}/index",
        "/knowledge/documents",
        "/knowledge/search",
        "/knowledge/chat",
    }.issubset(paths)
