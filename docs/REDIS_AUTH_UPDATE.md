# Redis 密码配置与更新工具

## 需求与边界（2026-11-05）

为现有 Docker Run 部署提供独立的 `deploy/configure-redis-auth.py`。使用宿主机
Python 3.7+ 标准库和 Docker，不安装依赖、不拉取镜像，不自动发布到测试服务器。
仅支持本机独占的 `oracle-recovery-redis`、`oracle-recovery-api` 和
`oracle-recovery-worker*` 容器；数据库、其他项目和网络不变。

工具自动管理维护窗口：先检查正在运行的 Worker，执行中、已预取或定时任务非空，
或 Worker 不响应时退出；停止 API 后再次检查，再向 Worker 发出 TERM 并等待正常退出，
不强制杀进程。随后备份并更新认证，自动恢复原来运行的服务并检查连接与 Worker 响应。
从 Redis 现有容器环境或部署 `.env` 获取旧密码，交互输入新密码；为避免旧版 URL
编码与 shell 配置差异，新密码限定为至少 20 位 ASCII 字母、数字和 `_-`。

工具备份 `.env`、inspect 及 Redis SAVE 快照，保留旧容器，并对客户端 docker commit，
防止容器内热更新丢失；通过 Docker 1.27 API 按原配置重建，复用原数据卷和镜像。
旧 URL 覆盖项逐个替换密码，保留原 host、port、DB；不删除已有队列或结果。
Redis 通过持久化配置文件开启 requirepass，不把密码放入命令行。
保持原运行/停止意图：原来运行的服务自动恢复，原来停止的服务保持停止。
数据库和其他项目不随本脚本启动。异常时停止已启动的新客户端并恢复原配置与服务状态。

成功标准：Redis 无认证 PING 返回 NOAUTH、正确密码返回 PONG、错误密码被拒绝，
API/Worker 的三类 Redis URL 在容器配置中均已同步；重启 Redis 后再次验证认证。
失败自动回滚已替换容器及 `.env`，保留审计目录；管理员也可用同一工具手动回滚。
拒绝外部 Redis、共享卷、特殊 Redis 启动命令和 ACL 用户模式，不猜测现场配置。

## 使用

上传单个脚本到服务器部署目录，确认正在使用的 `.env` 在该目录，直接执行：

```sh
python3 ./configure-redis-auth.py --env-file ./.env
```

交互输入并确认新密码，等待“配置完成”提示即可，不需要手动停止或启动容器。
发现任务忙或检查失败会明确报错，换密码前退出时不会改变密码。不要删除备份容器或快照镜像。
配置成功后，脚本会把实际密码写入当前部署的 `.env` 的 `REDIS_PASSWORD`。旧 `.env` 缺少该字段时会自动添加，已有字段时会更新。如果 `.env` 显式设置了 `REDIS_URL`、`CELERY_BROKER_URL` 或 `CELERY_RESULT_BACKEND`，脚本也会同步其中的密码。以后修改密码运行同一命令即可。实际密码保存在部署服务器的 `.env` 中，GitHub 只保留文档和脚本。

需要时也可单条命令回滚；工具先检查任务并自动停止当前客户端：

```sh
python3 ./configure-redis-auth.py --rollback /绝对路径/redis-auth-backup-时间戳
```

备份目录包含密码，权限为仅当前管理员可读。需要可写部署目录、可访问
`/var/run/docker.sock`、足够存储空间。脚本保留原端口暴露，不自动修改防火墙。

如果 Docker 启动容器失败，终端会显示容器名及更新或回滚阶段，Docker 原始报错写入备份目录的 `docker-update-error.txt` 或 `docker-rollback-error.txt`。只在服务器本地查看这些文件，分享前应移除凭据。

若手动 `docker start` 也报 `overlay2 ... merged: device or resource busy`，先处理宿主机挂载占用。用下面的只读命令检查一个报错容器的挂载，换成实际容器名。返回的 `/proc/<PID>/mountinfo` 可用于定位持有挂载的进程。

```sh
p=$(docker inspect --format '{{index .GraphDriver.Data "MergedDir"}}' oracle-recovery-worker-sm4)
[ -n "$p" ] && grep -Fl -- " $p " /proc/[0-9]*/mountinfo 2>/dev/null
```

有匹配时，用 `ps -p <PID> -o pid,ppid,comm` 确认进程归属；同一挂载命名空间可能有多个 PID。没有匹配时，可用 `journalctl -u docker -n 80 --no-pager` 查看 Docker 侧的挂载和卸载错误。保留原容器、备份及数据卷。若需重启 Docker，应先确认同机其他容器的停机窗口。

待 Docker 能正常启动容器后，用本工具的 `--rollback` 读取此次备份，恢复更新前的 `.env` 和原先运行的服务。确认应用恢复后再尝试配置密码。

## 同一服务器全量更新

1. 确认没有在途任务，备份旧部署目录的 `.env`、现场修改过的 `config/` 文件和系统 MySQL 数据库。页面里保存的任务、调度、数据库连接、审批授权配置及用户都在系统 MySQL 中。核对本机 MySQL 实际挂载的数据卷，或者核对外部 MySQL 地址和数据库名。若配置了 `DATABASE_URL`，以它指定的数据库为准。
2. 解压新包后，先在新包目录运行 `sh ./verify-package.sh`。通过后把**正在使用的旧 `.env`** 复制到新包目录，并设置 `chmod 600 .env`。原样保留数据库连接参数、`MYSQL_DATA_VOLUME`（本机 MySQL）、`CREDENTIAL_ENCRYPTION_KEY`、`SECRET_KEY` 和 Redis 连接参数。需要时逐项合并现场修改过的 `config/` 文件。包内 `package-images.env` 使用新包自带的版本。
3. 保留现有系统 MySQL、Redis 容器及其数据卷。若已用本工具设置密码，也要保留旧目录中 Redis 容器挂载的 `redis-auth-backup-*/redis.conf`，它是正在使用的服务端配置。升级时保持 `STOP_LOCAL_INFRA=false`，保留 SM4 JAR、查询导出等业务数据卷及 Oracle 宿主机数据目录。旧目录仍含 Redis 挂载文件时，应保留该目录。
4. 在新包目录运行 `sh ./start-service.sh`。启动脚本重建 API 和 Worker，使其加载复制过来的 `.env`。运行 `sh ./status-service.sh`，登录页面检查原有任务、连接、调度和授权配置，并确认 Worker 能正常连接 Redis。

Redis 的两种升级情形：

- **旧 Redis 已启用认证**：复制带实际密码的旧 `.env` 后继续使用现有 Redis 容器。若旧 `.env` 缺少 `REDIS_PASSWORD`，在服务器本地补上 Redis 当前实际使用的密码，并核对显式 Redis/Celery URL 中的密码一致。
- **旧 Redis 未启用认证，或使用当前完整包新建本机 Redis**：先以空的 `REDIS_PASSWORD=` 和匹配的连接配置启动，再运行本工具。当前完整包新建 Redis 时，`.env` 中的密码只会传给应用，Redis 服务端认证由本工具设置。

若系统 MySQL 数据卷已经丢失，或改在另一台服务器部署，需要先恢复系统 MySQL 备份，并配置应用连接恢复后的数据库。复制 `.env` 本身无法恢复任务、连接或历史记录。

## 本地验证

专项测试包含原数据卷/网络/兼容参数保留、显式 URL 更新、重复密码配置去重、
客户端快照、运行/停止状态保留、部分重建失败回滚、运行中客户端及外部 URL 拒绝、
历史备份容器排除、Python 3.7 语法和 Linux 文本格式。均使用模拟 Docker，
不代表真实 Docker 17、Redis 或 Celery 业务联调已通过。
