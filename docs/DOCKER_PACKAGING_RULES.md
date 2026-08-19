# Docker 打包规则

## 唯一包风格基线

- Docker Run 包必须从最近一次用户确认的完整交付包增量派生，不得任意选择历史包重新组装。当前基线为 `oracle-recovery-service-docker-run-20260715-oracle-logs-sm4-coverage-no-business-db`。
- 包目录、脚本组织、README 章节顺序以及 `.env.example` 的分组和注释风格均保持基线一致。
- `.env.example` 是保序的兼容契约：
  - 未经明确需求，不得删除、重命名、移动、重新分组或按字母排序已有变量；
  - 已弃用或暂时不用但仍需兼容的变量，保留在原位置并注释原因，不直接删除；
  - 新变量追加到语义最接近的现有分组末尾，没有合适分组时才在文件末尾新增分组；
  - 不得修改无关默认值、注释、引号和空行；
  - 打包前将候选文件与基线逐行比较，存在未解释的删除或重排即停止打包。
- 用户确认新的完整包后，该包自动成为下一版唯一基线。

## 系统元数据库版本

系统元数据库 MySQL 固定使用：

```text
mysql:8.4
```

不要使用：

```text
mysql:latest
mysql:8
mysql:9.x
```

原因：MySQL 数据目录不能从 9.x 降级到 8.4。若数据目录曾被 MySQL 9.3 初始化，再用 MySQL 8.4 启动会报：

```text
Invalid MySQL server downgrade: Cannot downgrade from 90300 to 80409
```

## 系统元数据库资源配置

系统元数据库承载数据同步独立运行记录、表级结果、SQL 明细和运行日志。大批量数据同步任务，例如 100 张表以上，
在访问 `component-runs?limit=30` 时会按时间排序并读取较大的 JSON 结果。默认 MySQL 配置
`sort_buffer_size=256KB`、`tmp_table_size=16MB`、`innodb_buffer_pool_size=128MB` 对当前业务偏低，可能报：

```text
Out of sort memory, consider increasing server sort buffer size
```

后续 Docker Run 包、生产调优脚本和部署说明必须包含以下系统库最低配置：

```text
sort_buffer_size=16777216
join_buffer_size=4194304
read_buffer_size=1048576
read_rnd_buffer_size=4194304
tmp_table_size=268435456
max_heap_table_size=268435456
max_allowed_packet=268435456
innodb_buffer_pool_size=536870912
```

已有环境可执行 `artifacts/tune-system-mysql-for-data-platform-logs.sh` 通过 `SET PERSIST` 调整，不需要清理系统库数据卷。

## 打包要求

- 使用 Docker Run 部署包时，系统库镜像默认值必须是 `mysql:8.4`。
- 使用 `docker-compose.yml` 或 `run-with-docker.sh` 时，`MYSQL_SERVICE_IMAGE` 默认值必须是 `mysql:8.4`。
- 老版本 Docker 启动 MySQL 8.4 系统库容器时，需要保留兼容参数：`--privileged`、`--security-opt seccomp=unconfined`、`--pids-limit -1`、`--ulimit nproc=65535:65535`。否则可能出现 `ls: cannot access '/docker-entrypoint-initdb.d/': Operation not permitted`。
- 系统 MySQL 调优配置必须作为打包检查项：包内启动/说明/运维脚本需覆盖 `sort_buffer_size`、`tmp_table_size`、`max_heap_table_size`、`max_allowed_packet`、`innodb_buffer_pool_size` 等最低值，避免数据同步运行日志查询因排序内存不足报 500。
- 不要把业务库镜像打进应用服务包，包括 Oracle、SQL Server、Doris、MySQL 恢复目标库。
- MySQL 恢复目标库和系统元数据库必须分离，不能把恢复任务导入系统元数据库。

## 清理重建说明

清理系统库数据卷只会删除本系统元数据，例如：

- 登录用户、权限、API Key
- 任务记录、执行日志
- 数据源配置、恢复任务配置
- SM3/SM4 任务、调度、审计记录
- 批量授权中心的部门、用户映射、部门库映射、授权批次、授权明细

不会删除业务库里的真实业务数据，例如 Oracle、SQL Server、Doris、MySQL 恢复目标库中的表数据。

清理后现有启动脚本可以重建空系统库：MySQL 容器启动后，应用会执行 `python scripts/init_db.py`，自动创建系统表并创建默认管理员。

- ??? Docker ?????????API ???Worker ??? Redis ??????? `--pids-limit -1`?`--ulimit nproc=65535:65535`?`--security-opt seccomp=unconfined`??? `init_db.py` ? Python ????? `RuntimeError: can't start new thread`?
# 打包前必读入口

每次打包前必须先阅读并核对：

```text
docs/PACKAGING_MUST_READ.md
```

目标服务器明确为：

```text
docker-ce 17.03
```

因此所有 Docker Run 部署包必须按老 Docker 兼容方式生成，不要依赖 `docker-compose` / `docker compose`，不要使用新 Docker 参数。应用迁移容器、API 容器、Worker 容器和 Redis 容器也必须保留老 Docker 兼容参数，不能只给 MySQL 加。

## Oracle Auto Import Python Version Note

- The Oracle Docker host test baseline must include Python `3.7.9`, because the user's external failing server reports `/usr/bin/python3 -> 3.7.9`.
- The 128 host has been aligned for reproduction: `/usr/local/bin/python3` points to `/usr/local/python3.7.9/bin/python3.7`.
- Keep `/usr/local/python3.8.18` and `/usr/local/bin/python3.8` available only as an explicit fallback; do not make Python `3.8+` the default requirement again.
- Oracle auto import preflight must accept Python `3.7+`. Do not raise the requirement back to Python `3.8+` unless the remote import script is intentionally changed to use 3.8-only syntax and a full Oracle import test is repeated.
- Before claiming Oracle auto import is fixed, verify the preflight detail shows `python_bin=/usr/local/bin/python3` and `version=3.7.9`, then complete an actual DMP import and row-count check.

## Large Oracle DMP Timeout Note

- For large Oracle DMP restore scenarios, especially around `1TB`, never use `120s` as the auto-probe or import engine timeout.
- Docker Run packages should default to:
  - `ORACLE_IMPORT_OPERATION_TIMEOUT_SECONDS=604800` as the single long timeout for Oracle DMP discovery, SQLFILE probes, trial imports, `imp`/`impdp`, auto-import engine execution, and Celery task limits.
  - `DEFAULT_IMPDP_TIMEOUT_SECONDS=604800` kept only for backward compatibility with older deployments.
  - `ORACLE_METADATA_PROBE_TIMEOUT_SECONDS=7200` kept only for backward compatibility; new Oracle import processing code should use `ORACLE_IMPORT_OPERATION_TIMEOUT_SECONDS`.
  - `ORACLE_SSH_CHECK_TIMEOUT_SECONDS=600` for lightweight SSH/container visibility checks.
- Verification must include both a command lasting longer than 120 seconds and a real Oracle DMP import with row-count validation.
- Oracle large imports with heavy index creation must ensure TEMP tablespace is expanded before execution. The Oracle 21c initialization script must keep configurable recovery tempfile creation enabled by default.
