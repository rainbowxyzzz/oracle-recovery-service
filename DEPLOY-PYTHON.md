# 纯 Python 部署（远程 MySQL + 本机 Redis）

## 默认配置

| 组件 | 默认值 | 说明 |
|------|--------|------|
| **MySQL** | `MYSQL_HOST=127.0.0.1` | 改成远程 IP 即可 |
| **Redis** | `REDIS_HOST=127.0.0.1:6379` | 本机，一般不用改 |

不再使用 PostgreSQL。

---

## 1. 准备远程 MySQL

在 MySQL 服务器执行（或让 DBA 执行）`scripts/init_mysql.sql`，按实际改库名/用户/密码。

```sql
CREATE DATABASE oracle_recovery DEFAULT CHARACTER SET utf8mb4;
CREATE USER 'recovery'@'%' IDENTIFIED BY '你的密码';
GRANT ALL ON oracle_recovery.* TO 'recovery'@'%';
FLUSH PRIVILEGES;
```

确保 **运行恢复服务的机器** 能访问 MySQL 的 `3306` 端口。

---

## 2. 准备本机 Redis

```bash
# Ubuntu
sudo apt install -y redis-server
sudo systemctl enable redis-server

# 或仅启动容器
./install-python.sh --with-redis-docker
```

---

## 3. 配置 `.env`

```bash
cp deploy/env.python .env
```

编辑远程库：

```ini
MYSQL_HOST=192.168.1.20
MYSQL_PORT=3306
MYSQL_USER=recovery
MYSQL_PASSWORD=强密码
MYSQL_DATABASE=oracle_recovery

REDIS_HOST=127.0.0.1
REDIS_PORT=6379
```

密码含特殊字符时无需手动转义，程序会自动 URL 编码。

也可用完整 URL 覆盖（高级）：

```ini
DATABASE_URL=mysql+aiomysql://user:pass@host:3306/oracle_recovery?charset=utf8mb4
DATABASE_URL_SYNC=mysql+pymysql://user:pass@host:3306/oracle_recovery?charset=utf8mb4
```

---

## 4. 安装并启动

```bash
chmod +x install-python.sh start-python.sh stop-python.sh
./install-python.sh
./start-python.sh
```

验证：

```bash
curl http://127.0.0.1:8000/api/v1/health
# {"status":"ok","mysql":{"ok":true,"message":"MySQL 连接成功"},...}
```

Swagger：`http://IP:8000/docs` → **setup** → `POST /setup/check/mysql`、`/setup/check/redis`

---

## 5. 前台调试

```bash
source venv/bin/activate
export PYTHONPATH=src
source .env
uvicorn recovery_service.main:app --host 0.0.0.0 --port 8000 --reload
# 另开终端
celery -A recovery_service.workers.celery_app:celery_app worker -l info
```
