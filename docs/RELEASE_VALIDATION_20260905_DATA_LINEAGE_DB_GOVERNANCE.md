# 2026-09-05 数据血缘数据库治理与任务工作区发布验证记录

## 1. 发布范围

- 发布模式：模式二，增量热更新到 `192.168.150.128`，不重启 Worker，不生成 Docker Run 包。
- 功能范围：数据库维度血缘统计、库业务层级手动配置、数据库筛选、任务工作区数据库治理区、OpenMetadata 投影和既有 OpenLineage 兼容接口。
- API 容器：`oracle-recovery-api`，镜像保持不变，仅热更新 `/app/src/recovery_service` 下 5 个源码文件。

## 2. 发布前保护

- 128 所有 9 个 Worker 的 Celery `active`、`reserved`、`scheduled` 检查均返回 `- empty -`。
- 发布前 API 健康检查通过，MySQL 连接成功。
- 系统库备份：`/opt/oracle-recovery/releases/20260905-lineage-db-governance-r1/oracle_recovery_before.sql`。
- API 原文件备份：同一发布目录 `backup/` 下的模型、Schema、路由、服务和 UI 文件。

## 3. 关键实现

- 数据库分组键固定为 `engine + connection_id/connection_name + catalog + database`，连接不同即使数据库同名也独立统计。
- `data_database_layers` 保存库业务层级和备注；该配置不改变 `restored/raw/standard/secured` 数据层级，不保存连接凭据。
- 血缘中心的数据库筛选、资产详情、数据库统计、任务资产表共用同一 `database_key` 和业务层级字段。
- `Base.metadata.create_all` 在 API 启动时建立 `data_database_layers`、`data_lineage_events` 表。

## 4. 真实验证结果

- API 重启后健康检查：`status=ok`，MySQL 连接成功。
- 数据库表检查：`data_database_layers`、`data_lineage_events` 均存在。
- 临时选择数据库：`doris|87718e55-3d3a-409a-943e-e14cc76e00d4||codex_auto_raw_20260821`。
- 库业务层级保存、血缘筛选和恢复原值闭环通过；筛选结果 `9` 个资产、`10` 条关系，OpenMetadata 投影 `9` 个实体。
- `/ui` 资源包含 `dataLineageDatabase`、`dataLineageDatabaseGroups`、`dataAutomationDbForm`、`数据自动化任务工作区` 4 个新 UI 标识。
- 发布后 API 容器 `RestartCount=0`；Worker、MySQL、Redis 和 Oracle 容器保持运行，API 近期日志未发现 `Traceback`、`Critical`、`Exception` 或 `Error`。
- 临时库业务层级已恢复为发布前原值，一次性远程验证脚本已清理。

## 5. 本地回归

- `python -m pytest tests/test_data_automation.py tests/test_data_platform_tree.py -q`：`15 passed`。
- UI 两段 JavaScript 语法检查通过。
- 后端 `ruff check --select I,F401,F841` 通过。
- 全量测试：`315 passed, 1 skipped, 6 failed, 32 subtests passed`；6 个失败均为既有 `test_microservice_modes.py` 与本地 FastAPI/Starlette 组合的 `'_IncludedRouter' object has no attribute 'path'` 兼容问题，与本次改动无关。
- 本机 Browser 插件仍存在信任路径故障，因此本轮只完成了远程 UI HTML 资源和前端脚本检查，未把可见窗口视觉检查标记为通过。
