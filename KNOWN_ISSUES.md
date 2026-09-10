# Known Environment Issues

本文档只记录已经确认、容易重复遇到的本地环境问题。后续遇到相同症状时，先按这里处理，不要重新探索运行时或重搭环境。

## Codex Windows sandbox helper failure

### Symptom

本地普通命令或 `apply_patch` 报错：`helper_unknown_error: setup refresh had errors`。

### Cause

这是 Codex 桌面 Windows sandbox / 执行器层问题，不是项目代码、Python 依赖、Java、Node 或数据库问题。

### Correct solution

先重试一次简单只读命令确认。如果仍失败，等待执行器恢复，或在用户授权后使用受控提权命令继续当前项目内操作。

### Do not

不要因此重装 Python、Node、Docker 或修改项目业务代码。

## PowerShell is not Bash

### Symptom

在 PowerShell 中执行 Bash heredoc、复杂引号、远程 SQL 或多层 SSH 命令时出现解析错误，例如 `python - <<'PY'` 不可用。

### Cause

PowerShell 与 Bash 的 heredoc、变量展开、单双引号和换行语义不同。

### Correct solution

本地短脚本使用 `python -c` 或 `.ps1`；复杂 Linux 远程操作优先生成 `.sh` 文件后传到目标服务器执行。

### Do not

不要把 Bash heredoc 直接粘到 PowerShell；不要为了引号问题改业务代码。

## Windows quoting can truncate remote queue-check commands

### Symptom

执行远端队列检查、Docker 状态检查、SQL 检查或其他包含多层引号的 SSH 命令时，命令在 Windows / PowerShell quoting 过程中被截断，导致远端实际收到的命令不完整。

### Cause

这类命令通常同时包含 PowerShell 字符串、SSH 命令、Linux shell、JSON/SQL/filter 表达式等多层 quoting。Windows 本地解析、OpenSSH 参数传递和远端 POSIX shell 解析叠加后，容易造成引号提前闭合、反斜杠丢失、变量误展开或命令截断。

### Correct solution

一旦发现远端命令被截断，应立即停止该命令，不继续在同一行里调整引号反复重试。正确做法是把检查逻辑写成 POSIX `.sh` 脚本，上传到远端后执行；脚本内显式使用 `set -e`、固定变量、只读检查命令和清晰输出。复杂远端命令、队列检查、SQL 检查、Docker 检查都优先采用上传脚本方式。

### Do not

不要继续在 PowerShell 双引号里嵌套多层 SSH / Bash / SQL / JSON 命令反复试错；不要在命令截断风险未消除时执行可能改变远端状态的命令；不要把 quoting 截断误判为远端服务故障或业务代码问题。

## PowerShell range arguments need parentheses or variables

### Symptom

命令 `Select-Object -Index 28..42` 报错：`Cannot convert value "28..42" to type "System.Int32"`。

### Cause

在当前 PowerShell 参数绑定中，未加括号的范围表达式可能被作为字符串传给参数。

### Correct solution

使用变量或括号，例如 `$range = 28..42; Get-Content file | Select-Object -Index $range`。

### Do not

不要因此怀疑文件编码、PowerShell 版本或项目脚本本身。

## Windows PowerShell 5.1 lacks ProcessStartInfo.ArgumentList

### Symptom

脚本在 Windows PowerShell 5.1 中报错：`You cannot call a method on a null-valued expression`，位置在 `$psi.ArgumentList.Add(...)`。

### Cause

Windows PowerShell 5.1 基于 .NET Framework，`System.Diagnostics.ProcessStartInfo.ArgumentList` 不可用或表现不一致；PowerShell 7 中可用。

### Correct solution

项目环境脚本应使用 PowerShell call operator 和参数数组调用外部命令，保证 Windows PowerShell 5.1 与 PowerShell 7 都能执行。

### Do not

不要要求用户为了运行本项目环境检查脚本而升级 PowerShell；不要把该报错判断为 Python 或依赖安装失败。

## Windows PowerShell 5.1 wraps native stderr as NativeCommandError

### Symptom

使用 `2>&1` 捕获外部命令错误输出时，PowerShell 5.1 会把 stderr 包装成长格式 `NativeCommandError`，导致检查脚本输出大段调用栈。

### Cause

这是 Windows PowerShell 5.1 对 native command stderr 的错误流包装行为。

### Correct solution

项目环境检查脚本应由自身输出结构化 WARNING / ERROR，外部命令 stderr 可静默处理或转换为简短 exit code。

### Do not

不要因为 `NativeCommandError` 文本去修改 Docker、数据库或业务代码。

## Python executable and virtual environment

### Symptom

`python` 可用但依赖不完整，或不同任务中反复尝试多个 Python / pip。

### Cause

当前系统 Python 已确认是 `C:\Users\zy\AppData\Local\Programs\Python\Python313\python.exe`，但项目标准环境应使用仓库根目录 `.venv\Scripts\python.exe`。如果 `.venv` 不存在，系统 Python 只能作为 bootstrap 入口。

### Correct solution

运行：`pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/bootstrap.ps1`。之后固定使用 `.venv\Scripts\python.exe`。

### Do not

不要重复安装到系统 Python；不要创建第二个虚拟环境；不要切换到未确认的 conda / uv / poetry 环境。

## Missing Python modules in system Python

### Symptom

系统 Python 中 `openpyxl`、`pyarrow`、`pytest_asyncio`、`mypy` 等模块可能缺失。

### Cause

系统 Python 不是项目完整依赖环境。

### Correct solution

使用 `scripts/bootstrap.ps1` 修复项目 `.venv`，不要把系统 Python 的缺包当成业务代码错误。

### Do not

不要因为系统 Python 缺包而全局安装依赖或修改 requirements。

## Python dependency download is slow or PyPI is unreachable

### Symptom

`scripts/bootstrap.ps1` 在下载较大的 Windows wheel（例如 `pyarrow`）时长时间无输出或速度很慢。

### Cause

这是网络或 PyPI 下载链路问题，不是 Python 解释器选择问题，也不是业务代码问题。

### Correct solution

继续使用同一个 `.venv` 入口重复运行 bootstrap。必要时只在本次命令中指定镜像源，例如：`pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/bootstrap.ps1 -PipIndexUrl https://pypi.tuna.tsinghua.edu.cn/simple -PipTrustedHost pypi.tuna.tsinghua.edu.cn`。

### Do not

不要创建第二套虚拟环境；不要全局修改 pip 配置；不要随意降低或替换项目依赖版本。

## pytest asyncio_mode warning

### Symptom

运行测试时出现 `Unknown config option: asyncio_mode`。

### Cause

`pytest_asyncio` 未安装，pytest 不能识别 `pyproject.toml` 中的 asyncio 配置。

### Correct solution

运行 `scripts/bootstrap.ps1` 安装开发依赖。

### Do not

不要删除 `pyproject.toml` 中的 pytest 配置。

## tests/test_microservice_modes.py router compatibility failure

### Symptom

`tests/test_microservice_modes.py` 失败，典型错误：`AttributeError: '_IncludedRouter' object has no attribute 'path'`。

### Cause

这是该测试对 FastAPI / APIRouter 内部结构的兼容性问题，属于测试代码问题，不是数据库、Redis、Doris 或 Oracle 环境问题。

### Correct solution

与微服务路由测试无关的开发任务使用：`python -m pytest tests --ignore=tests/test_microservice_modes.py`。需要修复该测试时单独立项，调整测试 helper 对路由结构的读取方式。

### Do not

不要通过重装数据库、重装依赖或重启 128 来处理这个失败。

## Docker CLI exists but daemon is unreachable

### Symptom

`docker --version` 可用，但 `docker ps` 报 npipe / Docker Desktop Linux engine 不可达。

### Cause

本地 Docker Desktop daemon 未启动或当前 Windows 会话无法访问 daemon。

### Correct solution

如果只是单元测试，跳过本地 Docker；如果需要容器验证，由用户启动 Docker Desktop，或按发布规则在 128 验证。

### Do not

不要重装 Docker；不要修改 `docker-compose.yml` 或 Docker Run 包来绕过本地 daemon 未启动。

## Local database and service ports are unreachable

### Symptom

`127.0.0.1:3306/6379/8000/9030/8030/8040/1521` 连接失败。

### Cause

本地 MySQL、Redis、API、Doris、Oracle listener 通常没有运行；真实数据库链路依赖 Docker 或 128。

### Correct solution

本地优先跑 mock / 静态检查 / 不依赖数据库的单元测试。需要真实链路时按用户授权使用 128。

### Do not

不要无限重试本地端口；不要因为本地端口不通修改业务代码。

## Java/JDK missing locally

### Symptom

`java`、`javac` 或 `JAVA_HOME` 不存在。

### Cause

本地没有确认可用 JDK。普通 Python 单元测试不需要 Java。

### Correct solution

只有构建 Doris SM4 Java UDF jar 时才需要配置 JDK，并且必须保证输出 Java 8 兼容 class file version 52.0。可用 `DORIS_SM4_JAVAC_BIN` 指定 `javac`。

### Do not

不要为了无关测试安装或切换全局 JDK；不要生成 Java 17 class file version 61.0。

## Oracle client tools are not configured locally

### Symptom

`ORACLE_HOME` 为空，`impdp` 或 `sqlplus` 命令不存在。

### Cause

本地开发环境未配置 Oracle Client；项目真实还原通常在目标 Oracle 主机/容器执行 CLI。

### Correct solution

本地单元测试使用 mock 或非 Oracle CLI 路径；真实 DMP 还原在 128 或配置好的 Oracle 环境执行。

### Do not

不要为了普通单元测试安装 Oracle Client；不要把本地缺少 `impdp` 当成还原代码错误。

## Spark is not a current local runtime

### Symptom

`SPARK_HOME` 或 `spark-submit` 不存在。

### Cause

当前项目没有声明本地 `pyspark` 依赖，现有 Doris SQL/ETL 能力不要求本地 Spark。

### Correct solution

除非未来任务明确引入 Spark 本地执行，否则不要检查或安装 Spark 作为必需项。

### Do not

不要因为 Spark 不存在而阻断普通 Python 测试。

## Recursive scans under tmp may fail

### Symptom

递归扫描项目目录时在 `tmp/pytest-*` 下遇到 `Access denied`。

### Cause

历史测试数据或临时目录存在权限限制。

### Correct solution

环境检查脚本只检查固定路径，不递归扫描整个 `tmp`。

### Do not

不要递归遍历整个仓库寻找虚拟环境；不要删除 `tmp` 下不明确归属的数据。

## 128 SSH automation without sshpass

### Symptom

Windows 本地没有 `sshpass`，无法用 Linux 风格脚本自动传密码 SSH。

### Cause

当前 Windows 环境只有 OpenSSH 客户端；`sshpass` 不是 Windows 默认工具。

### Correct solution

需要 128 发布验证时，使用受控凭据、交互式 SSH/SCP、密钥或 Codex 可用的远程操作路径。复杂命令先写成 `.sh` 上传执行。

### Do not

不要把服务器密码写入源码、部署包、脚本或文档。

## Docker 20 upgrade may roll back immediately on a Type=simple service

### Symptom

升级脚本显示 Docker 已停止、已启动，随后出现 `errors pretty printing info`，并提示恢复升级前二进制；最终 `docker -v` 仍为 17.03。

### Cause

`systemd`（Linux 服务管理器）中的 `docker.service` 使用 `Type=simple`。`systemctl start` 返回时 Docker Socket（本地通信套接字）可能尚未就绪，旧脚本立即执行一次 `docker info` 后误判失败。

### Correct solution

使用 `artifacts/install-docker-20.10.24-compose-2.24.7.sh` 修正版。它等待 Docker 服务端可访问并核验服务端版本；失败时保留诊断信息并恢复旧二进制。

### Do not

不要仅因 Docker 服务当时未启动就使用裸机 `fresh` 脚本；不要删除 `/var/lib/docker` 强行重装。

## Oracle 21c TEMP initialization reports ORA-03206

### Symptom

业务包启动 Oracle 21c 时在 TEMP 临时表空间初始化阶段报 `ORA-03206: maximum file size ... in AUTOEXTEND clause is out of range`。

### Cause

2026-09-09 A 原始归档误带旧版 `oracle21c-ee/initialize.sh`，固定 `MAXSIZE 100G` 超过 Smallfile Tablespace（小文件表空间）按数据库块大小允许的上限。这是打包回退，不是 Oracle 数据损坏。

### Correct solution

对当前原始 A 归档先执行 `artifacts/fix-server-a-oracle21c-temp-limit.sh`，再重新运行 `start-service.sh`。后续完整包必须直接携带修正版初始化脚本并运行 `tests/test_oracle21c_initialize.py`。

### Do not

不要删除 Oracle 数据文件、TEMP 表空间或容器数据卷；不要通过无限降低固定上限掩盖脚本未根据块大小计算的问题。

## OpenMetadata configuration does not enable unified login

### Symptom

在 A 的 `.env` 中修改了 OpenMetadata 地址，业务系统仍显示本地用户名密码登录页。

### Cause

`OPENMETADATA_*` 是机器间元数据同步配置；`OIDC_*` 是人员统一认证配置。两套配置相互独立，且默认 `OIDC_ENABLED=false`。

### Correct solution

完整配置 A 的 `OIDC_*`，在 Keycloak（统一身份认证服务）登记精确的登录回调、退出回跳和网页来源地址，然后执行 `start-service.sh` 重建容器。用 `/api/v1/auth/oidc/config` 核对运行时配置。

### Do not

不要把 OpenMetadata 接口令牌当作登录凭据；不要在修改 `.env` 后只执行 `docker restart`，原容器不会载入新环境变量。

## OpenMetadata MySQL password differs after editing the environment file

### Symptom

修改 `OM_MYSQL_ROOT_PASSWORD` 并重启后，OpenMetadata 无法连接其 MySQL 元数据库。

### Cause

该变量只在 MySQL 数据卷首次初始化时设置 root（数据库最高管理账号）密码。已有卷内的实际密码不会随环境文件自动改变。

### Correct solution

首次部署时先修改占位密码再启动。已有数据时，先备份，在 MySQL 内完成改密后同步修改 `.env.openmetadata`；只有明确可丢弃数据时才重新初始化数据卷。

### Do not

不要把该密码与 A 的系统 MySQL、Oracle、Doris 或恢复目标库密码混为一谈；不要在有数据时直接删除 OpenMetadata 数据卷。

## mysql-recovery-target is not the application system database

### Symptom

服务器重启后 `oracle-recovery-mysql` 没有运行，却发现名为 `mysql-recovery-target` 的容器已启动。

### Cause

`oracle-recovery-mysql` 是业务系统元数据库；`mysql-recovery-target` 是 MySQL 业务恢复目标或历史测试容器。容器名称相似，但用途、数据卷和连接配置不同，后者不能代替前者。

### Correct solution

检查 A 的 `.env` 中 `START_LOCAL_MYSQL=true`、`MYSQL_CONTAINER_NAME=oracle-recovery-mysql` 和 `MYSQL_DATA_VOLUME=oracle_recovery_mysql_data`，核对两个容器的镜像、卷和创建时间，再通过 `start-service.sh` 启动系统服务。

### Do not

不要因为当前无运行任务就直接删除 `mysql-recovery-target`；未确认其挂载数据和业务归属前只做只读检查。
