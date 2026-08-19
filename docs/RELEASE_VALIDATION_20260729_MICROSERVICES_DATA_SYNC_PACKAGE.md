# 2026-07-29 微服务与数据同步可靠性完整包验证

## 1. 交付范围

完整包版本：

```text
20260729-microservices-data-sync-resilience-r1-no-business-db
```

本次只在 Windows 本地生成交付包，没有切换、停止或重建 128 当前运行容器。镜像从已完成真实业务验证的 128 导出。

包内不包含 Oracle、SQL Server、Doris 或 MySQL 恢复目标业务镜像，也不包含资源开通测试 mock。

## 2. 镜像组成

| 服务 | 镜像 | 已验证镜像 ID |
|---|---|---|
| API | `oracle-recovery-service-api:20260728-data-sync-long-run-resilience-r1` | `sha256:d2e4ee07c39603e9d32589681005dec704b6cf892de39207d1389763a25c5f45` |
| Oracle Worker | `oracle-recovery-service-worker-oracle:20260728-microservice-phase1-r1` | `sha256:d038163d8bc757a3c8dc6c187b95fc49f538710ac4462655a0cf44dde306ea42` |
| SM4 Worker | `oracle-recovery-service-worker-sm4:20260728-microservice-phase1-r1` | 同 phase1-r1 Worker |
| SM3 Worker | `oracle-recovery-service-worker-sm3:20260728-microservice-phase1-r1` | 同 phase1-r1 Worker |
| Doris SQL Worker | `oracle-recovery-service-worker-sql:20260728-microservice-phase1-r1` | 同 phase1-r1 Worker |
| Data Sync Worker | `oracle-recovery-service-worker-data-sync:20260728-data-sync-long-run-resilience-r1` | `sha256:4dbb186f1febb48189cca8b4f8c8934374a221f1b3622488e86473171fcaac9b` |
| Data Platform Worker | `oracle-recovery-service-worker-data-platform:20260728-microservice-phase1-r1` | 同 phase1-r1 Worker |
| Resource Provisioning Worker | `oracle-recovery-service-worker-resource-provisioning:20260728-resource-provisioning-r4` | `sha256:7eb6634bd5fda6038cbe717dcb47f9af629e981547daa1351be3d55c4abffc6c` |

镜像归档 SHA256：

```text
9d271180022793e1d096afd6ed9a2302d5028cb447a168b57d6bcf215aed4aa8
```

API 中 `data_sync.py` SHA256 与最终本地源码一致：

```text
8960a3099ebdd8134881cae9871bd6ad3107e9064c90385af1da9d00befe6e6d
```

API 同时通过资源开通 r4 页面契约检查：存在有数用户名/密码字段，不存在旧手工 Token 输入框。

## 3. 打包规则核对

- 从 `oracle-recovery-service-docker-run-20260715-oracle-logs-sm4-coverage-no-business-db` 唯一结构基线派生。
- `.env.example` 原有字段顺序、原有默认值和旧账号兼容值保持不变；新增字段位于语义对应分组。
- 系统 MySQL 固定为 `mysql:8.4`。
- 本地 MySQL 启动参数和持久调优脚本均包含 8 项大日志查询资源基线。
- MySQL、Redis、迁移、API 和 7 个 Worker 保留 Docker 17.03 线程兼容参数。
- 启动脚本不依赖 Compose，不使用 `--pull`、`--mount` 或 `host-gateway`。
- Oracle 21c 生命周期脚本保留，Oracle 业务镜像不进入应用包。
- SM4 动态 UDF 继续优先使用 `javac --release 8`，并保留 `-source 8 -target 8` 回退。

## 4. Linux 验证

在 128 独立临时目录完成，不替换当前服务：

- 所有 `*.sh` 通过 `sh -n`。
- `start-service.sh`、`load-images.sh`、`status-service.sh`、`stop-service.sh` 通过指定语法检查。
- `. ./.env.example` 成功，关键镜像变量和 `mysql:8.4` 值正确。
- 包内 shell、env、SQL、YAML 和 README 文件无 UTF-8 BOM、无 CRLF。
- `load-images.sh` 真实加载镜像归档，输出 8 个预期标签，加载后镜像 ID 与 128 已验证 ID 一致。
- 外部 MySQL/Redis 模式启动演练生成 9 条应用 `docker run`：1 次迁移、1 个 API、7 个独立 Worker；9 条均有线程兼容参数。
- 本地 MySQL/Redis 模式启动演练生成 11 条 `docker run`，确认 MySQL 8.4、Redis 兼容参数和 8 项 `SET PERSIST` 调优进入链路。
- 运行脚本未启动、停止或替换真实容器。

## 5. 已有功能验证继承

- 数据同步专项测试 22 项通过。
- 数据平台组件复用测试 13 项通过。
- 微服务模式测试 8 项通过。
- 最新 API 完整测试 140 项通过，1 项按环境跳过。
- 128 真实 3 表 Catalog `INSERT SELECT`、并行度 2、逐表节点与目标行数验证通过。
- 资源开通 r4 候选完整测试 133 项通过，1 项按环境跳过；自动 Token 首次登录、内存复用、失效刷新、非登录错误不刷新和历史手工 Token 兼容均通过。

详细业务证据见：

- `RELEASE_VALIDATION_20260728_DATA_SYNC_LONG_RUN_RESILIENCE.md`
- `RELEASE_VALIDATION_20260728_RESOURCE_PROVISIONING.md`
- `RELEASE_VALIDATION_20260728_MICROSERVICE_PHASE1.md`

## 6. 交付文件

交付包括完整 Docker Run 包、独立应用镜像包、对应 SHA256 文件及包内 `VERSION.txt`、README 和本验证记录。完整包最终 SHA256 以同目录 sidecar 校验文件为准。
