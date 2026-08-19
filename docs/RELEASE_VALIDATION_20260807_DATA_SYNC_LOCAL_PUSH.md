# 2026-08-07 数据同步本地 MySQL 推送发布验证

## 范围

本次按发布验证模式热更新 `192.168.150.128`，不生成完整 Docker Run 包。

目标能力：

- 数据同步源连接支持 MySQL。
- MySQL 源固定使用 `local_mysql`，`auto` 自动解析为 `stream_load`。
- MySQL 源识别表映射读取 MySQL 元数据，不依赖 Doris Catalog。
- 目标连接仍必须是 Doris。
- 日志和校验明确区分本地 MySQL 推送 Stream Load 与 Doris Catalog 联邦查询。

## 影响文件

- `extracted-app/src/recovery_service/static/ui.html`
- `extracted-app/src/recovery_service/api/v1/data_platform.py`
- `extracted-app/src/recovery_service/services/data_platform.py`
- `extracted-app/src/recovery_service/services/data_sync.py`

## 发布前检查

- `celery inspect active`：空。
- `celery inspect reserved`：空。
- `celery inspect scheduled`：空。
- 系统库 `DataPlatformComponentRun` 中 `queued` / `running` 运行记录：`0`。
- API 健康检查：`{"status":"ok","mysql":{"ok":true,"message":"MySQL 连接成功"}}`。

## 128 热更新

热更新目录：

```text
/root/codex-releases/20260807-data-sync-local-mysql-push-r1-20260807-172931
```

已备份并热更新容器：

| 容器 | 镜像 | 处理 |
|---|---|---|
| `oracle-recovery-api` | `oracle-recovery-service-api:20260730-api-orchestration-dbeaver-sqlapi-canvas-r1` | 覆盖 API、服务与前端文件并重启 |
| `oracle-recovery-worker-data-sync` | `oracle-recovery-service-worker-data-sync:20260728-data-sync-long-run-resilience-r1` | 覆盖数据同步服务文件并重启 |
| `oracle-recovery-worker-data-platform` | `oracle-recovery-service-worker-data-platform:20260728-microservice-phase1-r1` | 覆盖数据平台服务文件并重启 |

容器内 `py_compile` 通过。

## 发布后验证

HTTP 页面 `http://192.168.150.128:8000` 已包含：

- `dataSyncSourceConnection">源连接`
- `["doris", "mysql"].includes(item.engine)`
- `source_engine: sourceProfile?.engine`
- `local_mysql`

HTTP 页面已不包含：

- `dataSyncSourceConnection">Doris 连接`

容器内冒烟：

- `mysql auto -> stream_load`
- `doris auto -> insert_select`
- MySQL 源 Catalog 返回 `local_mysql`
- MySQL 源保存时拒绝 `insert_select`
- MySQL 源保存时缺目标 Doris 连接会报错

发布后状态：

- API 健康检查正常。
- `oracle-recovery-api`、`oracle-recovery-worker-data-sync`、`oracle-recovery-worker-data-platform` 均正常运行。
- 最近 8 分钟三类容器日志未发现 `Traceback`、`CRITICAL`、`Internal Server Error`。

## 用户操作提示

浏览器如仍显示旧字段，需要执行强制刷新：

```text
Ctrl + F5
```

刷新后数据同步页面的源连接字段应显示为“源连接”，下拉中可选择 MySQL 数据连接。

## 2026-08-07 r2 修复：MySQL 源 Stream Load 不再执行 SWITCH

### 问题

用户新建 `CESHI` 数据同步任务，配置为：

- `source_engine=mysql`
- `source_catalog=local_mysql`
- `source_schema=ceshi`
- `target_database=CESHI`
- `sync_method=stream_load`
- 表数：2

运行时报错：

```text
阶段=query_source，异常类型=ProgrammingError，错误码=1064，
原因：MySQL 不支持 Doris 专用语句 SWITCH local_mysql。
```

### 根因

Stream Load 分支读取源表时无条件执行了：

```python
_switch_catalog(cur, source_catalog)
```

该语句只适用于 Doris 源连接。MySQL 源连接应直接查询：

```sql
SELECT ... FROM `源库`.`源表`
```

### 修复

将源端 Catalog 切换限定为 Doris 源：

```python
if source_profile.engine == "doris":
    _switch_catalog(cur, source_catalog)
```

### 本地验证

- `tests/test_data_sync.py`：25 passed。
- `tests/test_data_platform_component_tasks.py`：15 passed。
- `py_compile services/data_sync.py`：通过。

新增回归用例覆盖：

- MySQL 源。
- Stream Load。
- 中文表名和连字符表名。
- 确认不会执行 `SWITCH local_mysql`。

### 128 热更新

热更新目录：

```text
/root/codex-releases/20260807-data-sync-mysql-no-switch-r1-20260807-174957
```

已覆盖并重启：

- `oracle-recovery-api`
- `oracle-recovery-worker-data-sync`
- `oracle-recovery-worker-data-platform`

发布后验证：

- API 健康检查正常。
- 三个容器均已重启并运行。
- 容器内 `data_sync.py` 已包含 `if source_profile.engine == "doris"` 保护。
- 最近 5 分钟未发现 `Traceback`、`CRITICAL`、`Internal Server Error`。

## 2026-08-07 r3 修复：Stream Load 中文字段 header 编码

### 问题

`CESHI` 再次运行后，源 SQL 已正常执行并进入第 1 批 Stream Load，但两张表均失败：

```text
阶段=stream_load，异常类型=UnicodeEncodeError，错误码=latin-1，
原因：'latin-1' codec can't encode characters。
```

### 根因

Stream Load 的 `columns` HTTP header 中包含中文字段名。Python `urllib` 发送 HTTP header 时要求 latin-1 编码，中文列名无法编码。

### 修复

新增 UTF-8 header 传输分支：

- 普通 latin-1 header 继续走 `urllib`。
- header 中包含中文等非 latin-1 字符时，使用 raw socket 发送 UTF-8 header。
- 保留 Doris Stream Load 所需的 `Expect: 100-continue`。

热更新目录：

```text
/root/codex-releases/20260807-data-sync-streamload-utf8-header-r1-20260807-180726
/root/codex-releases/20260807-data-sync-streamload-expect-r1-20260807-181022
```

## 2026-08-07 r4 修复：Stream Load 特殊字段名引用

### 问题

`CESHI` 再次运行后，`transaction` 表成功写入 1000 行，`仓配` 表失败：

```text
Stream Load 失败：SyntaxParseException:
mismatched input '(' expecting {<EOF>, ';'}(line 1, pos 49)
```

### 根因

`仓配` 表字段 `订单总金额(包含客户运费、平台补贴)(原币种)` 包含括号。Doris 解析 Stream Load `columns` header 时，如果列名未加反引号，会把括号按表达式语法解析。

### 修复

Stream Load `columns` header 中所有列名统一使用 Doris 反引号引用：

```python
"columns": ",".join(_q(column) for column in columns)
```

热更新目录：

```text
/root/codex-releases/20260807-data-sync-streamload-quote-columns-r1-20260807-181310
```

### 本地验证

- `tests/test_data_sync.py`：27 passed。
- `py_compile services/data_sync.py`：通过。

新增回归覆盖：

- 中文字段名触发 UTF-8 header 传输。
- 含括号字段名在 `columns` header 中加反引号。

### 128 CESHI 闭环验证

最终运行 ID：

```text
78dd989d-cefd-4861-961f-01760153112e
```

运行结果：

```text
状态：succeeded
成功 2 张表，失败 0 张表，跳过 0 张表，写入 2000 行。
```

表级结果：

| 表 | 状态 | 写入行数 |
|---|---|---:|
| `亚马逊订单应收核对-transaction报表` | `succeeded` | 1000 |
| `亚马逊订单应收核对-仓配报表数据` | `succeeded` | 1000 |

目标表只读行数复核：

| 表 | 当前行数 |
|---|---:|
| `亚马逊订单应收核对-transaction报表` | 2000 |
| `亚马逊订单应收核对-仓配报表数据` | 1000 |

说明：`transaction` 表当前 2000 行，是因为 r3 验证中该表已部分成功追加 1000 行，r4 最终成功运行又追加 1000 行。当前任务写入策略为追加写入，后续如需幂等验证，应改用清空后写入或先清理目标表。

发布后检查：

- API 健康检查正常。
- 三个受影响容器均正常运行。
- 最近 10 分钟未发现 `Traceback`、`CRITICAL`、`Internal Server Error`。
