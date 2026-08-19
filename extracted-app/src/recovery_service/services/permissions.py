from __future__ import annotations

from copy import deepcopy
from typing import Any

PERMISSION_CATALOG = [
    {
        "id": "apiOrchestration",
        "label": "接口编排中心",
        "actions": [
            {"id": "apiOrchestration:read", "label": "查看连接器、流程和运行日志"},
            {"id": "apiOrchestration:design", "label": "维护连接器、SQL API 和流程"},
            {"id": "apiOrchestration:test", "label": "测试外部连接器"},
            {"id": "apiOrchestration:publish", "label": "发布流程"},
            {"id": "apiOrchestration:execute", "label": "执行流程和 SQL API"},
            {"id": "apiOrchestration:sqlWrite", "label": "定义和执行写 SQL API"},
        ],
    },
    {
        "id": "connections",
        "label": "数据连接",
        "actions": [
            {"id": "connections:read", "label": "查看连接"},
            {"id": "connections:manage", "label": "新增/编辑/删除连接"},
            {"id": "connections:test", "label": "测试连接"},
            {"id": "connections:catalog", "label": "浏览库表"},
        ],
    },
    {
        "id": "restore",
        "label": "数据导入 / 恢复",
        "actions": [
            {"id": "restore:read", "label": "查看恢复任务"},
            {"id": "restore:submit", "label": "提交恢复任务"},
            {"id": "restore:cancel", "label": "取消/停止/重试恢复任务"},
        ],
    },
    {
        "id": "dorisCsv",
        "label": "Doris CSV 导入",
        "actions": [
            {"id": "dorisCsv:read", "label": "预览/查看文件"},
            {"id": "dorisCsv:import", "label": "执行 CSV 导入"},
        ],
    },
    {
        "id": "batchAuthorization",
        "label": "批量授权中心",
        "actions": [
            {"id": "batchAuthorization:read", "label": "查看批量授权"},
            {"id": "batchAuthorization:manage", "label": "维护部门关系"},
            {"id": "batchAuthorization:import", "label": "执行初始化导入"},
            {"id": "batchAuthorization:execute", "label": "执行授权/下线/延期"},
        ],
    },
    {
        "id": "resourceProvisioning",
        "label": "数据空间批量开通",
        "actions": [
            {"id": "resourceProvisioning:read", "label": "查看开通批次与日志"},
            {"id": "resourceProvisioning:execute", "label": "提交与重试开通任务"},
        ],
    },
    {
        "id": "dorisEncrypt",
        "label": "Doris 数据加密",
        "actions": [
            {"id": "dorisEncrypt:read", "label": "扫描加密字段"},
            {"id": "dorisEncrypt:execute", "label": "执行 SM4 加密"},
        ],
    },
    {
        "id": "dorisSm3",
        "label": "Doris SM3 映射脱敏",
        "actions": [
            {"id": "dorisSm3:read", "label": "扫描/查看 SM3 任务"},
            {"id": "dorisSm3:execute", "label": "提交 SM3 任务"},
            {"id": "dorisSm3:cancel", "label": "取消 SM3 任务"},
            {"id": "dorisSm3:decrypt", "label": "映射反查解密"},
        ],
    },
    {
        "id": "dorisSm3Logs",
        "label": "SM3 日志审计",
        "actions": [
            {"id": "dorisSm3Logs:read", "label": "查看 SM3 任务日志"},
        ],
    },
    {
        "id": "dorisSqlEtl",
        "label": "Doris SQL/ETL",
        "actions": [
            {"id": "dorisSqlEtl:read", "label": "查看 SQL/ETL 任务"},
            {"id": "dorisSqlEtl:execute", "label": "执行 SQL/ETL"},
            {"id": "dorisSqlEtl:manage", "label": "创建/删除 SQL/ETL 任务"},
        ],
    },
    {
        "id": "dataPlatform",
        "label": "离线开发",
        "actions": [
            {"id": "dataPlatform:read", "label": "查看离线开发"},
            {"id": "dataPlatform:design", "label": "设计流程与节点"},
            {"id": "dataPlatform:publish", "label": "提交/上线/下线版本"},
            {"id": "dataPlatform:execute", "label": "手动执行任务"},
            {"id": "dataPlatform:monitor", "label": "查看运行监控"},
        ],
    },
    {
        "id": "cleanup",
        "label": "数据库清理",
        "actions": [
            {"id": "cleanup:read", "label": "浏览清理目标/生成计划"},
            {"id": "cleanup:execute", "label": "执行清理"},
        ],
    },
    {
        "id": "users",
        "label": "用户管理",
        "actions": [
            {"id": "users:read", "label": "查看用户"},
            {"id": "users:manage", "label": "创建/修改用户"},
        ],
    },
    {
        "id": "apiKeys",
        "label": "API Key 管理",
        "actions": [
            {"id": "apiKeys:read", "label": "查看 API Key"},
            {"id": "apiKeys:manage", "label": "创建/禁用 API Key"},
        ],
    },
]

ALL_MODULE_IDS = [str(item["id"]) for item in PERMISSION_CATALOG]
ALL_ACTION_IDS = [str(action["id"]) for item in PERMISSION_CATALOG for action in item["actions"]]


def all_permissions() -> dict[str, list[str]]:
    return {"modules": ALL_MODULE_IDS.copy(), "actions": ALL_ACTION_IDS.copy()}


def empty_permissions() -> dict[str, list[str]]:
    return {"modules": [], "actions": []}


def normalize_permissions(value: dict[str, Any] | None, *, admin: bool = False) -> dict[str, list[str]]:
    if admin:
        return all_permissions()
    if not value:
        return empty_permissions()
    modules = [item for item in value.get("modules", []) if item in ALL_MODULE_IDS]
    actions = [item for item in value.get("actions", []) if item in ALL_ACTION_IDS]
    return {"modules": list(dict.fromkeys(modules)), "actions": list(dict.fromkeys(actions))}


def permission_catalog_response() -> list[dict[str, Any]]:
    return deepcopy(PERMISSION_CATALOG)
