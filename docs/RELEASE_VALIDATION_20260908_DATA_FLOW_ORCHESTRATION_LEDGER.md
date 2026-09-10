# 数据流编排与运行台账发布验证记录（2026-09-08）

## 范围与方式

- 模式：发布验证（模式二），未执行回滚演练或完整重新打包。
- 目标：`192.168.150.128`。
- 变更：发布“数据流编排与运行台账”UI、编排接口、ODS 安全优先状态机、Oracle 恢复 Schema 全表动态识别以及受保护 DWD SQL 强制 `restricted`。
- 兼容边界：复用既有恢复、数据同步、SM4、工作流和 OpenMetadata 执行器；普通手工数据同步仍保留按表选择。数据库模型与线上文件哈希一致，本次无数据库迁移。

## 本地验证

- 主测试：`342 passed, 1 skipped`。
- 安全访问、SQL 策略、数据同步与 OpenMetadata 专项：`45 passed`。
- 数据自动化与组件运行核心回归：`36 passed`。
- `ui.html`/`assistant.html` 内联脚本语法、Import lint 和目标文件 `git diff --check` 通过。

## 发布前保护

- API 健康，MySQL 连接正常。
- 数据自动化批次、组件运行、工作流运行和 SM4 批次的 `queued/running/reserved` 数量均为 `0`。
- 对比线上容器文件后仅更新 5 个有差异的文件；模型、迁移、安全访问解析和 SM4 基础文件保持不变。
- 更新前文件备份：`/root/codex-release/20260908-data-flow-orchestration-ledger/backup`。
- 候选归档 SHA-256：`940a396b6553f4c134528529ab9d6303cb70b952a3a7f42961f7709cad3fa4e0`。

## 发布结果

- 已更新并重启：`oracle-recovery-api`、`oracle-recovery-worker-data-platform`、`oracle-recovery-worker-data-sync`。
- 三个容器均为 `running=true`、`RestartCount=0`；发布后 `/api/v1/health` 返回 MySQL 连接成功。
- 线上 `/ui` 与候选 `ui.html` SHA-256 均为 `ed824c775725aabe168e8bc73c3ade7429104a1b1d6d939590c3e7f5f2ce580f`。
- 6 个 `/api/v1/data-automation/orchestration/*` 路由已加载；台账只读冒烟返回 4 条业务流、2 条已启用业务流、0 个运行中批次、0 个异常。
- 发布后 API、数据平台 Worker 和数据同步 Worker 日志未发现 `Traceback`、`CRITICAL`、`Internal Server Error`、`SyntaxError`、`ImportError` 或 `ModuleNotFoundError`；队列复查仍为空。
- 已固化运行镜像：API `sha256:61dc4198882f...`、数据平台 Worker `sha256:38f15d13b5cf...`、数据同步 Worker `sha256:0a38e73b3d1d...`。
- 回退标签：`oracle-recovery-service-api:pre-data-flow-ledger-20260908`、`oracle-recovery-service-worker-data-platform:pre-data-flow-ledger-20260908`、`oracle-recovery-service-worker-data-sync:pre-data-flow-ledger-20260908`。

## 可见页面与业务数据边界

- 可见浏览器已打开 `http://192.168.150.128:8000/ui?release=20260908-flow-ledger`，但页面状态抓取连续超时，因此本次不把浏览器点击、响应式和视觉截图验收标记为通过。
- 本次未创建隔离 Oracle/Doris 数据，也未执行真实恢复、加密或工作流写入；既有安全访问层的真实 Oracle Data Pump、Doris SM4、安全视图与 OpenMetadata 闭环验证仍记录在 `docs/RELEASE_VALIDATION_20260908_SECURITY_ACCESS_LAYER.md`。
