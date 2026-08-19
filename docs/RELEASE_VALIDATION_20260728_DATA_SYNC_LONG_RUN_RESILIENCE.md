# 2026-07-28 数据同步长耗时连接恢复发布验证

## 1. 问题与范围

百表级 Catalog `INSERT SELECT` 任务运行约 11 小时 54 分钟后，前 43 张表成功，后续 73 张表统一显示 `原因：(0, '')，尚未生成 SQL`；失败表稍后单独运行成功。

核查确认 PyMySQL 2.2.8 的 `InterfaceError(0, "")` 表示连接 socket 已关闭。数据同步旧实现会在 `_switch_catalog` 和 `_table_exists` 中吞掉首次断连异常，并在已关闭连接上继续执行；表级调度没有共享恢复、退避或故障停止扩散，导致短暂基础设施异常被放大为剩余表批量失败。

本次改动严格限定为数据同步模块：

- `services/data_sync.py`
- `tests/test_data_sync.py`
- 数据同步 PRD与发布验证材料

未修改共享 Celery 配置、系统 MySQL、其他业务 Worker、前端页面或完整部署包。

## 2. 备份与哈希

开发前备份：

```text
artifacts/source-snapshots/data-sync-long-run-resilience-prechange-20260728-225559.zip
SHA256: 51D35BA334E8893552341D19CAEBCA0EC2D8480C91DDB1E537717D8BF11B9C6C
```

最终核心文件：

```text
extracted-app/src/recovery_service/services/data_sync.py
SHA256: 8960A3099EBDD8134881CAE9871BD6AD3107E9064C90385AF1DA9D00BEFE6E6D

extracted-app/tests/test_data_sync.py
SHA256: 5B41DE5023931D3252CC78D3718AEB9E6AB6BFCF293A40DA03AE66571CA65135
```

## 3. 实现内容

- `SWITCH Catalog` 只在明确语法/解析错误时执行兼容回退，连接异常原样上抛。
- 目标表存在性检查不再把连接异常误判为目标表不存在。
- 每张表明确记录 `connect_source`、`connect_target`、源/目标元数据、建表、补字段、清空、查询和写入阶段。
- 识别 PyMySQL `0/2006/2013/2014/2055`、连接重置、拒绝、超时和 socket 关闭等基础设施异常。
- 默认每张表最多 3 次连接级尝试；同一多表任务使用共享恢复门控，只允许一个线程探测源 Doris、源 Catalog 和目标 Doris，其他并行线程暂停等待。
- 默认恢复窗口为 30 次、间隔 10 秒，约 5 分钟；不新增页面字段。
- 写入请求发出前可用新连接自动重试；DDL、`TRUNCATE`、`INSERT SELECT` 或 Stream Load 已经或可能发出后，当前表禁止盲目重试并提示核对目标表。
- 恢复窗口耗尽时停止提交新表，未开始表标记为 `skipped`，避免剩余表全部快速判为映射失败。
- 错误日志记录阶段、异常类型、错误码、连接尝试数、`write_started`、SQL 是否生成和最后 SQL；连接错误不再显示为“表映射配置无效”。

正式规则见 `docs/DATA_CHANGE_TRIGGER_AND_SYNC_PRD.md` 第 3.5 节。

## 4. 自动化测试

专项测试增加到 22 项，新增覆盖：

- 关闭 socket 不被 `SWITCH` 二次执行覆盖。
- 只有明确语法错误才执行 Catalog 兼容回退。
- 目标表元数据连接异常原样传播。
- 写入前连接恢复后重试当前表。
- 写入后连接中断不重试当前表。
- 并行线程共享一次恢复探测。
- 恢复耗尽后仅当前表失败，未开始表标记跳过。

128 候选镜像结果：

- API 和 Worker `test_data_sync.py`：各 22 项通过。
- API 和 Worker `test_data_platform_component_tasks.py`：各 13 项通过。
- API 和 Worker `test_microservice_modes.py`：各 8 项通过。
- API 完整测试：`Ran 140 tests`，`OK (skipped=1)`。
- 发布后 API 和独立数据同步 Worker 再次各执行 22 项专项测试，全部通过。

## 5. 128 发布

128 当前运行版本：

| 容器 | 镜像 |
|---|---|
| `oracle-recovery-api` | `oracle-recovery-service-api:20260728-data-sync-long-run-resilience-r1` |
| `oracle-recovery-worker-data-sync` | `oracle-recovery-service-worker-data-sync:20260728-data-sync-long-run-resilience-r1` |

候选镜像 ID：

```text
API: sha256:d2e4ee07c39603e9d32589681005dec704b6cf892de39207d1389763a25c5f45
Worker: sha256:4dbb186f1febb48189cca8b4f8c8934374a221f1b3622488e86473171fcaac9b
```

发布前确认 `data_sync` 队列为 0、Celery 无 active/reserved/scheduled 任务、系统库无 `queued/running` 组件运行，并生成系统 MySQL 全量备份。旧 API 和旧数据同步 Worker 已停止保留用于回滚；其他 6 个业务 Worker 未切换。

## 6. 真实业务闭环

在隔离 Doris 库 `CODEX_DS_LONG_RUN_R1` 创建 3 张源表，通过正式 API 创建数据同步任务并投递独立 `data_sync` 队列：

- 同步方式：`insert_select`
- 写入方式：`append`
- 结构策略：`source`
- 表级并行度：2
- 3 个表级节点全部 `succeeded`
- 3 张目标表行数均为 3
- 表级日志包含 3 条 `insert_select` 和 3 条 `finish_table`

验证证据：

```text
artifacts/128-releases/20260728-data-sync-long-run-resilience-r1/data-sync-long-run-r1-e2e-result.json
SHA256: 47BBE1452787EA94AC6A42E6F0C05E33F2D733B58EB202A05E1FC15056C99922
```

没有为了测试连接恢复而停止共享 Doris。连接中断、并行单飞、恢复耗尽和写入安全分支由候选与已部署镜像中的确定性故障注入测试验证。

## 7. 最终状态与清理

- API 健康检查正常，MySQL 连接成功。
- 7 个业务 Worker 全部 `pong`，`data_sync` 队列为 0。
- API 与数据同步 Worker 最近 300 行日志无 `ERROR`、`Traceback` 或 `CRITICAL`。
- 隔离 Doris 数据库已删除。
- 隔离数据同步节点、表级运行记录、日志和审计记录已删除，复核节点和运行记录均为 0。
- 按用户先前要求，本次没有调用 Chrome；本次属于后端可靠性修复，不涉及页面变更。
- 本次没有生成 Docker Run 完整部署包。

部署与清理脚本：

```text
artifacts/128-releases/20260728-data-sync-long-run-resilience-r1/deploy-data-sync-long-run-resilience-r1-128.sh
SHA256: DEDA2C4AB94B7734833C1EB5584EC41488CC9AC84C973D1B8807EC9EF36F9757

artifacts/128-releases/20260728-data-sync-long-run-resilience-r1/cleanup-data-sync-long-run-r1-128.sh
SHA256: C4A029CEE57997972E399581C95B703201179E56C5B7E671DBF7CDCC6C881F9C
```
