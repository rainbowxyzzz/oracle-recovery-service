# SM3 队列与并发运维说明

SM3 脱敏任务通过 Celery + Redis 队列提交和消费。

## 系统接口

查看队列状态：

```http
GET /api/v1/doris-sm3/queue
X-API-Key: change-me-before-production
```

返回内容包括：

- Redis broker 地址
- 队列名
- Redis 队列积压数量
- Worker 数量
- Worker 并发配置
- Celery active / reserved / scheduled 数量
- 系统内排队中的 SM3 任务
- 系统内执行中的 SM3 任务

## 默认 MQ 地址

在 Docker 部署中，默认配置为：

```text
Redis 容器：oracle-recovery-redis
Redis 地址：redis:6379
Broker DB：0
Result DB：1
队列名：celery
Broker URL：redis://redis:6379/0
Result Backend：redis://redis:6379/1
```

对应 `.env`：

```text
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_DB_BROKER=0
REDIS_DB_RESULT=1
CELERY_DEFAULT_QUEUE=celery
CELERY_WORKER_PREFETCH_MULTIPLIER=1
SM3_WORKER_CONCURRENCY=2
```

## 服务器上查看 Redis 队列

进入 Redis 容器：

```bash
docker exec -it oracle-recovery-redis redis-cli
```

查看队列长度：

```redis
LLEN celery
```

如果启用了 Celery 优先级队列，也可以查看相关 key：

```redis
KEYS celery*
TYPE celery
LLEN celery
```

## 查看 Worker 状态

在 API 或 Worker 容器中执行：

```bash
celery -A recovery_service.workers.celery_app:celery_app inspect active
celery -A recovery_service.workers.celery_app:celery_app inspect reserved
celery -A recovery_service.workers.celery_app:celery_app inspect scheduled
celery -A recovery_service.workers.celery_app:celery_app inspect stats
```

## 并发策略

默认：

```text
SM3_WORKER_CONCURRENCY=2
CELERY_WORKER_PREFETCH_MULTIPLIER=1
```

含义：

- 最多同时执行 2 个后台任务。
- Worker 每次只预取 1 个任务，避免某个客户端一次提交多个任务后把队列提前占满。

如果 Doris 资源充足，可以逐步调大：

```text
SM3_WORKER_CONCURRENCY=3
```

不建议一次调得过高。大表脱敏会消耗 Doris CPU、内存、磁盘 IO 和网络。

## 同表互斥

系统已增加同表互斥：

```text
connection_id + database + table_name
```

同一张表如果已有 `queued`、`running` 或 `cancelling` 的 SM3 任务，新任务会被拒绝，并返回已有任务 ID。不同表可以并发执行。

## 前端表现

SM3 页面任务列表上方会展示队列状态：

- MQ 地址
- 队列名
- Redis DB
- Worker 数量
- 并发配置
- 等待数量
- 执行中数量
- 当前执行任务

只要存在运行中任务或队列积压，页面会自动刷新任务列表和队列状态。
