# 2026-07-31 接口编排中心完整 Docker Run 包验证

## 1. 背景与结论

生产当前完整包为 `20260729-resource-data-permissions-r1-no-business-db`，运行 API 为 `oracle-recovery-service-api:20260729-resource-data-permissions-r1`，尚未包含 `oracle-recovery-worker-api-orchestration`。此前生成的 API 增量包错误地以 128 热更新后的 `20260730-api-orchestration-workbenches-r7` 为唯一基线，因此生产预检在检查接口编排 Worker时返回 `No such object`。

本次不再跨不一致基线提供增量更新，改为生成完整 Docker Run 包：

```text
oracle-recovery-service-docker-run-20260731-api-orchestration-complete-r1-no-business-db
```

本次只打包和验证，不替换 128 运行容器。

## 2. 镜像范围

| 服务 | 镜像 | 镜像 ID |
|---|---|---|
| API | `oracle-recovery-service-api:20260730-api-orchestration-dbeaver-sqlapi-canvas-r1` | `sha256:502b4856373480f3b8be264840216ff53f9f146c2ab693e6add9d8a83e80583c` |
| API Orchestration Worker | `oracle-recovery-service-worker-api-orchestration:20260730-api-orchestration-mapping-r1` | `sha256:4bd78ff4b8b0331d008aa9425eb4486bd2b03a43bf8570555c64b24f523af3c0` |
| Resource Provisioning Worker | `oracle-recovery-service-worker-resource-provisioning:20260729-resource-role-delete-r1` | `sha256:dfffd36002ae2c2bccc1ea441f5730c3f1c9dca25131488bc40f3a898d825632` |
| Data Sync Worker | `oracle-recovery-service-worker-data-sync:20260728-data-sync-long-run-resilience-r1` | `sha256:4dbb186f1febb48189cca8b4f8c8934374a221f1b3622488e86473171fcaac9b` |
| Data Platform Worker | `oracle-recovery-service-worker-data-platform:20260728-microservice-phase1-r1` | `sha256:d038163d8bc757a3c8dc6c187b95fc49f538710ac4462655a0cf44dde306ea42` |
| SQL Worker | `oracle-recovery-service-worker-sql:20260728-microservice-phase1-r1` | `sha256:d038163d8bc757a3c8dc6c187b95fc49f538710ac4462655a0cf44dde306ea42` |
| SM3 Worker | `oracle-recovery-service-worker-sm3:20260728-microservice-phase1-r1` | `sha256:d038163d8bc757a3c8dc6c187b95fc49f538710ac4462655a0cf44dde306ea42` |
| SM4 Worker | `oracle-recovery-service-worker-sm4:20260728-microservice-phase1-r1` | `sha256:d038163d8bc757a3c8dc6c187b95fc49f538710ac4462655a0cf44dde306ea42` |
| Oracle Worker | `oracle-recovery-service-worker-oracle:20260728-microservice-phase1-r1` | `sha256:d038163d8bc757a3c8dc6c187b95fc49f538710ac4462655a0cf44dde306ea42` |

完整应用镜像归档 SHA256：

```text
e40d19eb40dde22c59fa63894df8bca4e9996a00bb60f343bc19ee14af41b3b7
```

业务数据库镜像不进入部署包。系统元数据库默认值仍固定为 `mysql:8.4`，Redis 默认值仍为 `redis:7-alpine`，两者支持本地容器或外部服务模式。

## 3. 部署脚本变化

- `start-service.sh` 增加 `oracle-recovery-worker-api-orchestration`，运行模式为 `api-orchestration`，默认并发度为 `2`。
- `.env.example` 在相关分组追加 `API_ORCHESTRATION_WORKER_IMAGE`、`CELERY_API_ORCHESTRATION_QUEUE` 和 `API_ORCHESTRATION_WORKER_CONCURRENCY`；其余字段顺序和默认值保持不变。
- API、Resource Provisioning Worker和应用镜像归档版本值更新；其余业务 Worker标签保持不变。
- `stop-service.sh` 和 `status-service.sh` 纳入接口编排 Worker。
- 启动前继续由最新 API 镜像执行 `scripts/init_db.py`，用于创建缺失的接口编排系统元数据表，不操作 Oracle、Doris、SQL Server 或 MySQL 恢复目标库业务数据。
- 保留 Docker 17.03 所需的 pids、nproc、seccomp 兼容参数和 MySQL 8.4 日志查询资源基线。

## 4. 验证结果

在 Windows 完成结构派生、镜像归档下载和 SHA256 复核；在本机 WSL Ubuntu 24.04 完成 Linux 包级验证：

- 镜像归档包含 9 个预期应用标签，无缺失或额外标签。
- 包内 Shell、env、SQL、YAML、README、Markdown 和文本文件统一为 UTF-8 无 BOM、LF。
- 所有 `*.sh` 通过 `sh -n`。
- `. ./.env.example` 成功，MySQL 8.4、旧账号兼容值、接口编排队列和 Worker并发配置正确。
- 镜像归档通过 `sha256sum -c` 和 `gzip -t`。
- `load-images.sh` 在 Linux 中完整读取镜像归档并调用一次 `docker load`。
- 外部 MySQL/Redis 模式演练生成 10 条应用 `docker run`：迁移 1 条、API 1 条、Worker 8 条。
- 本地 MySQL/Redis 模式演练生成 12 条 `docker run`，确认 MySQL 8.4、Redis、迁移、API、8 个 Worker和 8 项 MySQL 资源配置全部进入启动链路。
- 本次没有停止、替换或重启 128 容器，没有执行回滚演练或生产数据库迁移。

## 5. 继承的业务验证

- 接口编排连接器、SQL API、成功/失败流程、重新执行和逐节点日志已经在 128 完成真实闭环，详见 `RELEASE_VALIDATION_20260730_API_ORCHESTRATION.md`。
- 角色 ID 持久化和人工删除角色能力已经在 128 完成接口、Worker、Token 刷新和审计验证，详见 `RELEASE_VALIDATION_20260729_RESOURCE_ROLE_DELETE.md`。
- 数据同步长任务恢复、资源开通、Oracle、SM3、SM4、SQL 和离线开发 Worker沿用最近完整包中的已验证镜像。

## 6. 已废弃增量包

`oracle-recovery-service-incremental-update-20260731-api-orchestration-dbeaver-sqlapi-canvas-r1` 只适用于已经运行 `20260730-api-orchestration-workbenches-r7` 且已存在接口编排 Worker的环境，不适用于当前生产完整包。生产从 `20260729-resource-data-permissions-r1` 升级时必须使用本次完整包。
