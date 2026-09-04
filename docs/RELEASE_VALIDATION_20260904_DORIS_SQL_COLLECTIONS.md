# 2026-09-04 Doris SQL 集合发布验证记录

## 发布边界

- 模式：模式二，发布到 128 并验证。
- 目标：为 Doris SQL 开发工作台增加 SQL 集合能力，支持把多个已保存 SQL 任务归并为有序集合，测试开发版，发布不可变生产版，并允许其他模块引用生产版本。
- 本次追加修复：SQL 工作台新增按钮和 SQL 集合弹窗在 1440/960/720 宽度下的显示问题。
- 数据结构：复用 `data_platform_workflows`、`data_platform_workflow_versions`、`data_platform_nodes`、`data_platform_workflow_runs`、`data_platform_node_runs`，未新增表，未执行数据库迁移。
- 影响服务：`oracle-recovery-api`、`oracle-recovery-worker-data-platform`。
- 未触碰服务：Oracle、Doris、MySQL 系统库、Redis 数据卷。

## 128 发布

- 服务器：`192.168.150.128`。
- 热更新包：`/root/codex-release/.release-tmp-doris-sql-collections-20260904-r1.tgz`。
- 发布脚本：`/root/codex-release/deploy-doris-sql-collections-20260904.sh`。
- 备份目录：
  - 首次 SQL 集合热更新：`/root/codex-backups/doris-sql-collections-20260904-r1-20260904-184707`。
  - 补齐统一样式静态文件：`/root/codex-backups/doris-sql-collections-20260904-r1-20260904-191104`。
- 固化镜像：
  - `oracle-recovery-service-api:20260904-doris-sql-collections-r1`，image id `89dd67bde71c`。
  - `oracle-recovery-service-worker-data-platform:20260904-doris-sql-collections-r1`，image id `feafad4a4cf2`。

## 本地验证

- Python 语法：`py_compile` 通过。
- 前端脚本语法：从 `ui.html` 抽取内联 `<script>` 后用 Node `vm.Script` 检查通过。
- 定向测试：`tests/test_doris_sql_collections.py`，3 passed。
- 较宽回归：`python -m pytest tests --ignore=tests/test_microservice_modes.py`，308 passed，1 skipped。
- 说明：`tests/test_microservice_modes.py` 为此前已确认的无关旧失败，本次未修改该路径。

## 128 验证

- 发布前检查：payload 内容为 4 个预期文件；`data_platform_workflow_runs` 无 `pending/running` 在途任务。
- 容器内语法：API 容器内 `py_compile` 通过。
- 静态资源：`ui-unified.css`、`assistant-unified.css`、`assistant.html` 已落地到 API 容器。
- 路由注册：`/api/v1/doris-sql-etl/collections` 已出现在 OpenAPI。
- 健康检查：`/api/v1/health` 返回 `status=ok`，MySQL 连接成功。
- 服务状态：`oracle-recovery-api`、`oracle-recovery-worker-data-platform`、`oracle-recovery-mysql`、`oracle-recovery-redis` 均运行。
- 日志检查：发布后 10 分钟 API/worker 错误筛选无输出。

## 真实接口闭环

隔离前缀：`CODEX_SQL_COLLECTION_20260904`。

- 创建 3 个 Doris SQL 任务。
- 创建集合并指定顺序 `[task2, task1, task3]`。
- 开发版运行成功，节点返回值 `[2, 1, 3]`。
- 发布生产版后修改源 SQL 任务，旧生产版运行仍返回 `2`，证明生产快照不可变。
- 重新保存并发布后，新生产版运行返回 `200`，证明重新发布后变更生效。
- 归档集合后，成员 SQL 任务仍保留。
- 验证完成后清理隔离测试数据，`workflows_after=0`，`nodes_after=0`。

## 页面回归

隔离前缀：`CODEX_SQL_COLLECTION_UI_20260904`。

- 使用 128 页面完成登录、进入 Doris SQL 开发工作台、打开 SQL 集合弹窗、新建集合、保存、关闭后重新打开、回填校验、修改说明、再次回填、归档。
- 覆盖视口：1440x900、960x900、720x900。
- 断言：SQL 集合弹窗不超出视口，保存按钮可见，页面无横向溢出。
- 结果：无 JavaScript `pageerror`，无 HTTP 5xx。
- 验证完成后清理 UI 隔离测试数据，`ui_workflows_after=0`。

## 残留风险

- 128 根分区使用率约 93%，可用约 8.7G；本次未执行完整打包或大镜像重建。
- 浏览器控制插件在当前 Codex 会话中仍受 `helper_unknown_error` 影响，因此截图文件未能通过工具二次人工查看；页面自动化已完成可量化布局断言。
