# 双服务器离线部署、升级与排障操作手册

> 文档日期：2026-09-10
>
> 适用范围：A 服务器部署数据恢复与治理业务系统；B 服务器部署 OpenMetadata（开放元数据治理平台）。
>
> 本文是当前双服务器部署的主操作手册。历史交付说明与本文冲突时，以本文及实际交付包的 `VERSION.txt`（版本文件）为准。

## 1. 两台服务器的固定职责

| 服务器 | 固定职责 | Docker（容器运行引擎）要求 | 启动方式 | 不应部署的内容 |
| --- | --- | --- | --- | --- |
| A 服务器 | 业务 API（应用程序接口）、9 个 Worker（后台任务进程）、系统 MySQL 8.4、Redis 7 | 兼容现有 Docker 17.03 | `docker run`（直接启动容器） | 不部署 OpenMetadata |
| B 服务器 | OpenMetadata 1.13.0、其独立 MySQL、Elasticsearch 9.3.0 | Docker 20.10.24 及以上 | Docker Compose 2.24.7（多容器编排工具） | 不部署业务 API、业务 Worker、A 的系统 MySQL |

两台服务器只通过 B 服务器宿主机的 `8585` 端口通信：

```text
A 服务器业务系统  ──HTTP──>  http://B_SERVER_IP:8585/api
管理员浏览器      ──HTTP──>  http://B_SERVER_IP:8585
```

它们不共享 Docker 网络，也不能使用对方的容器名称寻址。

## 2. 本次问题清单与固定结论

### 2.1 B 服务器不是裸机时，不能运行 `fresh` 新装脚本

服务器上只要存在旧 Docker，即使 Docker 服务当时没有启动，也属于“升级”，不是“裸机新装”。

- `install-docker-20.10.24-compose-2.24.7-fresh.sh`：只用于从未安装 Docker、且 `/var/lib/docker` 为空的新服务器。
- `install-docker-20.10.24-compose-2.24.7.sh`：用于已有 Docker 的服务器升级，包含二进制备份和失败恢复。

不能仅凭 `docker ps -a` 没有容器就删除 `/var/lib/docker`。这个目录还可能保存镜像、卷、网络和旧运行状态。

### 2.2 Docker 20 升级脚本曾误判启动失败

旧脚本在 `systemd`（Linux 服务管理器）启动 Docker 后立即执行 `docker info`，而当前服务器的服务单元使用 `Type=simple`。服务管理器返回“已启动”时，Docker Socket（本地通信套接字）可能还没有准备好，于是脚本误判失败并恢复 Docker 17.03。

修正版升级脚本会等待 Docker 服务可访问，并核验服务端版本，不再用一次瞬时检查决定升级成败。

### 2.3 A 服务器 Oracle TEMP 临时表空间初始化脚本回退

旧交付归档中的 Oracle 21c 初始化脚本固定设置 `MAXSIZE 100G`。对于 Smallfile Tablespace（小文件表空间），该值可能超过 Oracle 根据块大小允许的上限，报错：

```text
ORA-03206: maximum file size ... in AUTOEXTEND clause is out of range
```

当前原始 A 归档尚未重新打包，因此使用该归档时必须先执行本文第 7.2 节的最小修复脚本。后续完整打包必须直接包含修正版，不能再依赖人工补丁。

### 2.4 OpenMetadata 地址与统一认证是两套独立配置

- `OPENMETADATA_*`：业务系统向 OpenMetadata 推送元数据和血缘的机器间接口配置。
- `OIDC_*`：业务系统跳转到 Keycloak（统一身份认证服务）进行人员登录的配置。

只修改 OpenMetadata 地址，不会启用统一认证。若 `OIDC_ENABLED=false`，业务系统显示本地用户名密码登录页是预期行为。

### 2.5 修改 `.env` 后只重启旧容器不够

Docker 容器创建时读取环境变量。修改 `.env` 后，单独执行 `docker restart` 只会重启原容器，不会载入新变量。应重新执行包内启动脚本，让脚本重建业务容器。

### 2.6 `OM_MYSQL_ROOT_PASSWORD` 属于 B 服务器

`OM_MYSQL_ROOT_PASSWORD` 是 B 服务器 OpenMetadata 自带 MySQL 元数据库的 `root`（数据库最高管理账号）密码，不是 A 的系统 MySQL 密码，也不是 Oracle、Doris 或业务恢复目标库密码。

首次生产部署必须修改。数据库卷已经初始化后，仅编辑环境文件不会自动修改数据库内部已有密码。

### 2.7 `oracle-recovery-mysql` 与 `mysql-recovery-target` 不是同一个库

- `oracle-recovery-mysql`：A 服务器业务系统自己的系统元数据库，保存账号、配置、任务和运行记录；业务系统正常运行时必须可用。
- `mysql-recovery-target`：MySQL 恢复功能使用的业务恢复目标容器，可能是历史测试或独立业务容器；它不能替代系统元数据库。

服务器重启后如果只看到 `mysql-recovery-target`，不能据此认为系统库已经启动，也不能未经确认就删除该容器。应检查 `.env` 中的 `START_LOCAL_MYSQL`、`MYSQL_CONTAINER_NAME` 和两个容器的挂载卷、镜像及创建时间。

### 2.8 Docker 二进制包不需要编译

`docker-20.10.24.tgz` 是 Linux x86_64 的已编译静态二进制包，`docker-compose-linux-x86_64` 也是已编译可执行文件。安装脚本负责解压、复制、授权和注册服务，不需要在目标服务器编译源码。

### 2.9 业务镜像与 OpenMetadata 的 Docker 版本边界

A 业务镜像和启动脚本继续按 Docker 17.03 兼容基线交付；不因 B 升级 Docker 而重新打业务镜像。B 的 OpenMetadata 包依赖 Docker 20.10+ 与 Docker Compose 2.24.7，不能按 A 的 Docker 17.03 启动方式部署。

### 2.10 OpenMetadata 不与 A 的业务包混装

当前方案是两个独立交付包。A 包没有 OpenMetadata 镜像，B 包没有业务 API、Worker、A 系统 MySQL 或 Redis。部署时必须先按文件名和大小确认上传对象，不能把早期“同机组合包”当作双服务器方案使用。

## 3. 当前交付件与校验值

### 3.1 A 服务器业务包

```text
oracle-recovery-business-docker17-offline-20260909.tar.gz
大小：741107642 字节（约 707 MiB）
SHA256（文件完整性摘要）:
2f7aba3e460f81d1714e91c4dffdbb8a172d4f0bf813b386dff1fc9d5f309c0c
```

该归档兼容 Docker 17.03，但归档内的 Oracle TEMP 初始化脚本是旧版。还需一并上传：

```text
fix-server-a-oracle21c-temp-limit.sh
SHA256:
8a830600653f2c68b275505382948703f2df1a5b22aca7b882cf00d75152adba
```

### 3.2 B 服务器 OpenMetadata 包

```text
openmetadata-docker20-1.13.0-standalone-20260910-fresh-r1.tar.gz
大小：1288198156 字节（约 1.20 GiB）
SHA256:
405b98735dabafac2c05ac71a6a3b9500526387a19b2399fe91b8e3202c08ca4
```

### 3.3 B 服务器 Docker 安装文件

```text
docker-20.10.24.tgz
docker-compose-linux-x86_64
```

根据服务器现状二选一安装脚本：

```text
新装脚本：install-docker-20.10.24-compose-2.24.7-fresh.sh
SHA256: 7c85ffd58ed38925889394b35db0d09e65feb1ee3135a13d52b1e44cbdf385d5

升级脚本：install-docker-20.10.24-compose-2.24.7.sh
SHA256: 8eb7d8bd5cb4a0f223dd0fd07e1c2a77ed350ec69a50e2b1bb1686f3218a1a33
```

Windows 桌面交付根目录为：

```text
C:\Users\zy\Desktop\双服务器部署包-A旧版Docker业务系统-B新版DockerOpenMetadata-20260909
```

其中 A、B 包分别放在对应子目录；本手册的桌面副本名为 `00-双服务器离线部署与升级操作手册-20260910.md`。

## 4. 部署前准备

先记录实际值，后续不要把示例占位符原样写入配置：

```text
A_SERVER_IP=________________
B_SERVER_IP=________________
KEYCLOAK_BASE_URL=________________
KEYCLOAK_REALM=oracle-recovery
业务系统访问地址=http://A_SERVER_IP:8000
OpenMetadata访问地址=http://B_SERVER_IP:8585
```

检查网络和端口：

- 管理员浏览器能够访问 A 的 TCP `8000` 和 B 的 TCP `8585`。
- A 能够访问 B 的 TCP `8585`。
- A 能够访问 Keycloak 的端口，例如 `8090`。
- Keycloak 能够回跳到 A 的 `8000` 端口。
- B 的 OpenMetadata 管理端口 `8586`、MySQL 和 Elasticsearch 不需要对外开放。

生产环境还应先备份现有 `.env`、数据库卷和服务数据，再进行升级或容器替换。

## 5. B 服务器：先判断是新装还是升级

在 B 服务器执行只读检查：

```sh
command -v docker || true
docker --version 2>/dev/null || true
systemctl status docker.service --no-pager 2>/dev/null || true
docker ps -a 2>/dev/null || true
docker images -a 2>/dev/null || true
docker volume ls 2>/dev/null || true
du -sh /var/lib/docker 2>/dev/null || true
```

按下面规则选择，不要凭感觉选择：

| 检查结果 | 判断 | 使用脚本 |
| --- | --- | --- |
| 找不到 Docker 命令、无 Docker 服务、`/var/lib/docker` 不存在或为空 | 真正的新服务器 | `fresh` 新装脚本 |
| Docker 命令或服务存在，但服务未启动 | 已安装旧 Docker | 先启动并检查，再用升级脚本 |
| Docker 正在运行，不论容器列表是否为空 | 已安装旧 Docker | 升级脚本 |
| `/var/lib/docker` 非空但 Docker 命令缺失 | 存在残留或历史数据 | 停止，先备份并调查，不使用新装脚本 |

不要为了通过检查而直接删除这些路径：

```text
/var/lib/docker
/usr/bin/docker*
/usr/bin/containerd*
/etc/systemd/system/docker.service
/etc/docker
```

Docker 不是只安装在一个目录中；手工删除容易造成二进制、服务文件和数据目录版本不一致。

## 6. B 服务器：安装或升级 Docker

### 6.1 真正裸机的新装方式

把下列文件放在 `/home`：

```text
/home/docker-20.10.24.tgz
/home/docker-compose-linux-x86_64
/home/install-docker-20.10.24-compose-2.24.7-fresh.sh
```

执行：

```sh
cd /home
sha256sum install-docker-20.10.24-compose-2.24.7-fresh.sh
chmod 700 install-docker-20.10.24-compose-2.24.7-fresh.sh
sh ./install-docker-20.10.24-compose-2.24.7-fresh.sh
docker --version
docker info
docker compose version
systemctl is-enabled docker.service
systemctl is-active docker.service
```

如果脚本提示 `/var/lib/docker` 非空，不能删除后强行继续，应回到第 5 节确认是否实际存在旧 Docker。

### 6.2 已有 Docker 的升级方式

把下列文件放在 `/home`：

```text
/home/docker-20.10.24.tgz
/home/docker-compose-linux-x86_64
/home/install-docker-20.10.24-compose-2.24.7.sh
```

先确认没有正在执行的重要容器任务，再执行：

```sh
cd /home
sha256sum install-docker-20.10.24-compose-2.24.7.sh
chmod 700 install-docker-20.10.24-compose-2.24.7.sh
sh ./install-docker-20.10.24-compose-2.24.7.sh
docker --version
docker info
docker compose version
systemctl is-active docker.service
```

脚本会备份升级前的 Docker 二进制文件。如果升级失败，会恢复旧二进制。失败时不要立即再次执行，先检查脚本输出中记录的备份目录以及：

```sh
systemctl status docker.service --no-pager
journalctl -u docker.service -n 200 --no-pager
systemctl cat docker.service
ls -l /var/run/docker.sock
```

日志中的 `group docker not found` 或 `cgroup blkio weight` 警告不等于服务启动失败；应以 `docker info` 能否访问服务端及服务端版本为准。

## 7. A 服务器：部署业务系统

### 7.1 解压并校验

```sh
cd /home
sha256sum oracle-recovery-business-docker17-offline-20260909.tar.gz
tar -xzf oracle-recovery-business-docker17-offline-20260909.tar.gz
cd oracle-recovery-business-docker17-offline-20260909
sh ./verify-package.sh
```

不要在升级时直接执行 `cp .env.example .env`，这会覆盖现有密码、统一认证和 OpenMetadata 配置。

- 首次安装且 `.env` 不存在：`cp .env.example .env`。
- 版本升级且 `.env` 已存在：先备份，再只合并本版新增或变更的字段。

```sh
test -f .env && cp -p .env ".env.before-update-$(date +%Y%m%d%H%M%S)"
chmod 600 .env
```

### 7.2 对当前 A 归档应用 Oracle TEMP 最小修复

将 `fix-server-a-oracle21c-temp-limit.sh` 放到解压后的业务包根目录，然后执行：

```sh
cd /home/oracle-recovery-business-docker17-offline-20260909
sha256sum fix-server-a-oracle21c-temp-limit.sh
chmod 700 fix-server-a-oracle21c-temp-limit.sh
sh ./fix-server-a-oracle21c-temp-limit.sh
```

脚本只替换 `oracle21c-ee/initialize.sh`，自动备份旧文件；如果目标不是预期旧版本会拒绝覆盖。重复执行会提示无需修复。

修复后再启动：

```sh
sh ./start-service.sh
sh ./status-service.sh
```

该修复不会删除 Oracle 数据文件，不会重建 Oracle 容器，也不会改业务数据库。

## 8. B 服务器：部署 OpenMetadata

### 8.1 解压与初始配置

```sh
cd /home
sha256sum openmetadata-docker20-1.13.0-standalone-20260910-fresh-r1.tar.gz
tar -xzf openmetadata-docker20-1.13.0-standalone-20260910-fresh-r1.tar.gz
cd openmetadata-docker20-1.13.0-standalone-20260909
sh ./verify-package.sh
cp .env.openmetadata.example .env.openmetadata
chmod 600 .env.openmetadata
vi .env.openmetadata
```

至少修改：

```env
OM_MYSQL_ROOT_PASSWORD=一个足够长且独立的真实密码
```

该密码只服务于 B 的 `openmetadata_mysql` 容器。不要与 A 的 `MYSQL_ROOT_PASSWORD` 混用。

首次启动：

```sh
sh ./start-openmetadata.sh
sh ./status-openmetadata.sh
docker compose ps
```

访问 `http://B_SERVER_IP:8585`。默认本地认证未改变时，初始管理员通常为：

```text
账号：admin@open-metadata.org
密码：admin
```

首次登录后应立即修改默认密码。若交付包或实际版本已改变初始凭据，以启动日志和当前 OpenMetadata 配置为准。

### 8.2 已经初始化后如何修改数据库密码

OpenMetadata 的 MySQL 持久卷初始化后，环境变量不会反向修改数据库内部密码。此时有两种情况：

- 尚无需要保留的数据：停止服务、确认目标卷后重新初始化。
- 已有需要保留的数据：先备份，然后在数据库内执行改密，再同步修改 `.env.openmetadata`。

不要仅改 `.env.openmetadata` 后直接重启，否则 OpenMetadata 可能因数据库认证失败而无法启动。

## 9. 在 OpenMetadata 创建接口令牌

业务系统自动推送元数据时，推荐使用 `ingestion-bot`（数据采集机器人账号）的 Bot Token（机器人接口令牌），不要长期使用管理员个人令牌。

1. 管理员登录 `http://B_SERVER_IP:8585`。
2. 打开 `Settings`（设置）。
3. 打开 `Bots`（机器人账号）。
4. 选择 `ingestion-bot`（数据采集机器人）。
5. 点击 `Generate Token`（生成令牌）或复制当前有效令牌。
6. 将完整令牌保存到受控密码库或 A 服务器 `.env`，不要发到聊天记录、命令历史或截图中。

写入 A 的 `.env` 时只填写原始令牌，不加 `Bearer ` 前缀；业务代码会自动生成 HTTP 授权头：

```env
OPENMETADATA_URL=http://B_SERVER_IP:8585
OPENMETADATA_API_URL=http://B_SERVER_IP:8585/api
OPENMETADATA_API_TOKEN=这里填写原始令牌
OPENMETADATA_SYNC_ENABLED=true
```

`OPENMETADATA_SYNC_ENABLED=true` 只表示开启元数据同步，不表示开启统一登录。

## 10. A 服务器启用 Keycloak 统一认证

### 10.1 A 的 `.env` 配置

示例中的地址必须替换成真实地址：

```env
OIDC_ENABLED=true
OIDC_PROVIDER_NAME=统一身份与权限认证
OIDC_ISSUER_URL=http://KEYCLOAK_SERVER_IP:8090/realms/oracle-recovery
OIDC_DISCOVERY_URL=
OIDC_CLIENT_ID=oracle-recovery-app
OIDC_CLIENT_SECRET=Keycloak中该客户端的真实密钥
OIDC_REDIRECT_URI=http://A_SERVER_IP:8000/api/v1/auth/oidc/callback
OIDC_POST_LOGOUT_REDIRECT_URI=http://A_SERVER_IP:8000/ui
OIDC_SCOPES=openid profile email
OIDC_AUTO_PROVISION_USERS=false
OIDC_COOKIE_SECURE=false
```

名词说明：

- OIDC（OpenID Connect，开放身份连接协议）：系统间传递登录身份的标准协议。
- Issuer URL（签发者地址）：Keycloak 中具体 Realm（认证域）的地址。
- Client ID（客户端编号）：Keycloak 中代表业务系统的应用编号。
- Client Secret（客户端密钥）：业务系统向 Keycloak 证明自身身份的机密值。
- Redirect URI（登录回调地址）：登录成功后 Keycloak 返回业务系统的地址。
- Post Logout Redirect URI（退出回跳地址）：Keycloak 全局退出后返回业务系统的地址。

当前系统使用 HTTP 时 `OIDC_COOKIE_SECURE=false`；改为 HTTPS 后必须设为 `true`。

`OIDC_AUTO_PROVISION_USERS=false` 表示不自动创建本地用户。Keycloak 返回的用户名必须能映射到业务系统已有用户，否则统一认证成功后仍可能被业务系统拒绝。

### 10.2 Keycloak 客户端配置

在 Keycloak 的 `oracle-recovery-app` 客户端中核对：

```text
Client authentication（客户端认证）= 开启
Valid Redirect URIs（有效登录回调地址）=
http://A_SERVER_IP:8000/api/v1/auth/oidc/callback

Valid Post Logout Redirect URIs（有效退出回跳地址）=
http://A_SERVER_IP:8000/ui

Web Origins（允许的网页来源）=
http://A_SERVER_IP:8000
```

回调地址必须与 A 的 `.env` 完全一致，包括协议、IP、端口和路径。

### 10.3 让配置生效

修改 A 的 `.env` 后执行：

```sh
cd /home/oracle-recovery-business-docker17-offline-20260909
sh ./start-service.sh
sh ./status-service.sh
curl -fsS http://127.0.0.1:8000/api/v1/auth/oidc/config
```

返回内容应显示 OIDC 已启用。然后使用无痕窗口访问 `http://A_SERVER_IP:8000/ui`，验证会跳转至 Keycloak。

安全退出测试必须覆盖：

1. 通过 Keycloak 登录业务系统。
2. 点击“安全退出”。
3. 确认业务系统会话失效，并跳转 Keycloak 退出端点。
4. 再次登录时，若 Keycloak 没有其他有效单点登录会话，应重新要求输入凭据。

## 11. B 服务器是否也使用统一认证

A 业务系统与 B OpenMetadata 是两个独立 Web（网页）应用。A 开启统一认证不会自动改变 B。

如果希望 B 也使用同一 Keycloak，需要在 B 的 `.env.openmetadata` 中单独配置：

```env
OM_AUTHENTICATION_PROVIDER=custom-oidc
OM_CUSTOM_OIDC_PROVIDER_NAME=统一身份与权限认证
OM_OIDC_CLIENT_ID=OpenMetadata在Keycloak中的客户端编号
OM_OIDC_CLIENT_SECRET=对应客户端密钥
OM_OIDC_DISCOVERY_URI=http://KEYCLOAK_SERVER_IP:8090/realms/oracle-recovery/.well-known/openid-configuration
OM_OIDC_SCOPE='openid email profile'
OM_OIDC_CALLBACK=http://B_SERVER_IP:8585/callback
OM_OIDC_SERVER_URL=http://B_SERVER_IP:8585
```

OpenMetadata 最好使用独立的 Keycloak 客户端，不与 A 共用客户端密钥和回调地址。修改后需要重建 B 的 OpenMetadata 容器，并在切换前保留一个可恢复的管理员入口。

## 12. 推荐的完整部署顺序

1. 按第 5 节判定 B 是新装还是升级。
2. 在 B 安装或升级 Docker 20.10.24 与 Docker Compose 2.24.7。
3. 在 B 配置并启动 OpenMetadata。
4. 登录 OpenMetadata，修改默认管理员密码并创建 `ingestion-bot` 令牌。
5. 在 A 解压业务包；如果使用当前归档，先应用 Oracle TEMP 最小修复。
6. 备份并编辑 A 的 `.env`，分别配置 OpenMetadata 同步和 Keycloak 统一认证。
7. 在 A 执行 `start-service.sh` 重建并启动业务容器。
8. 逐项完成第 13 节验证。

## 13. 部署后验证清单

### 13.1 B 服务器

```sh
docker --version
docker info
docker compose version
cd /home/openmetadata-docker20-1.13.0-standalone-20260909
sh ./status-openmetadata.sh
curl -fsS http://127.0.0.1:8585/api/v1/system/version
```

验收结果：OpenMetadata 页面可登录，相关容器健康，版本接口有响应。

### 13.2 A 到 B 的网络

```sh
curl -fsS http://B_SERVER_IP:8585/api/v1/system/version
```

验收结果：A 能访问 B；若失败，检查 B 防火墙、监听地址和路由，不要先修改业务代码。

### 13.3 A 业务系统

```sh
cd /home/oracle-recovery-business-docker17-offline-20260909
sh ./status-service.sh
curl -fsS http://127.0.0.1:8000/api/v1/health
curl -fsS http://127.0.0.1:8000/api/v1/auth/oidc/config
docker logs --tail 100 oracle-recovery-api
```

验收结果：API、系统 MySQL、Redis 和 9 个 Worker 正常；日志无持续认证、数据库或线程创建错误。

### 13.4 功能闭环

1. 使用统一认证登录 A。
2. 执行一次安全退出并再次登录。
3. 在业务系统检查 OpenMetadata 连接状态。
4. 使用隔离测试数据推送一条元数据或血缘记录。
5. 在 B 的 OpenMetadata 页面确认实体和血缘可见。
6. 确认失败记录有明确错误，不允许页面显示成功但服务端没有写入。

## 14. 常见故障处理

### 14.1 A 仍显示本地登录页

```sh
grep '^OIDC_' .env
curl -fsS http://127.0.0.1:8000/api/v1/auth/oidc/config
docker inspect oracle-recovery-api | grep -A 2 OIDC_ENABLED
```

常见原因：只修改了 `OPENMETADATA_*`；`OIDC_ENABLED` 仍为 `false`；或修改 `.env` 后只执行了 `docker restart`。

### 14.2 OpenMetadata 同步未启用

确认四个字段同时正确：地址、API 地址、原始令牌、启用开关。不要把 `Bearer ` 写进 `OPENMETADATA_API_TOKEN`。

### 14.3 B 升级后仍显示 Docker 17.03

说明修正版脚本判定升级失败并恢复了旧二进制。检查脚本输出的备份目录、Docker 服务日志和 `/var/run/docker.sock`，不要删除 `/var/lib/docker` 后重装。

### 14.4 A 启动时报 `ORA-03206`

说明 Oracle TEMP 修复尚未应用，或仍在运行旧包内脚本。执行第 7.2 节脚本后重新运行 `start-service.sh`。

### 14.5 修改 OpenMetadata MySQL 密码后无法启动

如果数据库卷已存在，环境配置中的新密码与卷内旧密码不一致。恢复旧密码使服务启动，完成数据库内改密后再同步环境文件；有数据时不要直接删除卷。

### 14.6 A 的系统 MySQL 没启动，但存在 `mysql-recovery-target`

先做只读确认：

```sh
grep -E '^(START_LOCAL_MYSQL|MYSQL_CONTAINER_NAME|MYSQL_DATA_VOLUME|MYSQL_RESTORE_ENABLED|MYSQL_RESTORE_TARGET_MODE)=' .env
docker ps -a --filter name=oracle-recovery-mysql
docker ps -a --filter name=mysql-recovery-target
docker inspect oracle-recovery-mysql 2>/dev/null || true
docker inspect mysql-recovery-target 2>/dev/null || true
```

若 `.env` 使用本地系统库，默认应为：

```env
START_LOCAL_MYSQL=true
MYSQL_CONTAINER_NAME=oracle-recovery-mysql
MYSQL_DATA_VOLUME=oracle_recovery_mysql_data
```

然后重新执行 `sh ./start-service.sh`，由包内脚本启动或创建系统 MySQL。`mysql-recovery-target` 是另一类业务恢复目标；在没有核对挂载卷和业务用途前，不停止、不重命名、不删除。

## 15. 升级和回滚原则

- A 升级时保留 `.env` 和数据卷；不要用 `.env.example` 覆盖现有配置。
- B 升级 Docker 前确认无在途任务；升级脚本的二进制备份在确认稳定前不要删除。
- B 更新 OpenMetadata 前运行 `backup-openmetadata.sh`，并保存 `.env.openmetadata`。
- 回滚应用容器不等于回滚数据库结构。涉及数据库迁移时必须使用该版本配套回滚说明。
- 未确认数据可丢弃时，不删除 A 的 `oracle_recovery_mysql_data` 或 B 的 OpenMetadata 数据卷。
- 不把真实密码、Keycloak 客户端密钥或 OpenMetadata 令牌写入部署包、Git（代码版本库）、截图和聊天记录。

## 16. 下次打包必须完成的检查

- A、B 两个交付包仍然分开，不能恢复成同机组合包。
- A 包继续兼容 Docker 17.03，不引入 Docker Compose 或新 Docker 参数。
- B 包只包含 OpenMetadata 及其基础组件。
- 安装脚本根据“真正裸机”和“已有 Docker”分流，不能只根据 Docker 服务当时是否可访问判断。
- Docker 升级脚本等待服务端可用并核验服务端版本，不能只运行一次 `docker info`。
- A 包内 `oracle21c-ee/initialize.sh` 与当前修正版源码一致，并通过 Oracle TEMP 回归测试。
- A 的 `.env.example` 同时保留 `OIDC_*` 和 `OPENMETADATA_*`，说明两者互不替代。
- 更新脚本不得覆盖已有 `.env`；新增字段必须采用合并方式。
- 文档明确 `.env` 修改后需要重建容器。
- OpenMetadata 文档明确 `OM_MYSQL_ROOT_PASSWORD` 的归属、首次修改要求和已有卷改密规则。
- 文档包含 `ingestion-bot` 令牌创建步骤及“不带 `Bearer ` 前缀”的说明。
- 所有 Shell（命令解释器）脚本和环境文件使用 UTF-8 无 BOM（字节顺序标记）、LF（Linux 换行）并通过 `sh -n` 语法检查。
- 完整包在 Linux 上完成解压、摘要校验、包内校验和启动前检查。
- 未真实执行服务器部署时，发布说明必须明确写“未进行真实服务器验证”。
