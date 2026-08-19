# 打包必读：23.47.200.105 老 Docker 环境

本文件是每次生成部署包前必须阅读和核对的清单。目标服务器环境不是普通新版本 Docker，而是用户明确确认的老版本：

```text
docker-ce 17.03
```

## 1. 总原则

- 用户说“更新代码/修改功能”时，优先热更新发布到 `192.168.150.128` 验证。
- 只有用户明确说“打包”时，才生成完整部署包。
- 给出包以后，必须同时告诉用户：怎么上传、怎么解压、怎么改 `.env`、怎么启动、怎么查看状态、怎么访问页面。
- 每次打包必须写清楚版本号、包含内容、不包含内容、关键修复点。

## 2. 目标 Docker 版本约束

目标服务器是：

```text
docker-ce 17.03
```

因此部署包必须按老 Docker 处理：

- 不使用 `docker-compose` 或 `docker compose` 作为必需启动方式。
- 启动脚本必须只依赖 `docker run`、`docker load`、`docker network`、`docker exec`、`docker logs` 等老版本可用命令。
- 不使用新 Docker 才支持的参数，例如 `--pull`、`--mount`、`--add-host=host.docker.internal:host-gateway`。
- 不依赖 Compose 的 `depends_on.condition`、profiles、healthcheck 编排语义。
- 脚本要尽量使用 POSIX `sh` 语法，不要写 Bash 专属语法。

## 3. 系统库 MySQL 固定规则

系统元数据库 MySQL 必须固定为：

```text
mysql:8.4
```

不要使用：

```text
mysql:latest
mysql:8
mysql:9.x
```

原因：如果 MySQL 数据目录曾被 9.x 初始化，再用 8.4 启动会出现类似：

```text
Invalid MySQL server downgrade: Cannot downgrade from 90300 to 80409
```

如果需要清理重建，只会影响本系统元数据表，不会删除 Oracle、SQL Server、Doris 或 MySQL 恢复目标库里的真实业务数据。

## 3.1 系统库 MySQL 资源配置基线

数据同步独立运行日志会在系统元数据库中保存表级结果、SQL 明细和运行日志。100 张表以上的任务在查询
`data_platform_component_runs` 时会触发按时间排序和 JSON 结果读取，默认 MySQL 小缓冲容易报：

```text
Out of sort memory, consider increasing server sort buffer size
```

后续 Docker Run 包和生产环境系统库必须检查并保留以下最低配置：

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

这些值用于系统元数据库，不是 Oracle、Doris、SQL Server 或 MySQL 业务恢复目标库。已有生产环境可使用
`artifacts/tune-system-mysql-for-data-platform-logs.sh` 执行 `SET PERSIST` 调优，不需要清理数据卷。
打包时必须确认启动脚本、README 或随包运维脚本包含这组检查/配置，避免大批量数据同步日志接口再次 500。

## 4. 老 Docker 兼容参数

这些参数不能只加给 MySQL。老 Docker 环境中，应用迁移容器、API 容器、Worker 容器、Redis 容器也必须加兼容参数。

应用类容器、Redis 容器必须包含：

```sh
--pids-limit -1
--ulimit nproc=65535:65535
--security-opt seccomp=unconfined
```

MySQL 8.4 系统库容器必须包含：

```sh
--privileged
--security-opt seccomp=unconfined
--pids-limit -1
--ulimit nproc=65535:65535
```

否则可能遇到：

```text
RuntimeError: can't start new thread
ls: cannot access '/docker-entrypoint-initdb.d/': Operation not permitted
```

特别注意：`RuntimeError: can't start new thread` 往往发生在 `python scripts/init_db.py` 的迁移容器里，不是 MySQL 容器里。因此 `oracle-recovery-migrate` 也必须加兼容参数。

## 5. 业务库镜像打包规则

默认应用部署包只包含：

- `oracle-recovery-service-api`
- `oracle-recovery-service-worker`
- 启停脚本
- 配置模板
- 必要文档

不要把业务库镜像打进应用包，包括：

- Oracle 业务/恢复目标库镜像
- SQL Server 业务/恢复目标库镜像
- Doris 镜像
- MySQL 恢复目标库镜像

系统元数据库 MySQL 和 Redis 属于基础设施，可由脚本按 `.env` 启动，也可配置为外部服务。

## 6. 外部 MySQL 与本地 MySQL

不要擅自给用户创建一个新的 MySQL。脚本必须支持两种模式：

```sh
START_LOCAL_MYSQL=true
```

表示在本机启动系统 MySQL 8.4 容器。

```sh
START_LOCAL_MYSQL=false
```

表示使用用户已有 MySQL，需要读取：

```sh
MYSQL_HOST
MYSQL_PORT
MYSQL_USER
MYSQL_PASSWORD
MYSQL_DATABASE
```

如果用户说已有 MySQL，不要改回本地自动创建。

## 7. PowerShell 与 Linux Shell 易错点

当前工作机常用 PowerShell，而目标服务器是 Linux shell。不要混用语法。

易错写法：

```powershell
python - <<'PY'
```

这是 Linux heredoc，不能直接在 PowerShell 中执行。

PowerShell 中推荐：

```powershell
@'
print("ok")
'@ | python -
```

通过 PowerShell 发送远程 Linux 命令时，避免在双引号中写：

```powershell
$c:/app/path
```

PowerShell 会把 `$c:` 当成变量作用域解析。推荐做法：

- 简单命令才直接 `ssh root@host "command"`。
- 复杂命令先写成 `.sh` 文件，再 `scp` 到服务器执行。
- 服务器脚本使用 `"$c:/app/path"` 这种写法时，应放在服务器端脚本文件里，不要让 PowerShell 先解析。

## 8. 打包前检查清单

### 8.0 打包与发布验证模式

每次执行前必须明确采用以下一种模式，不得静默升级：

1. **快速增量包**：用户说“最小打包”“快速打包”“增量包”或“只打修改镜像”时采用。只包含受影响镜像和必要脚本；完成本地检查、SHA256、Linux 最终包解压/语法/校验和部署前预检。可以加载镜像，但不得替换容器、执行迁移、重启、回滚或再次更新。
2. **发布验证包**：用户明确要求发布到 128 并验证时采用。在快速增量包基础上只执行一次实际更新，检查备份、迁移、健康、队列、日志和必要业务冒烟；默认不回滚、不再次更新。
3. **完整发布验收**：用户明确要求完整验证或回滚演练时采用。执行更新、回滚、健康恢复、再次更新和完整业务回归，开始前说明额外耗时和环境操作。

模式选择规则：

- 用户只说“打包”时仍生成完整 Docker Run 包，但不自动发布 128，只执行打包必需的静态检查和 Linux 包级验证。
- 用户说“最小/快速/增量打包”时必须使用模式一，不得自行扩展为实际部署或回滚演练。
- 用户要求“打包并发布测试”时使用模式二；只有明确提出回滚或完整验收时使用模式三。
- 镜像只导出和传输一次，复用已验证镜像 ID、发布清单和标准脚本模板；安全检查失败时停止并报告，不得通过升级模式继续执行。
- 如果 128 上已经存在最新热更新且完成真实验证的版本，则打包基线必须以 128 当前运行状态为准，再同步回本地生成完整包；不得回退到本地旧源码、旧镜像或旧包重新组装覆盖已验证能力。

### 8.1 包结构与 `.env.example` 风格基线

后续打包不得在多个历史包之间任选模板。必须从最近一次用户确认的完整交付包增量演进。当前唯一基线是：

```text
oracle-recovery-service-docker-run-20260715-oracle-logs-sm4-coverage-no-business-db
```

保持以下内容与基线一致：

- 包目录层级和文件命名；
- `start-service.sh`、`load-images.sh`、状态与停止脚本的整体组织方式；
- README 的章节顺序和表达风格；
- `.env.example` 的分组、字段顺序、注释、空行和默认值表达方式。

`.env.example` 采用“保序增量修改”规则：

1. 不得根据代码中的环境变量重新生成整个文件，也不得按字母排序。
2. 已有字段不得无故删除、移动、换组、改名或改变无关默认值。
3. 暂时不使用但仍有历史兼容价值的字段，留在原位置并注释，说明已弃用或仅用于兼容；不要直接删除。
4. 新字段放到语义最接近的现有分组末尾；确实没有对应分组时，才在文件末尾追加新分组。
5. 版本升级只允许修改镜像标签、镜像包文件名，以及本次需求明确涉及的配置。
6. 每次打包必须对基线和新 `.env.example` 做逐行 diff。发现无关删除或重排时停止打包。
7. 必须在 `VERSION.txt` 或 README 中列出新增、弃用、默认值变化的环境变量；没有变化也应写明“环境变量结构沿用基线”。

用户确认一个新完整包后，该包成为下一版唯一基线，旧包不再作为派生模板。

每次打包前必须检查：

- `start-service.sh` 是否只使用 `docker run`，不依赖 compose。
- `start-service.sh` 中 `oracle-recovery-migrate` 是否带老 Docker 兼容参数。
- `start-service.sh` 中 `oracle-recovery-api` 是否带老 Docker 兼容参数。
- `start-service.sh` 中 `oracle-recovery-worker` 是否带老 Docker 兼容参数。
- `start-service.sh` 中 Redis 容器是否带老 Docker 兼容参数。
- MySQL 系统库镜像默认值是否为 `mysql:8.4`。
- 系统 MySQL 是否包含或明确提示执行数据同步运行日志调优配置：`sort_buffer_size=16777216`、`tmp_table_size=268435456`、`max_heap_table_size=268435456`、`max_allowed_packet=268435456`、`innodb_buffer_pool_size=536870912` 等。
- 是否没有打入业务库镜像。
- `.env.example` 是否说明 `START_LOCAL_MYSQL=true/false` 两种模式。
- `doris_sm4_function.py` 是否强制把 SM4 Java UDF 编译为 Java 8 兼容 class：优先 `javac --release 8`，不支持时退回 `-source 8 -target 8`。Doris Java 8 运行时只能识别 class file version 52.0，不能打出 Java 17 的 61.0。
- 包名是否带清晰版本后缀，避免用户拿错旧包。
- 是否生成 `.sha256` 校验文件。
- 是否给用户使用步骤，而不只是给包路径。

## 9. 当前已知问题与对应处理

| 问题 | 原因 | 处理 |
|---|---|---|
| `RuntimeError: can't start new thread` | 老 Docker pids/nproc/seccomp 限制，常出现在迁移容器 | migrate/API/worker/redis 均加 `--pids-limit -1 --ulimit nproc=65535:65535 --security-opt seccomp=unconfined` |
| MySQL 8.4 启动权限异常 | 老 Docker seccomp/权限限制 | MySQL 容器加 `--privileged --security-opt seccomp=unconfined --pids-limit -1 --ulimit nproc=65535:65535` |
| MySQL 9.x 数据目录无法用 8.4 启动 | 数据目录版本降级不允许 | 使用原 9.x 迁移数据，或清理系统库数据卷后用 8.4 重建 |
| 数据同步运行日志 `component-runs` 查询报 `Out of sort memory` 或接口 500 | 系统 MySQL 默认排序缓冲、临时表和 InnoDB 缓冲过小，100 张表以上运行日志 JSON 查询排序时触发 | 执行 `artifacts/tune-system-mysql-for-data-platform-logs.sh`，或在系统库中持久化 `sort_buffer_size=16MB`、`tmp_table_size/max_heap_table_size/max_allowed_packet=256MB`、`innodb_buffer_pool_size=512MB` |
| 用户已有 MySQL 但脚本又创建 MySQL | 未区分本地系统库和外部系统库 | 使用 `START_LOCAL_MYSQL=false`，并填写外部 MySQL 参数 |
| `Access denied for user 'recovery'@'172.x.x.x'` | Docker Run 包默认密码被改成 `ChangeMe_*`，但已有系统库数据卷仍是旧版默认 `root/recovery` | 保持旧版兼容默认值：`MYSQL_ROOT_PASSWORD=root`、`MYSQL_USER=recovery`、`MYSQL_PASSWORD=recovery`。应用迁移/API/Worker 仍用 `recovery` 连接，不要改成 root。若用户已生成 `.env`，需要同步把 `.env` 改回旧默认或改成真实系统库密码 |
| SM4 函数创建报 `class file version 61.0 ... only recognizes ... 52.0` | SM4 UDF jar 被 Java 17 编译，但 Doris 运行时是 Java 8 | `doris_sm4_function.py` 必须优先使用 `javac --release 8`，或退回 `-source 8 -target 8`；同时升 jar 版本号并清理 `/tmp/oracle-recovery-sm4-jars/*.jar`，避免继续使用旧 jar |
| PowerShell 命令在本机报变量/重定向错误 | 把 Linux shell 语法直接粘到 PowerShell | 复杂命令写成 `.sh` 上传执行，或用 PowerShell here-string 管道 |
## 10. Oracle 19c/21c ORACLE_HOME 自动探测打包项

2026-07-17 已新增 Oracle 容器内 SQL*Plus 环境自动探测能力，用于处理：

```text
SP2-0667: Message file sp1<lang>.msb not found
SP2-0750: You may need to set ORACLE_HOME to your Oracle software directory
```

根因通常不是数据库或 PDB 不可用，而是任务配置仍带有 19c 默认路径
`/opt/oracle/product/19c/dbhome_1`，并被错误注入 21c 容器。21c 常见真实路径为
`/opt/oracle/product/21c/dbhome_1`。禁止按数据库版本猜测后强制覆盖容器环境。

下次打包必须确认：

- `OracleAutoImportRunner` 和 `oracle_dmp_auto_import.py` 都保留动态探测逻辑。
- 配置的 Oracle Home 只有在 `bin/sqlplus`、`bin/impdp` 和 `sqlplus/mesg/sp1*.msb` 同时存在时才可使用。
- 配置路径无效时，依次检查容器现有 `ORACLE_HOME`、19c/21c 常见路径，并通过 `command -v sqlplus` 反推真实目录。
- 探测成功后只在 Oracle 容器命令内导出 `ORACLE_HOME`、`PATH`、`LD_LIBRARY_PATH`；不得把容器路径注入运行远程 Python 工具的 Docker 宿主机进程。
- Oracle 工具预检必须真实执行 `sqlplus -V`，不能只执行 `command -v sqlplus`。
- 预检必须识别 `SP2-0667`、`SP2-0750` 和 `Error 6 initializing SQL*Plus` 为失败。
- 发布验证必须至少覆盖一个真实 19c 或 21c 容器。验证 21c 时，应故意传入不存在的 Oracle Home，并确认最终解析到真实 21c 路径、`sqlplus -V` 成功且 PDB 查询成功。
- 手工进入容器后执行 `export ORACLE_HOME=...` 只对当前 shell 生效，不能替代应用代码中的每次命令自动探测。

## 11. Oracle 21c 容器生命周期启动规则

Docker Run 包虽然不包含 Oracle 业务镜像，但必须包含 Oracle 21c 的容器检查、创建、启动和初始化脚本，并由 `start-service.sh` 调用。

- `ORACLE21C_MODE=auto`：发现已有容器、已有镜像或 `ORACLE21C_IMAGE_TAR` 时管理 21c；三者都不存在时明确记录并跳过。
- `ORACLE21C_MODE=container`：强制使用本地 21c。没有容器时必须检查精确镜像；镜像不存在时先尝试加载用户指定的 `ORACLE21C_IMAGE_TAR`，仍不存在则停止并给出明确错误。
- `ORACLE21C_MODE=external`：不管理本地 21c 容器。
- 已有且运行中的容器不得重建；应校验持久目录挂载后执行幂等初始化。
- 已有但停止的容器应自动启动、接入应用网络、等待 PDB `READ WRITE`，再执行初始化。
- 没有容器但镜像存在时，应使用老 Docker 兼容参数创建容器，挂载 oradata、DMP 和 tablespace 目录，然后等待并初始化。
- 初始化必须幂等创建 `RECOVERY_DMP_DIR` 和 `RECOVERY_TABLESPACE_DIR`，向 SYSTEM 授权，并验证 SYSTEM/PDB 连接及真实 Oracle 21c `sqlplus -V`。
- 不得把 Oracle 21c 镜像打进应用包。离线服务器应由用户预加载镜像，或单独提供并配置 `ORACLE21C_IMAGE_TAR`。
- 每次修改后必须在真实 21c 上验证：运行中容器、停止容器、从已有镜像创建容器、缺镜像强制失败、自动模式无资源跳过。
- 真实创建测试不得删除 Oracle 数据目录。应保留原容器作为可恢复备份，测试结束后恢复原容器并确认健康状态。

# Packaging Text Format Hard Rule

- Docker Run packages must store `*.sh`, `.env.example`, `*.sql`, `*.yaml`, and README files as UTF-8 without BOM and LF-only line endings.
- Never use Windows PowerShell `Set-Content -Encoding UTF8` for package scripts or `.env.example`, because Windows PowerShell 5 writes a BOM that breaks Linux shebang and `.env` sourcing.
- Docker Run package scripts must not rely on executable bits surviving Windows packaging. When one packaged script invokes another packaged script, call it through `sh ./script-name.sh` or explicitly verify executable permission after extraction. `start-service.sh` must not fail with `./load-images.sh: Permission denied` when the user starts it via `sh start-service.sh`.
- Before delivering any package, verify on a Linux host:
  - no file starts with bytes `EF BB BF`;
  - no packaged shell/env file contains CRLF;
  - `sh -n start-service.sh load-images.sh status-service.sh stop-service.sh` passes;
  - `. ./.env.example` can be sourced without `APP_ENV=production: command not found`.

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

## SM4 UDF Jar Persistence Note

- SM4 UDF jar 不能只放在容器 `/tmp/oracle-recovery-sm4-jars` 作为长期存储。新版包更新或容器重建后，`/tmp` 内 jar 会丢失，但 Doris 已创建的函数仍会指向旧 jar URL，导致后续任务提示找不到 jar。
- 应用默认 `DORIS_SM4_UDF_JAR_DIR` 必须使用 `/app/data/sm4-jars`。
- `.env.example` 必须显式包含 `DORIS_SM4_UDF_JAR_DIR=/app/data/sm4-jars`，避免外部部署包因环境变量缺失退回临时目录。
- Docker Run 包的 `start-service.sh` 必须给 `oracle-recovery-api` 和 `oracle-recovery-worker` 挂载同一个持久卷：
  `-v oracle_recovery_sm4_jars:/app/data/sm4-jars`
- 用户使用新版包更新后，不应因为 jar 丢失而被迫重新创建密钥函数。只有主动更换密钥或刷新函数时，才需要重新创建/刷新 SM4 函数。
## Oracle Auto Import Preflight Timeout Note

- Oracle auto import preflight must not use fixed `30s` SSH command timeouts. SSH, Docker, and container visibility checks must use `ORACLE_SSH_CHECK_TIMEOUT_SECONDS`.
- `.env.example` must include:
  - `ORACLE_SSH_CHECK_TIMEOUT_SECONDS=600`
  - `ORACLE_AUTO_IMPORT_PREFLIGHT_RETRIES=2`
  - `ORACLE_AUTO_IMPORT_PREFLIGHT_RETRY_DELAY_SECONDS=5`
  - `ORACLE_AUTO_IMPORT_SKIP_PREFLIGHT_CODES=`
- Production Docker daemon may be slow during large DMP import, heavy IO, or container pressure. Preflight checks must retry before failing.
- Skipping preflight is allowed only for non-essential checks, for example `docker_container`, `oracle_tools`, `host_dmp_files`, `container_dmp_path`, `pdb`, and `directory_policy`.
- Do not skip `ssh`, `python`, or `script_compile`; these are required to run the auto-import script.
