# 2026-07-29 数据连接授权完整包验证

## 1. 交付范围

完整包版本：

```text
20260729-resource-data-permissions-r1-no-business-db
```

本次完整包从最近的 `20260729-microservices-data-sync-resilience-r1-no-business-db` 交付包增量派生，只在 Windows 本地生成交付文件，没有停止或重建 128 当前运行容器。

包内不包含 Oracle、SQL Server、Doris 或 MySQL 恢复目标业务镜像，也不包含资源开通或数据连接授权测试 mock。

## 2. 镜像增量与启动边界

本次业务代码变更只涉及两个镜像：

- API 更新为 `oracle-recovery-service-api:20260729-resource-data-permissions-r1`。
- Resource Provisioning Worker 更新为 `oracle-recovery-service-worker-resource-provisioning:20260729-resource-data-permissions-r1`。

其余 6 个业务 Worker 沿用最近完整包中的原标签和已验证镜像 ID，没有重新构建。为了支持网络隔离环境的全新安装，完整应用镜像归档仍携带 8 个应用标签，而不是只携带两个增量标签。

`start-service.sh` 继续作为完整安装/完整升级入口，执行数据库迁移并启动 API 和 7 个独立 Worker。脚本只修改上述两个镜像默认值，其他 Worker 的镜像变量、队列和启动命令保持不变。只更新现有测试环境时使用模块发布脚本，不使用完整包启动脚本替换无关容器。

## 3. 镜像组成

| 服务 | 镜像 | 已验证镜像 ID |
|---|---|---|
| API | `oracle-recovery-service-api:20260729-resource-data-permissions-r1` | `sha256:1d5636953f57f432ba82e25bccd4b2f44e8e755fe2a93d1aee62cccc08697f48` |
| Oracle Worker | `oracle-recovery-service-worker-oracle:20260728-microservice-phase1-r1` | `sha256:d038163d8bc757a3c8dc6c187b95fc49f538710ac4462655a0cf44dde306ea42` |
| SM4 Worker | `oracle-recovery-service-worker-sm4:20260728-microservice-phase1-r1` | 同 phase1-r1 Worker |
| SM3 Worker | `oracle-recovery-service-worker-sm3:20260728-microservice-phase1-r1` | 同 phase1-r1 Worker |
| Doris SQL Worker | `oracle-recovery-service-worker-sql:20260728-microservice-phase1-r1` | 同 phase1-r1 Worker |
| Data Sync Worker | `oracle-recovery-service-worker-data-sync:20260728-data-sync-long-run-resilience-r1` | `sha256:4dbb186f1febb48189cca8b4f8c8934374a221f1b3622488e86473171fcaac9b` |
| Data Platform Worker | `oracle-recovery-service-worker-data-platform:20260728-microservice-phase1-r1` | 同 phase1-r1 Worker |
| Resource Provisioning Worker | `oracle-recovery-service-worker-resource-provisioning:20260729-resource-data-permissions-r1` | `sha256:90d33e7f61e5c9f0447b658fad9edcc0337cf8c2c4689a654db90683a6ab9099` |

镜像归档 SHA256：

```text
264039018664e2fcede87903df6b512500f746d1b74ccf8f033904e51f8ccb75
```

## 4. 配置与打包规则

- `.env.example` 与上一完整包逐行比较，只修改 `API_IMAGE`、`RESOURCE_PROVISIONING_WORKER_IMAGE` 和 `APP_IMAGE_TAR` 三个值；没有新增、删除、移动、重命名或重排字段。
- 系统 MySQL 固定为 `mysql:8.4`，兼容账号仍为 `root/recovery/recovery/oracle_recovery`。
- MySQL、Redis、迁移、API 和 7 个 Worker 保留 Docker 17.03 的 pids、nproc 和 seccomp 兼容参数。
- 本地系统 MySQL 启动及持久调优脚本保留 8 项大日志查询资源基线。
- 启动链路不依赖 Compose，不使用 `--pull`、`--mount` 或 `host-gateway`。
- Oracle 21c 生命周期脚本保留，Oracle 业务镜像不进入应用包。
- SM4 动态 UDF 继续优先使用 `javac --release 8`，并保留 `-source 8 -target 8` 回退。

## 5. Linux 验证

在 128 独立临时目录完成以下验证，未停止、重建或替换真实容器：

- 包内 Shell、env、SQL、YAML、README、Markdown 和文本文件无 UTF-8 BOM、无 CRLF。
- 所有 `*.sh` 通过 `sh -n`；指定的加载、启动、状态和停止脚本再次通过语法检查。
- `. ./.env.example` 成功，两个新镜像值、未变的数据同步 Worker、MySQL 8.4 和旧账号兼容值均正确。
- 镜像归档通过 `sha256sum -c` 和 `gzip -t`。
- `load-images.sh` 真实加载 8 个应用标签，加载后的镜像 ID 与 128 已验证镜像一致。
- 外部 MySQL/Redis 模式启动演练生成 9 条应用 `docker run`：1 次迁移、1 个 API、7 个 Worker。
- 本地 MySQL/Redis 模式启动演练生成 11 条 `docker run`，确认 MySQL 8.4、Redis、应用容器兼容参数及 8 项 MySQL 资源配置进入链路。
- 两种模式的启动命令均只把 API 和 Resource Provisioning Worker 指向本次新镜像；其余 6 个 Worker 标签保持不变。

## 6. 功能验证继承

- 数据连接授权与原资源开通专项测试 32 项重新执行通过。
- 候选镜像完整测试 `Ran 151 tests`，结果为 `OK (skipped=1)`。
- 128 真实链路验证正常授权、Token 失效刷新、HTTP 500 不刷新、同名资源冲突和失败行重试，最终 4 行全部成功。
- 原开通成功后不会自动产生授权批次；授权失败不改变原开通批次状态。
- API/MySQL 健康，7 个 Worker 全部 `pong`，隔离测试数据和 mock 均已清理。

详细业务证据见：

- `RELEASE_VALIDATION_20260728_RESOURCE_PROVISIONING.md` 第 12 节。
- `test-reports/resource-data-permissions-e2e-result.json`。

## 7. 交付文件

交付包括完整 Docker Run 包、独立完整应用镜像包、对应 SHA256 文件以及包内 `VERSION.txt`、README、PRD 和发布验证记录。完整包最终 SHA256 以同目录 sidecar 校验文件为准。
