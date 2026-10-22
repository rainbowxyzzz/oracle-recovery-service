# Oracle UTF8 独立创建脚本 · 20261013-r1

本交付只包含 UTF8 Oracle 的独立入口、配置模板、复用的部署/初始化脚本及回归用例。不包含 Oracle 镜像、应用镜像，不替换 A/B 完整包，不修改应用或业务 `.env`。兼容项目 Docker 17.03 Docker Run 部署方式，不需要 Compose。

## 1. 目录与主库隔离

以下为项目默认值，已有容器的实际挂载优先，脚本发现不一致会停止：

| 项目 | 主 Oracle 21c | 本脚本 UTF8 Oracle |
|---|---|---|
| 容器名 | oracle-recovery-oracle21c-ee | oracle-recovery-oracle21c-utf8 |
| 数据目录 | /data/oracle-recovery/oracle21c/oradata | /data/oracle-recovery/oracle21c-utf8/oradata |
| 表空间目录 | /data/oracle-recovery/oracle21c/tablespaces | /data/oracle-recovery/oracle21c-utf8/tablespaces |
| 监听端口 | 1522 | 1523 |
| SID / PDB | ORCL21C / ORCLPDB1 | ORCLUTF8 / ORCLPDBUTF8 |
| 字符集 | ZHS16GBK | AL32UTF8 / AL16UTF16 |

共享的导入文件目录是 `/data/oracle-recovery/oracle21c/dmp`，它只用于 DMP 等输入文件，不是两个实例共用数据库文件。重建不会移动或删除其中的文件。现有部署助手会对所用的三个挂载目录设置 0777 访问权限，不递归修改目录内文件。

检查真实挂载（只读；主库名称不同时使用实际名称）：

```sh
docker inspect oracle-recovery-oracle21c-ee --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'
docker inspect oracle-recovery-oracle21c-utf8 --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'
```

脚本会核对所有其他容器（含停止状态）的挂载，遇到数据/表空间目录相同、包含或被包含时拒绝继续。即使主库容器不存在，主库标准目录也禁止用作 UTF8 数据目录。不认识的旧挂载会阻断，不猜测后删除。运行期间不要同时启动另一个建库/清理脚本。

## 2. 必须设置与可以不动的配置

仅读取同目录 `oracle-utf8.env`，或命令行指定的配置文件。配置是 shell 格式，仅使用可信文件。

| 配置 | 要求 |
|---|---|
| ORACLE21C_PASSWORD | **必须填写数据库密码**，不是 SSH 密码；模板不保存真实密码。为了与业务包现有回退配置一致，可使用主 Oracle 的数据库密码。 |
| ORACLE21C_IMAGE | 默认 softwareplant/oracle:clean-21.3.0-ee；服务器必须有对应镜像。 |
| ORACLE21C_IMAGE_TAR | 仅镜像尚未加载时可填离线镜像文件路径；不自动联网下载。使用 `--reset` 前须先加载镜像。 |
| 容器、SID/PDB、端口、网络、三个目录 | 与上表一致时不改。旧容器挂载若不同，先核对，不要指向主库数据目录。 |
| ORACLE21C_INIT_SGA_SIZE / INIT_PGA_SIZE | 已设为 8192 / 2048（MB），不必再手动调内存。 |
| ORACLE21C_MEMORY_LIMIT | 已设为 20g，是容器内存上限，不是立即预分配 20GB；SGA 为 8GB、PGA target 为 2GB。 |
| ORACLE21C_STARTUP_TIMEOUT_SECONDS | 默认 1800 秒。 |

建库/停止库启动前要求宿主 `MemAvailable` 至少 12GB，不满足就停止；这不代表可保证所有后续导入负载。初始 SGA 8GB 高于截图报告的最低 5744MB，但不同机器仍需以实际 DBCA 结果为准。

保留现有 TEMP 配置：初始恢复临时文件 20G，按实际 Smallfile 上限截断最大值；请保证数据盘容量和数据库增长空间。**磁盘 TEMP 20G 与容器内存 20g 是不同概念。**

### 与业务 `.env` 的关系

- 独立脚本完全不读、不改业务 `.env`，不需要 API_IMAGE、APP_IMAGE_TAR、SM4_WORKER_IMAGE。
- **不要把独立配置的 `ORACLE21C_*` 整段复制到业务 `.env`！** 业务 `.env` 的 `ORACLE21C_*` 指主库，UTF8 配置使用 `UTF_ORACLE21C_*`。
- 容器/网络/目录采用默认值、密码与主库一致且原业务配置未自定义 UTF8 时，独立建好后通常无需修改业务配置。
- 若业务 `.env` 原来配置过 UTF8 密码或目录，必须与独立配置保持一致；若 UTF8 密码不同于主库，则业务 `.env` 需一次性配置 `UTF_ORACLE21C_PASSWORD`。这些是环境配置，不需要每次版本升级重填。
- 业务包仍需要管理本地 UTF8 时保持 `UTF_ORACLE21C_MODE=auto`（或 `container`），不是 `external`。本独立脚本不会强制用户放弃 UTF8。
- 如果以后打算删除 UTF8 容器并由业务 `start-service.sh` 新建，还需在业务 `.env` 一次性设置 `UTF_ORACLE21C_INIT_SGA_SIZE=8192`、`UTF_ORACLE21C_INIT_PGA_SIZE=2048`、`UTF_ORACLE21C_MEMORY_LIMIT=20g`；否则旧业务包可能再次沿用主库的小内存默认值。本脚本不偷偷修改那份配置。

## 3. 使用步骤

将本脚本包上传到目标 Linux 服务器一个新目录；不要覆盖旧业务包。以 root 或有 Docker/目录管理权限的账户操作：

```sh
tar -xzf oracle-utf8-standalone-20261013-r1.tar.gz
cd oracle-utf8-standalone-20261013-r1
sha256sum -c SHA256SUMS
cp oracle-utf8.env.example oracle-utf8.env
chmod 600 oracle-utf8.env
vi oracle-utf8.env
```

只需先填 `ORACLE21C_PASSWORD`，再确认镜像、端口和目录。不要重复执行 `cp` 覆盖已填写的配置。

检查镜像和可用内存：

```sh
docker image inspect softwareplant/oracle:clean-21.3.0-ee >/dev/null
free -m
```

缺镜像时先加载已有离线镜像，例如 `docker load -i /实际路径/oracle-image.tar.gz`（请替换为真实文件）。本小包不带镜像。

### 新环境或正常启动

```sh
sh ./create-oracle-utf8.sh
```

已存在的正确容器会复用，不删除数据库。如果旧容器 SGA/PGA 仍为 1536/1024，则拒绝“改 env 假装生效”，明确要求人工修复或选择下述重建。

### 清理之前失败的 UTF8 初始化残留并重建

**仅在确认可创建全新空 UTF8 库、没有正在使用该库的任务时执行。已有 UTF8 业务数据不会自动迁移。**

```sh
sh ./create-oracle-utf8.sh --reset
```

重建顺序：

1. 核对目录、其他容器挂载、UTF 身份/实际挂载、内存和本地镜像；阻断条件下不停止旧容器。
2. 保存旧 UTF 容器配置为 `oradata.container-backup-时间-进程号.json`，权限 600（包含密码，勿外传）。
3. 仅停止并移除 `oracle-recovery-oracle21c-utf8`；不执行主库 stop/rm，不使用卷删除参数。
4. 将 `oradata`、`tablespaces` 分别改名为 `oradata.backup-时间-进程号`、`tablespaces.backup-时间-进程号`，保存在原父目录。
5. 用新空目录及 8GB/2GB 参数建库，等待 PDB READ WRITE、验证真实字符集、目录对象、SYSTEM 登录及 SQLPlus。

无旧容器但有失败残留目录时，仍可使用 `--reset` 原地改名备份。不会递归删除整个 `/data/oracle-recovery`，也不会移动 DMP 或主库文件。备份长期保留，由管理员确认无需恢复后再自行处理。

若重建中断，旧备份仍在，先看错误与备份路径；**不要反复 reset**。恢复旧库需先停止新 UTF 实例、保留新数据，再根据备份配置恢复两组旧目录及原容器配置；本工具不提供自动回滚，不要把新旧目录混用。

自定义配置文件写法：`sh ./create-oracle-utf8.sh --reset /绝对路径/oracle-utf8.env`。

## 4. 如何判断成功

必须看到最后的 `[oracle-utf8] READY: ... / ORCLPDBUTF8 / AL32UTF8`，不能只看容器 Up 或初始化进度 100%。初始化脚本实际检查 PDB、字符集和数据库登录失败时会非零退出。

```sh
docker ps --filter name=oracle-recovery-oracle21c-utf8
docker logs --tail 100 oracle-recovery-oracle21c-utf8
```

数据库连接：目标服务器 IP、端口 `1523`、Service Name `ORCLPDBUTF8`、用户 `SYSTEM`、独立配置中的数据库密码。随后回到原业务包目录启动业务服务；无需再次重建 UTF 库。

## 5. 本次修复与验证范围

- 提供独立配置、旧失败数据备份重建和主库挂载隔离检查；明确内存初始化与已有 SPFILE 的区别。
- 修复共享初始化脚本读取历史失败日志的问题：先查 PDB；仅按本次容器 StartedAt 之后的日志判定启动失败。DBCA 历史文件另作诊断，不作为本次失败判定。
- Windows 下 33 项 Oracle 相关 mock 回归通过；Linux 下 3 项真实文件移动测试通过，Docker 为模拟，验证旧数据备份、主库/DMP 文件保留、非空残留阻断及符号链接拒绝。
- 本交付完成 Linux 解压、SHA256、UTF-8 无 BOM/LF、shell 语法和配置加载检查。
- **未在用户这台新服务器执行真实 DBCA 建库，也未发布或重启 128，不能把 mock 通过当成新服务器数据库已经启动成功。**

随包文件系统自检仅供开发验证，在 Linux 上创建独立测试目录、模拟 Docker，不操作真实数据库：

```sh
ORACLE_UTF8_SCRIPT_DIR="$PWD" ORACLE_UTF8_FILESYSTEM_TEST=1 python3 tests/test_oracle_utf8_reset_linux.py
```

启动/重建脚本本身不依赖 Python。
