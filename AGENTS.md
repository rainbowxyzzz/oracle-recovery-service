# Project Rules

## Language

- 默认使用简体中文与用户交流。
- 代码、命令、文件路径、API 名称、错误信息和专有名词保持原文，必要时用中文解释。

## Project Environment

本节记录当前 Windows 本地开发环境的已确认事实和固定入口。后续执行任何开发、修复、测试、打包前，必须优先复用这里的环境信息，不要重新探索 Python、Java、Node、Docker、Oracle、Doris、Spark 等运行时。

### Environment

- OS / Shell：当前本地开发机为 Windows，Shell 为 PowerShell；可使用 PowerShell 7，也兼容 Windows PowerShell 执行只读检查脚本。
- Python：项目要求 Python `>=3.10`。当前已确认系统 Python 为 `C:\Users\zy\AppData\Local\Programs\Python\Python313\python.exe`，版本 `3.13.3`。
- 虚拟环境：项目标准虚拟环境路径为仓库根目录 `.venv\Scripts\python.exe`。如果 `.venv` 不存在，先运行 `scripts/bootstrap.ps1` 创建；不要创建第二套虚拟环境。
- Python 依赖管理：当前使用 `extracted-app\requirements.txt` 安装运行依赖，`extracted-app\pyproject.toml` 记录项目元数据、pytest、ruff、mypy 配置；当前没有确认可用的 `poetry.lock`、`uv.lock`、`conda` 环境或 Node lockfile。
- Java/JDK：本地未确认存在可用 `java` / `javac`，`JAVA_HOME` 可能为空。只有构建 Doris SM4 Java UDF jar 时才需要 Java；构建必须生成 Java 8 兼容 class file version 52.0，可通过 `DORIS_SM4_JAVAC_BIN` 指定 `javac`。
- Spark：当前项目没有声明本地 `pyspark` 依赖，未确认存在 `SPARK_HOME` 或 `spark-submit`。除非需求明确触达 Spark 本地运行，否则不要把 Spark 缺失当作本地单元测试失败原因。
- Node/npm：当前已确认系统 Node.js 路径为 `C:\Program Files\nodejs\node.exe`，版本 `v18.20.8`；npm 路径为 `C:\Program Files\nodejs\npm.cmd`，版本 `10.8.2`。项目没有 `package.json`，Node 主要用于静态 HTML 内联脚本语法检查。
- Docker：当前已确认 Docker CLI 存在，但本地 Docker Desktop daemon 可能不可达。Docker 不可达时，不要反复重试或修改业务代码；需要真实容器验证时按发布规则使用 128 或先由用户启动 Docker Desktop。
- 数据库和外部服务：系统元数据库为 MySQL `8.4`，缓存为 Redis `7`；业务链路依赖 Oracle、Doris、SQL Server/MySQL 恢复目标库等。当前本地 `127.0.0.1:3306/6379/8000/9030/8030/8040/1521` 可能不可达，真实数据库验证通常依赖 128 或用户提供的远程环境。
- Oracle Client / JDBC / OCI：Python 侧使用 `oracledb`；Oracle 还原链路会在目标主机/容器内调用 `imp`/`impdp`/`sqlplus`。本地未确认存在 `ORACLE_HOME`、`impdp`、`sqlplus`，不要为了普通单元测试安装 Oracle Client。
- Doris 连接：项目通过 Doris MySQL 协议和 HTTP/Stream Load 能力访问 Doris；Python 依赖侧主要使用 `pymysql` / `httpx`，连接信息由系统配置或数据库连接配置提供。
- 项目启动：本地开发入口位于 `extracted-app`，使用 `PYTHONPATH=src` 启动 FastAPI、Celery Worker 和脚本；Docker Run 部署仍遵守本文件打包规则。

### Required workflow

以后每次执行任务必须遵守：

1. 首先读取 `AGENTS.md` 和 `docs/PROJECT_PRD_SUMMARY.md`，再看本次涉及模块的 PRD 或发布验证记录。
2. 优先运行 `scripts/check-env.ps1`，确认当前本地环境状态。
3. 使用项目已有环境和仓库根目录 `.venv`。
4. 不随意创建新的虚拟环境。
5. 不随意切换 Python / Java / Node 版本。
6. 不重复安装已经存在的依赖。
7. 不修改宿主机全局环境来解决项目问题。
8. 不因为一个测试失败就重新搭建环境。
9. 首先判断问题属于代码问题还是环境问题。
10. 已知环境问题优先读取 `KNOWN_ISSUES.md`，确认是否已有固定处理方式。

### Commands

环境检查：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/check-env.ps1
```

严格检查（WARNING 也视为失败，适合发布前本地环境收敛）：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/check-env.ps1 -Strict
```

初始化或修复项目内 Python 环境：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/bootstrap.ps1
```

激活项目虚拟环境：

```powershell
.\.venv\Scripts\Activate.ps1
Set-Location extracted-app
$env:PYTHONPATH = "src"
```

安装依赖应优先使用 `bootstrap`。如需手工安装，只允许安装到项目 `.venv`：

```powershell
.\.venv\Scripts\python.exe -m pip install -r extracted-app\requirements.txt
.\.venv\Scripts\python.exe -m pip install "pytest>=8.3.0" "pytest-asyncio>=0.24.0" "ruff>=0.8.0" "mypy>=1.13.0"
```

主测试命令：

```powershell
Set-Location extracted-app
$env:PYTHONPATH = "src"
..\.venv\Scripts\python.exe -m pytest tests --ignore=tests/test_microservice_modes.py
```

已知兼容性失败的专项测试：

```powershell
Set-Location extracted-app
$env:PYTHONPATH = "src"
..\.venv\Scripts\python.exe -m pytest tests/test_microservice_modes.py
```

Lint：

```powershell
Set-Location extracted-app
$env:PYTHONPATH = "src"
..\.venv\Scripts\python.exe -m ruff check src tests
```

Type check：

```powershell
Set-Location extracted-app
$env:PYTHONPATH = "src"
..\.venv\Scripts\python.exe -m mypy src
```

前端静态脚本语法检查：

```powershell
node -e "const fs=require('fs'); const vm=require('vm'); for (const file of ['extracted-app/src/recovery_service/static/ui.html','extracted-app/src/recovery_service/static/assistant.html']) { const html=fs.readFileSync(file,'utf8'); const match=html.match(/<script>([\s\S]*)<\/script>/); if(match) new vm.Script(match[1]); } console.log('html script syntax ok');"
```

本地 API 启动（要求本地 MySQL / Redis 可用）：

```powershell
Set-Location extracted-app
$env:PYTHONPATH = "src"
..\.venv\Scripts\python.exe scripts/init_db.py
..\.venv\Scripts\python.exe -m uvicorn recovery_service.main:app --host 0.0.0.0 --port 8000 --reload
```

本地 Worker 启动（要求本地 MySQL / Redis 可用）：

```powershell
Set-Location extracted-app
$env:PYTHONPATH = "src"
..\.venv\Scripts\celery.exe -A recovery_service.workers.celery_app:celery_app worker -l info
```

本地服务低成本检查：

```powershell
docker ps
Test-NetConnection 127.0.0.1 -Port 3306
Test-NetConnection 127.0.0.1 -Port 6379
Test-NetConnection 127.0.0.1 -Port 8000
Test-NetConnection 127.0.0.1 -Port 9030
```

### Environment troubleshooting policy

当出现环境错误时：

1. 首先判断是代码问题还是环境问题。
2. 检查 `AGENTS.md`、`KNOWN_ISSUES.md` 和 `scripts/check-env.ps1` / `scripts/bootstrap.ps1`。
3. 不要连续尝试多个 Python、pip、Java、npm 或其他运行时。
4. 不要为了绕过一个问题创建第二套开发环境。
5. 不要自动修改宿主机全局配置。
6. 如果发现了一个未来还可能出现的问题，应将解决方案固化到 `AGENTS.md`、`KNOWN_ISSUES.md`、`scripts/check-env.ps1`、`scripts/bootstrap.ps1`、依赖文件或环境变量模板。
7. 同一个已经确认的问题以后不要重新诊断。
8. 如果某个外部服务本来就无法从当前环境访问，应明确报告并切换到 mock / 静态检查 / 128 授权验证路径，不要无限重试。

## Docker Packaging

- 系统元数据库 MySQL 必须固定使用 `mysql:8.4`，不要使用 `mysql:latest`、`mysql:8` 或 9.x 标签。
- 打包脚本、`.env.example`、`docker-compose.yml`、`run-with-docker.sh` 和 Docker Run 部署包中，系统库镜像默认值必须保持为 `mysql:8.4`。
- 老版本 Docker 启动 MySQL 8.4 系统库容器时，需要保留兼容参数：`--privileged`、`--security-opt seccomp=unconfined`、`--pids-limit -1`、`--ulimit nproc=65535:65535`。
- 老版本 Docker 启动应用迁移容器、API 容器、Worker 容器、Redis 容器时，也必须保留兼容参数：`--security-opt seccomp=unconfined`、`--pids-limit -1`、`--ulimit nproc=65535:65535`。否则 `init_db.py` 或 Python 服务可能报 `RuntimeError: can't start new thread`。
- MySQL 恢复目标库是业务恢复目标，不等同于系统元数据库；业务库镜像不应打进应用部署包。
- 如果 MySQL 数据目录曾被 9.x 初始化，不能直接用 8.4 启动同一个数据目录。要么继续用原 9.x 镜像启动并迁移数据，要么清理系统库数据卷后用 8.4 重新初始化。
- 清理系统库数据卷只影响本系统的元数据表，不会删除 Oracle、SQL Server、Doris 或 MySQL 恢复目标库中的业务数据。
- Docker Run 包必须兼容旧版系统库默认账号：`MYSQL_ROOT_PASSWORD=root`、`MYSQL_USER=recovery`、`MYSQL_PASSWORD=recovery`、`MYSQL_DATABASE=oracle_recovery`。不要擅自把默认值改成新的 `ChangeMe_*`，否则已有 `oracle_recovery_mysql_data` 数据卷会继续保留旧账号密码，迁移容器会报 `Access denied for user 'recovery'@'172.x.x.x'`。
- 应用迁移、API、Worker 必须继续使用 `MYSQL_USER` 连接系统库，不要改成 root 连接应用。root 只用于本地 MySQL 容器健康检查或用户明确执行初始化 SQL。
- 系统元数据库承载数据同步独立运行记录和大 JSON 运行日志，生产与 Docker Run 包必须保留最低 MySQL 资源配置：`sort_buffer_size=16777216`、`join_buffer_size=4194304`、`read_buffer_size=1048576`、`read_rnd_buffer_size=4194304`、`tmp_table_size=268435456`、`max_heap_table_size=268435456`、`max_allowed_packet=268435456`、`innodb_buffer_pool_size=536870912`。默认 256KB 排序缓冲和 16MB 临时表会导致 100 表级数据同步日志 `component-runs` 查询报 `Out of sort memory`/500。已有环境使用 `artifacts/tune-system-mysql-for-data-platform-logs.sh` 调优；后续打包必须把该配置作为检查项。
- SM4 Java UDF jar 必须编译为 Java 8 兼容 class file version 52.0。打包前检查 `doris_sm4_function.py`：优先使用 `javac --release 8`，不支持时退回 `-source 8 -target 8`。不能生成 Java 17 class file version 61.0，否则 Doris Java 8 运行时会报 `only recognizes class file versions up to 52.0`。

### Docker 20 新服务器独立交付基线

- Docker 17.03 仍是 128 和历史 Docker Run 包的默认兼容基线；只有用户明确指定全新服务器和 Docker 20+ 时，才允许生成独立的 Docker Compose 交付包，禁止用新版包覆盖 128 的旧版基线。
- 双服务器交付固定拆分：A 服务器继续使用 Docker 17.03 兼容的业务 Docker Run 包，B 服务器使用 Docker 20.10+ 和 Docker Compose 2.2.3+ 独立运行 OpenMetadata 1.13.0；不得再把两者包装成要求共享 Docker 网络的同机启动包。
- A 包复用 `20260909-data-automation-reliable-dispatch-r1` 业务镜像，不重复构建，并离线携带系统 MySQL 8.4、Redis 7-alpine 镜像；B 包只携带 OpenMetadata Server/DB 1.13.0、Elasticsearch 9.3.0 和 Docker Compose 2.24.7。
- OpenMetadata 使用平台 API 主动推送模式，不包含 Ingestion/Airflow；两台服务器通过 B 服务器可达的 `http://B_SERVER_IP:8585` 通信，不依赖跨主机 Docker 网络或容器名称解析。
- B 服务器首次配置必须修改 OpenMetadata 元数据库密码；A 服务器继续保留 Docker 17 历史账号兼容规则，并在获得 OpenMetadata 服务端 Token 后配置 `OPENMETADATA_URL`、`OPENMETADATA_API_TOKEN` 和 `OPENMETADATA_SYNC_ENABLED=true`。
- B 服务器部署 Docker 前必须先区分真正裸机与“旧 Docker 已安装但服务未启动”。存在 Docker 命令、服务单元或非空 `/var/lib/docker` 时不得执行 `fresh` 新装脚本，也不得通过删除 `/var/lib/docker` 强行满足新装条件。
- Docker 二进制升级脚本启动服务后必须等待 daemon（后台服务）和 Socket（本地通信套接字）就绪，并核验服务端版本；一次 `docker info` 失败不能直接认定升级失败。失败恢复必须保留旧二进制备份和服务日志位置。
- A 业务系统的 `OPENMETADATA_*` 机器间同步配置与 `OIDC_*` 人员统一认证配置互不替代。更新现有环境不得用 `.env.example` 覆盖 `.env`；修改 `.env` 后必须重建相关容器，单独 `docker restart` 不会加载新环境变量。
- 每次 A 完整打包必须比较包内 `oracle21c-ee/initialize.sh` 与 `deploy/oracle21c-ee/initialize.sh`，并运行 `tests/test_oracle21c_initialize.py`，防止 Smallfile TEMP 表空间上限修复再次从历史包回退。

## Requirement Intake and PRD Workflow

- 每次开发、修复、验证或打包前，必须先阅读并理解本项目规则文件、`docs/PROJECT_PRD_SUMMARY.md` 以及本次需求相关模块的 PRD、发布验证记录和历史约束；不能只依据对话片段或局部代码直接改动。
- 当需求新增或实质改变业务行为、API、数据结构、权限、调度、部署或兼容性契约，或者触达高风险模块、建立新的验收基线时，必须先更新对应 PRD 或项目 PRD 汇总，明确背景、目标、业务规则、兼容性边界、验收方式和风险点；PRD 更新完成并理解其含义后，才进入代码开发。
- 恢复已有文档约定行为的常规缺陷修复、测试修复、文档纠错和不改变行为的维护，默认不要求新增 PRD 条目；如果排查发现产品规则缺失、错误或存在冲突，则必须先更新 PRD。
- 新需求进入开发前，必须主动思考其合理性、与既有能力的冲突、对旧流程的影响和可替代方案，并向用户给出判断与建议。轻微歧义应先检查仓库和既有约定，采用保守解释并继续；只有不同解释会实质影响行为、兼容性、数据、安全、架构、部署或范围时，才停止并确认。
- 对数据同步、离线开发、调度、SM4、Oracle 导入、权限等高风险模块，开发前必须结合 PRD 列出本次改动触达的页面、接口、任务保存、运行、日志、打包和部署路径，并把这些路径纳入回归范围。
- PRD、源码、128 正在运行版本和最近确认包之间出现不一致时，先说明差异并确定以哪个版本为准，不得在未确认的情况下用旧包或旧逻辑覆盖已验证能力。

## Deployment Workflow

- 本地 Windows 环境是代码开发、阅读、修改、单元测试、前端静态检查和生成打包文件的工作区。
- `192.168.150.128` 是开发后的测试发布和真实验证场地，但本地代码修改不等于授权发布到 128。只有用户在当前任务中明确要求发布、部署验证或在 128 验证时，才允许对该服务器执行写入、容器替换、迁移、重启或创建测试数据。
- 用户说“打包”时，表示在 Windows 本地项目目录生成完整 Docker Run 部署包、镜像包和校验文件；打包本身不等于自动替换 128，除非用户明确要求发布该包到 128。
- 排查用户反馈的环境或页面问题时，可以只读查看 128 当前运行页面、状态、日志与容器中的实际文件，再结合本地源码判断；只读排查不扩大为远程修改授权。
- 用户要求更新或编写代码时，先在本地实施并完成适当验证，再准备最小适用的热更新路径；用户明确授权发布后，优先采用少量文件热更新并在 128 实际页面和运行链路中验证。
- 当前验证服务器 SSH 账号为 `root`；密码由受控凭据管理提供，仅用于用户授权的发布验证，不得复制到应用源码、部署包或公开交付文档。
- 只有用户明确要求“打包”时，才生成完整部署包或镜像包。
- 在用户已明确授权发布的前提下，如果改动可以通过少量文件热更新完成，优先提供并执行热更新，不重新打整包。
- 真正需要打包时，必须同步跟进版本说明，明确包含的功能版本和更新内容。

## Regression Prevention

- 已有可用行为默认属于兼容性契约。新增功能不得改变旧功能的可编辑范围、默认值、请求字段、保存结果或运行语义；确需改变时，必须先向用户说明影响并取得明确确认，同时更新 PRD。
- 修改共享高风险文件（尤其 `static/ui.html`、`services/data_platform.py`、任务调度、密钥和导入执行链路）前，必须比较当前源码、128 正在运行版本和最近确认包，列出本次改动触达的旧功能路径。
- PRD、历史行为和新需求存在冲突或歧义时必须先停止并确认，禁止自行选择会削弱旧能力的解释。
- 所有可编辑设置页面必须执行真实闭环回归：打开并回填旧值、直接修改目标字段、保存、检查请求与响应、刷新或重新进入后回读；同时覆盖启用和停用状态。只检查按钮存在、页面能打开或 API 健康不算通过。
- 新增入口或管理视图时，必须同时回归它所复用的原有新增、编辑、保存、删除、运行和调度能力。共享页面改动必须在可见 Chrome 最大化、左右半屏和还原窗口中验证主流程、控制台错误和页面溢出。
- 发布到 128 前必须使用隔离测试数据并确认无在途任务，完成备份、自动化测试和真实页面回归后才可重启；发布后检查 API/Worker 健康、错误日志并清理隔离数据。
- 每次完整打包必须携带本版本新增的回归用例，并从当前已验证源码和最近确认包派生，不得让旧包覆盖已修复的代码或测试。

## 必读打包约束补充

- 每次打包前必须先阅读并核对 `docs/PACKAGING_MUST_READ.md`。
- 目标服务器明确为 `docker-ce 17.03`，部署包必须按老 Docker 处理。
- Docker Run 包必须支持老 Docker，不要依赖 `docker-compose` / `docker compose`。
- 不要使用新 Docker 参数，例如 `--pull`、`--mount`、`--add-host=host.docker.internal:host-gateway`。
- 每次打包必须检查系统 MySQL 调优配置是否进入启动脚本、README 或随包运维脚本，并至少覆盖 `sort_buffer_size=16MB`、`tmp_table_size/max_heap_table_size/max_allowed_packet=256MB`、`innodb_buffer_pool_size=512MB`。
- 复杂远程命令不要直接塞进 PowerShell 双引号中执行；优先生成 Linux `.sh` 脚本后 `scp` 到服务器执行，避免 PowerShell 与 shell 的变量、冒号、heredoc 语法冲突。
- 老 Docker 兼容参数不只用于 MySQL，也必须用于 `oracle-recovery-migrate`、`oracle-recovery-api`、`oracle-recovery-worker`、Redis 容器，避免 `RuntimeError: can't start new thread`。
- 每次交付包以后，必须告诉用户具体使用步骤，不能只给包路径。

## Packaging and Release Verification Modes

打包与发布验证固定分为以下三种模式。每次开始前必须在状态更新中明确本次采用的模式和边界，不得在未告知用户的情况下自行升级验证级别。

### 模式一：快速增量包

- 触发词包括“最小打包”“快速打包”“增量包”“只打修改的镜像”“只更新受影响服务”等；这是上述表达的默认模式。
- 只列出受影响源码、数据库结构、接口、页面、镜像和容器，只打包受影响镜像及必要的加载、更新、状态、回滚脚本。
- 使用已经在 128 验证通过的镜像，镜像只导出和传输一次；不得无故重建、重复 `docker save/load` 或携带无关 Worker 和业务数据库镜像。
- 必须完成本地文件检查、SHA256、UTF-8 无 BOM、LF、Shell 语法，以及最终压缩包在 Linux 上的解压、包内校验和部署前预检。
- 部署前预检可以加载镜像和读取运行状态，但不得替换运行容器、执行数据库迁移、重启服务、演练回滚或再次更新。
- 除非发现交付包本身无法使用，否则不得把快速增量包自动扩展成发布验证包或完整发布验收。

### 模式二：发布验证包

- 仅在用户明确要求“发布到 128 并验证”“发布验证”“部署后验证”时采用。
- 包含快速增量包的全部检查，并在确认无在途任务、完成备份后，对 128 执行一次实际更新。
- 更新后检查数据库迁移、API/Worker 健康、目标队列、近期错误日志，以及本次受影响业务的必要冒烟闭环。
- 默认不执行回滚、不再次更新、不重复导出镜像；需要回滚演练时必须进入模式三或取得用户明确要求。

### 模式三：完整发布验收

- 仅在用户明确要求“完整发布验收”“完整验证”“回滚演练”“更新、回滚并再次更新”时采用。
- 包含模式二，并执行实际更新、回滚、健康恢复、再次更新和完整业务回归；高风险模块还需覆盖真实页面、接口、日志、调度和数据结果。
- 执行前必须说明该模式耗时明显更长，并列出将发生的容器切换、数据库备份、迁移和测试数据操作。

### 模式选择硬规则

- 用户只说“打包”时，仍按本文件既有规则生成完整 Docker Run 包，但不得自动发布到 128；验证范围仅限打包必需的静态检查和 Linux 包级验证。
- 用户说“最小打包”或“快速打包”时，必须使用模式一，不得因为追求更充分验证而擅自执行模式二或模式三。
- 用户同时要求“打包并发布测试”时使用模式二；只有明确要求回滚或完整闭环时才使用模式三。
- 安全检查发现阻塞条件时应停止并说明，不得以“顺便验证”为由升级模式。
- 脚本模板、发布清单和已验证镜像 ID 应复用；只有脚本结构或部署契约变化时才重新演练回滚。

## Packaging Text Format Hard Rule

- Docker Run packages must store `*.sh`, `.env.example`, `*.sql`, `*.yaml`, and README files as UTF-8 without BOM and LF-only line endings.
- Never use Windows PowerShell `Set-Content -Encoding UTF8` for package scripts or `.env.example`, because Windows PowerShell 5 writes a BOM that breaks Linux shebang and `.env` sourcing.
- Before delivering any package, verify on a Linux host:
  - no file starts with bytes `EF BB BF`;
  - no packaged shell/env file contains CRLF;
  - `sh -n start-service.sh load-images.sh status-service.sh stop-service.sh` passes;
  - `. ./.env.example` can be sourced without `APP_ENV=production: command not found`.

## Docker Run Package Style Baseline

- 后续 Docker Run 包必须以最近一次用户确认并交付的完整包为唯一结构基线。当前基线为：
  `oracle-recovery-service-docker-run-20260715-oracle-logs-sm4-coverage-no-business-db`。
- 不允许从更早历史包重新组装，也不允许根据 `Settings`、环境变量列表或个人习惯重新生成、重新排版 `.env.example`。
- 包目录、脚本名称、README 章节顺序、配置分组、注释风格和空行风格默认保持基线一致；只做本次版本确实需要的增量修改。
- `.env.example` 必须保持字段顺序稳定：
  - 已有配置不得无故删除、重命名、移动、重新分组或按字母排序；
  - 本版不再使用但仍需兼容的配置，保留在原位置并注释，补充“已弃用/仅兼容”的原因，不直接删除；
  - 新配置追加到语义最接近的现有分组末尾；没有合适分组时才在文件末尾新增分组；
  - 不得顺手改变无关配置的默认值、注释、引号形式或空行；
  - 旧版本兼容账号、MySQL 8.4、Oracle 超时和 SM4 持久卷等强制值仍服从本文件其他规则。
- 每次打包前必须将新 `.env.example` 与基线包逐行比较。除版本标签和本次需求明确涉及的字段外，删除、移动、重排都视为打包失败；确需变化时必须在版本说明中逐项解释。
- 新版本交付后，经用户确认的最新完整包自动成为下一次打包基线；不得继续从更老包派生。

## Frontend UI/UX Rules

- 适用范围：extracted-app/src/recovery_service/static/ui.html 主工作台、assistant.html 独立助手页及其本地静态样式。前端重构只改表现层，不修改后端业务逻辑、API URL、请求字段、返回结构、数据库结构、权限语义、任务调度或既有业务流程。
- 产品定位：现代、专业、克制的数据分析 / 数据中台后台；优先表达任务状态、影响范围、运行结果和可操作性，不使用营销式大面积装饰、夸张渐变或无必要动画。
- 页面骨架优先统一为 PageHeader → Search/Form → ActionBar → ContentCard → Table/Chart/Log → Pagination。toolbar 用于标题或区块标题，actions 用于操作集合并必须允许换行。
- 全站优先复用现有技术栈和本地资源，不进行无必要的 Vue/React 或框架迁移，不引入大量第三方依赖，不删除既有入口、字段、按钮、数据区域或业务能力。
- Design Tokens 必须统一使用并优先扩展现有兼容变量：页面背景 --background-page / --bg、卡片 --background-card / --panel、次级背景 --background-subtle、主文字 --text-primary / --text、次级文字 --text-secondary / --muted、边框 --border-color / --line、主色 --primary、浅主色 --primary-soft、成功 --success / --ok、警告 --warning / --warn、危险 --danger / --bad。字号优先 12 / 13 / 14 / 18 / 20px，间距优先 4 / 8 / 12 / 16 / 24 / 32px，默认控件高度 36px，圆角优先 6 / 8 / 12px 和 999px 状态胶囊。
- 统一按钮、输入框、Select、Textarea、Label、Toolbar、Tab、Card、List、Table、状态标签、Message、Empty、Loading、Error、Dialog、Drawer 的尺寸、颜色、边框、Hover、Focus-visible、Active、Disabled 和 Loading 状态。主操作使用 .primary，不可逆操作使用 .danger，取消 / 返回 / 刷新使用默认次操作样式。
- 表格文本默认左对齐，数字和时间遵循业务列规则；表格需要横向滚动时只允许在 .table-wrap 或对应工作台内部滚动，不制造页面级横向溢出。空态、加载态和错误态必须清晰可见，不能只显示空白。
- Modal / Drawer 必须限制在视口内，内容较多时使用内部滚动；关闭、确认和取消操作必须可达。不得通过新增 !important、超大 z-index 或主布局 position:absolute 解决冲突。
- 层级统一按 header → navigation → popover → modal → high-priority modal 管理，优先使用 30 / 40 / 50 / 80 / 100 对应 token。absolute 仅允许用于 Badge、Tooltip、Overlay、图标装饰和画布节点 / 连线 / 端口等坐标绘制；页面主体、查询区、按钮排列、Card 排列和 Form 布局必须使用 Grid / Flex。
- 图表必须有明确容器高度，和 SearchPanel / Toolbar 分离；初始化、容器变化、Tab / Drawer / Dialog 展开后正确 resize，销毁时释放实例，不得重复初始化。当前扫描未发现 ECharts 实例；后续新增图表必须遵守此规则。
- 响应式至少检查 1366×768、1440×900、1600×900、1920×1080，以及浏览器缩放 100%、125%、150%。窄屏可以让导航、表格、工作台或画布使用内部滚动，但不得出现页面级横向溢出、按钮遮挡、表单重叠或弹窗超出视口。
- 共享页面是高风险文件。修改前必须检查现有页面入口、组件、CSS、JS、弹窗、工作台、画布和测试；优先新增或调整统一样式层，避免重排业务 DOM 和修改业务 JS。完成后必须检查 git diff --stat、git diff、脚本语法、相关自动化测试、静态资源加载和响应式布局；不得把未执行的浏览器级验收写成通过。
- 详细页面清单、扫描问题、Token 表和图表 / 画布约束维护在 docs/frontend-guidelines.md；该文档与本节冲突时，以本节项目级规则为准。
