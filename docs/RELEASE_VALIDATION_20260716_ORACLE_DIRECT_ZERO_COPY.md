# Release Validation: 20260716 Oracle Direct Zero Copy

## 1. 发布范围

- Release tag：`20260716-oracle-direct-zero-copy`
- 完整包：`oracle-recovery-service-docker-run-20260716-oracle-direct-zero-copy-no-business-db`
- 本版将 Oracle“直接使用 Oracle DMP 目录”改为真实零复制读取。
- 完整包不包含 Oracle、SQL Server、Doris 或 MySQL 恢复目标业务镜像。
- 环境变量结构沿用 `20260715-oracle-logs-sm4-coverage-no-business-db` 基线，本版没有新增、删除或重排环境变量。

## 2. 实现逻辑

验证环境的实际挂载关系：

```text
/data/oracle-recovery/oracle19c/dmp
  -> /opt/oracle/recovery_dmp
```

共享 Oracle DIRECTORY：

```text
RECOVERY_DMP_DIR -> /opt/oracle/recovery_dmp
```

正式导入使用独立任务工作 DIRECTORY 写日志，并直接引用共享 DMP DIRECTORY：

```text
DIRECTORY=<任务工作DIRECTORY>
DUMPFILE=RECOVERY_DMP_DIR:codex_directdmp_%U.dmp
```

系统不再把 DMP 复制到 `auto_import/<run_id>`。任务完成后只归档和清理工作日志，不修改共享 DIRECTORY 和原始 DMP。

## 3. 自动化回归

- API 容器完整回归：47 passed，0 failed，1 skipped。
- Worker 容器完整回归：47 passed，0 failed，1 skipped。
- 两个容器跳过的均为依赖 Node.js 的画布布局行为测试；最新 UI 在本机 Node.js 18.20.8 下完成内联脚本提取并通过 `node --check`。
- API、Worker Python 源码和脚本通过 `compileall`。
- Oracle 独立工具通过 Python 3.7.9 语法编译与 dry-run。
- UI 内联 JavaScript 通过 `node --check`。
- Worker 默认入口启动并到达 Celery `ready`。

镜像归档：

```text
oracle-recovery-service-app-images-20260716-oracle-direct-zero-copy.tar.gz
SHA256=3097f7502d751404df818a8c09389511b57c8d37b9118eaa6c3798e785aa89b4
```

## 4. Oracle 真实导入验证

验证数据库：Oracle 19c，PDB `ORCLPDB1`。

独立工具验证：

- 直接读取 `codex_directdmp_%U.dmp`，共 6 个分片。
- Oracle 19c 接受 `DUMPFILE=RECOVERY_DMP_DIR:codex_directdmp_%U.dmp`。
- 完整导入成功，目标表 `DIRECT_DMP_SPLIT_TEST` 为 6000 行。
- 导入前后原始 DMP 文件大小和修改时间不变。
- 日志包含 `[zero-copy] ... docker cp was not executed`。
- 任务工作目录和任务工作 DIRECTORY 均已清理。

系统 API 正式任务：

```text
task_id=8ce0f275-85b9-483d-bf61-4eaf460759a3
state=succeeded
```

验证结果：

- `zero_copy_dump=true`
- `RECOVERY_DMP_DIR -> /opt/oracle/recovery_dmp`
- 目标表 6000 行
- 下载日志完整，日志中不存在 DMP 的 `docker cp`
- 测试 schema 和表空间已清理

系统 API 二次 dry-run：

```text
task_id=8e1ce7b2-fd5e-4dd1-8955-d49b954b0b2b
state=succeeded
```

元数据证据：

```json
{
  "copied_files": [],
  "zero_copy_dump": true,
  "oracle_directory": "RECOVERY_DMP_DIR",
  "oracle_directory_path": "/opt/oracle/recovery_dmp"
}
```

## 5. 安全与并发结论

- 共享 DIRECTORY 不存在时创建，路径一致时复用。
- 同名 DIRECTORY 已指向其他路径时任务阻断，禁止覆盖。
- `run_id` 包含任务片段与 UUID，顺序重试不会混用日志目录。
- 未发生 FULL fallback 时不复制不存在的 fallback 日志，不制造虚假失败事件。
- 原始 DMP 不是任务资源，成功、失败、取消和清理流程均不得删除。

## 6. 回滚方式

热更新回滚备份：

```text
/root/oracle-recovery-hotfix-backups/20260716-110033
```

完整包回滚时，停止当前 API/Worker，重新加载上一完整包的 API/Worker 镜像并启动。系统 MySQL 元数据、Oracle 原始 DMP、业务恢复目标库和 SM4 持久卷不需要删除。

## 7. 测试证据

本地归档：

```text
tmp/oracle-zero-copy-api-validation-8ce0f275.zip
tmp/oracle-zero-copy-api-validation-8ce0f275/
```

## 8. Docker Run 完整包验证

完整包已在 Linux Docker `1.13.1` 上完成验证，该版本早于目标 `docker-ce 17.03`。

- 镜像归档通过 SHA256 和 `gzip -t`。
- Docker save 元数据只包含 API 和 Worker，不包含任何业务数据库镜像。
- Shell、环境、SQL、YAML、Markdown 和文本文件均通过无 BOM、无 CRLF/CR 检查。
- `start-service.sh`、`load-images.sh`、`status-service.sh`、`stop-service.sh` 通过 `sh -n`。
- `.env.example` 与批准基线逐行比较，除三个镜像版本值外完全一致。
- 最终 ZIP 不包含运行时 `.env`，`ORACLE_DOCKER_SSH_PASSWORD` 在模板中保持为空。
- MySQL 默认镜像仍为 `mysql:8.4`，旧版 `recovery/recovery` 账号、Oracle 长超时和 SM4 持久卷配置均通过断言。
- 未发现 `--pull`、`--mount`、`host-gateway` 或 Compose 依赖。
- 完整包成功加载镜像、执行数据库迁移并启动 API、Worker。
- API health 返回 `status=ok`，Worker Celery 返回 `pong`。
- API 和 Worker 实际运行标签均为 `20260716-oracle-direct-zero-copy`。
- API 日志下载路由和 Worker 零复制实现检查通过。
- API、Worker 均挂载共享 `oracle_recovery_sm4_jars` 持久卷。

验证机保留了既有 `mysql:latest` 和 `redis:latest` 容器，启动脚本按兼容策略复用它们，没有替换其持久数据；交付包默认值仍为 `mysql:8.4` 和 `redis:7-alpine`。

迁移完成后出现一次 `aiomysql RuntimeError: Event loop is closed` 连接析构告警。迁移退出成功、系统表检查通过、API 和 Worker 均正常启动，因此记录为非阻断告警。

包启动后再次查询持久化任务：

- `8ce0f275-85b9-483d-bf61-4eaf460759a3`：`succeeded`，`zero_copy_dump=true`。
- `8e1ce7b2-fd5e-4dd1-8955-d49b954b0b2b`：`succeeded`，`zero_copy_dump=true`，`copied_files=[]`。

以上验证已完成，完整包满足交付条件。
