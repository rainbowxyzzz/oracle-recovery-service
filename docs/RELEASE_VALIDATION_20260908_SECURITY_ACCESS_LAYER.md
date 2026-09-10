# 安全访问层发布验证记录（2026-09-08）

## 范围

本次为 128 热更新验证，新增“标准敏感字段 → 源字段 → SM4 安全表 → access 视图”的冻结覆盖合同及普通用户 SQL 安全解析。明文源表不重命名、不替换。

## 已执行

- 本地：`pytest tests --ignore=tests/test_microservice_modes.py`，结果 `330 passed, 1 skipped`；新增安全访问目标回归 `17 passed`；Python 编译、F 类静态检查和 `ui.html` 脚本语法通过。
- 128：已备份 API、数据平台 Worker、SM4 Worker 的 `/app/src/recovery_service` 到 `/root/security-access-update/backup-20260907-2355`；已安装 `sqlglot 29.0.1`，完成数据库迁移。
- 128 数据库：确认 `doris_sm4_task_definitions.coverage_contracts` 和 `data_security_access_mappings` 已创建；无 `queued/running` 的自动化或 SM4 批次。
- 128 运行时：以临时 `AUDIT_SECURITY_*` 映射验证多敏感字段解析至 access 别名、密文字段筛选被拒绝、未激活映射拒绝明文回退；测试记录已清理，映射和测试连接均为 0 条。
- 128 服务：API、数据平台 Worker、SM4 Worker 重启后运行正常；`/api/v1/health` 返回 MySQL 连接成功；安全访问三个 API 路由已加载；页面包含“安全访问覆盖合同（JSON）”。OpenMetadata 服务容器健康，应用状态为 `configured/enabled/sync_ready`。
- 已将三类热更新容器提交回其运行标签，并保留 `pre-security-access-20260908` 镜像标签作为恢复点。

## Doris 物理闭环补充验证（2026-09-08）

- 按用户指令启动 128 `/home/apache-doris-4.1.1-bin-x64` 单机 Doris；FE `8030/9030`、BE `8040/9050/9060` 均已就绪，`SHOW BACKENDS` 返回 `Alive=true`。
- 创建隔离库 `AUDIT_SECURITY_20260908`、原始表 `CASE_RAW`、标准表 `CASE_STANDARD` 和专用 Doris 连接；仅插入两行审计样例。
- 基于独立的冻结覆盖合同和生产字段血缘，为 `PHONE`、`ID_CARD` 创建 SM4 批次。实际 SM4 Worker 成功生成 `CASE_RAW_sm4`，源表与安全表均为 2 行。
- 数据自动化状态机自动完成安全映射激活，创建 `AUDIT_SECURITY_ACCESS_20260908.CASE_RAW` 视图；两条映射均为 `active`。
- 受限 SQL 对原表引用自动改写为 access 视图：返回密文，且对 `PHONE` 的 `WHERE` 条件被拒绝。一次性 Doris 普通用户只获 access 视图读取授权，读取明文源库被拒绝；测试用户已删除。
- OpenMetadata 实际同步成功：3 个资产、7 条血缘关系、3 条 SM4 传播关系，失败数为 0。

## Oracle Data Pump 至安全访问全闭环（2026-09-08）

- 在 128 Oracle `ORCLPDB1` 创建仅用于验证的源 schema `AUDIT_RESTORE_SRC_20260908`，生成“案件登记”和“案件证据”两张合成审计表，各 4 行；使用真实 `expdp` 导出 `audit_restore_20260908.dmp`，文件落在平台恢复目录 `/data/oracle-recovery/oracle19c/dmp/`。
- 使用真实 `impdp` 和 `REMAP_SCHEMA` 导入独立目标 schema `AUDIT_RESTORE_TARGET_20260908`；两张恢复表均验证为 4 行。
- 经真实 Oracle→Doris 数据同步 Worker（本地 Oracle 读取、Stream Load）写入隔离库 `AUDIT_RESTORE_20260908`：`AUDIT_CASE_REGISTER_RAW`、`AUDIT_CASE_EVIDENCE_RAW`，共 8 行且每表 4 行。
- 标准化 SQL 工作流实际运行，生成 `AUDIT_CASE_REGISTER_STANDARD`（4 行）；身份证号、联系电话按字段规则分级为 SM4，标准层到原始层的全部字段级直接血缘已冻结。
- 首次加密提交揭示一个状态机竞态：SM4 `reserved` 是 Worker 正常接管态，却被自动推进器错误标为失败。已修复为等待 `queued/reserved/running` 三种中间态，并新增回归用例；本地专项测试 `10 passed`，128 API 已最小热更新并健康检查通过。
- 为新隔离 Doris 库创建并验证专用 SM4 函数后，恢复原批次继续执行：SM4 Worker 成功生成 `AUDIT_CASE_REGISTER_RAW_SM4`，原始、标准和安全表均为 4 行；两个安全映射均为 `active`，并创建 `AUDIT_RESTORE_ACCESS_20260908.AUDIT_CASE_REGISTER`。
- 受限 SQL 对 `AUDIT_CASE_REGISTER_RAW` 自动改写为 access 视图；普通临时 Doris 用户仅获得该视图权限，读取结果为密文，直接访问原表被拒绝。临时用户已删除；对密文字段的 `WHERE` 条件同样被拒绝。
- OpenMetadata 实际同步成功：4 个资产、19 条血缘关系（18 条字段级、3 条 SM4 传播关系），失败数为 0。
