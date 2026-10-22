# 2026-10-09 SM4 分类局部加密发布验证

## 发布模式与范围

- 模式：模式二（发布验证包的最小热更新路径）。
- 目标：`192.168.150.128`。
- 更新容器：`oracle-recovery-api`、`oracle-recovery-worker-sm4`、`oracle-recovery-worker-data-platform`。
- 未执行数据库迁移、完整镜像打包、回滚演练或其他容器更新。
- 发布前最近 200 个 SM4 批次中无 `queued`、`reserved`、`running`、`stopping` 状态任务。
- 原文件备份：`/root/codex-release/backups/20261009-sm4-partial`。

## 发布内容

- 保留 `CQ_SM4_ENCRYPT(STRING)` / `CQ_SM4_DECRYPT(STRING)` 整字段能力。
- 新增 `CQ_SM4_PARTIAL_ENCRYPT(STRING, STRING)` / `CQ_SM4_PARTIAL_DECRYPT(STRING, STRING)`。
- 支持证件号码、姓名、电话、地址四类固定局部规则。
- 任务表配置新增可选 `column_rules`，历史配置未提供时继续整字段加密。
- 文件解密入口支持整字段与局部规则混合解密。

## 验证结果

- 本地主测试：`382 passed, 1 skipped`；已知专项 `tests/test_microservice_modes.py` 仍为既有 `_IncludedRouter.path` 兼容问题。
- 新增 SM4 局部规则测试、Python 编译、关键静态检查和 HTML 内联脚本语法检查通过。
- 128 三个候选容器内 Python 编译通过，更新后文件 SHA256 与热更新目录一致。
- 128 API 健康检查通过，SM4 Worker 与数据平台 Worker 均进入 `ready`。
- 128 容器内验证四类规则全部可逆，证件号码、姓名、电话、地址的可见头尾符合规则；混合 SQL 中局部字段调用新函数，未映射字段继续调用历史整字段函数。
- 128 文件解密接口以一行隔离合成数据验证：四个局部字段和一个整字段同时解密，HTTP 200，`decrypted_count=5`，`failed_count=0`，结果与原文一致。
- OpenAPI 已包含任务 `column_rules` 和文件解密 `column_rules`，历史 `columns` 字段仍保留。
- 发布后相关容器持续运行；日志中的审批自动监听外部接口 400 为既有独立问题，与本次 SM4 变更无关。

## 未完成项与限制

- 128 当前没有运行中的 Doris 服务，两个既有 Doris 连接均返回连接拒绝，因此未执行 Doris 中真实 `CREATE ALIAS FUNCTION`、目标表写入及函数刷新闭环。
- 可见 Chrome 不可用，内置浏览器登录密码输入操作超时；已确认 128 返回的页面脚本包含四类规则且未带入本地企业知识库改动，但未宣称完成真实页面窗口与交互回归。
- 本次采用容器文件热更新，容器重建会恢复原镜像内容；后续正式完整交付时必须把相同源码纳入新镜像。
