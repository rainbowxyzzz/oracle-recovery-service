# SM4 密钥切换与批次运行隔离发布验证报告

验证日期：2026-08-20

验证环境：`192.168.150.128`
发布模式：发布验证模式二

## 发布边界

- 新增按“Doris 连接 + 数据库”的 MySQL 跨实例运行屏障。
- SM4 批次创建在屏障内解析并保存绑定密钥。
- Worker 从最终密钥预检到整批结束持续持有屏障。
- 密钥刷新在屏障内检查 `queued/reserved/running/stopping` 批次，并覆盖函数部署、验证、密钥版本及部署记录登记全过程。
- SM4 调度器增加跨实例扫描锁和条件式 `queued -> reserved` 原子抢占。
- 未修改表结构、历史任务、密钥内容、离线开发快照或调度定义。

## 发布前核对

- 128 三个关键 SM4 文件 SHA256 与本地修改前完全一致，未发现线上额外代码差异。
- 在途 SM4 批次为 0。
- 受影响容器：`oracle-recovery-api`、`oracle-recovery-worker-sm4`、`oracle-recovery-worker-data-platform`。
- 发布前文件已备份到 `/opt/oracle-recovery/releases/sm4-runtime-isolation-<timestamp>/backup`。

## 验证结果

- 本地专项测试：11 项通过。
- 本地全量测试：216 passed，1 skipped，32 subtests passed。
- 128 真实 MySQL 跨连接同库锁互斥：通过。
- 临时 queued 批次阻断密钥刷新：通过。
- 隔离记录清理：通过。
- 验证前后业务计数一致：SM4 任务 13、密钥版本 7、调度 6、离线开发版本 82。
- `/api/v1/health`：HTTP 200，MySQL 连接正常。
- 三个受影响容器均为 Up，发布后近期日志无 `error`、`exception` 或 `traceback`。

## 结论

发布验证通过。历史业务定义与密钥不变；同一数据库的密钥切换、批次创建和批次执行现在具备跨 API/Worker 实例的互斥保护。
