# 2026-08-26 Doris CSV 字段类型自定义发布验证记录

## 1. 发布模式与边界

- 本轮采用模式二“发布验证包”的验证强度，但只执行三个应用文件的最小热更新，不生成完整 Docker Run 包、不重建镜像、不执行回滚演练。
- 128 在本轮开始前处于用户要求的停止状态。验证期间仅临时启动 `oracle-recovery-mysql`、`oracle-recovery-redis` 和 `oracle-recovery-api`；未启动任何 Worker、业务数据库或 Doris。
- 本次无数据库结构迁移。发布前确认 Doris CSV 无在途任务，并备份 128 原文件到 `/root/codex-backups/20260826-csv-custom-types-r1`。

## 2. 发布内容

- `recovery_service/static/ui.html`
  - 字段映射增加 Doris 目标类型输入和常用类型候选。
  - 增加“全部设为 VARCHAR(65533)”按钮。
  - 合并到同一表模式把类型同步到全部文件预览并持久化。
- `recovery_service/api/schemas/doris_csv_import.py`
  - 增加字段类型规范化、白名单和长度/精度范围校验，阻断任意 SQL 片段。
- `recovery_service/services/doris_csv_import.py`
  - 建表前再次规范化字段类型。
  - 超长 VARCHAR 被选作 Doris Key 时继续收窄为 `VARCHAR(255)`；无合法 Key 类型时明确报错。

## 3. 本地验证

- Doris CSV、建表和 SQL 策略相关回归：`17 passed`。
- 完整自动化回归：`239 passed, 1 skipped`。
- Python 编译检查通过。
- `ui.html` 两段内联 JavaScript 均通过 Node.js 语法解析。
- `git diff --check` 通过；128 发布前的三个目标文件与 Git `HEAD` 完全一致，未覆盖服务器独有修改。

## 4. 128 真实 API 闭环

使用隔离连接和两个同表 CSV 文件验证以下场景：

1. 第一份 CSV 的前 205 行为整数，第 206 行为 `late-string`；解析初始类型为 `BIGINT`。
2. 两个文件的同一字段修改为带空格和小写的 `varchar ( 65533 )` 后，接口统一保存并回读为 `VARCHAR(65533)`。
3. 类型值 `VARCHAR(20)); DROP TABLE users; --` 返回 HTTP 422；拒绝后再次回读，两份文件仍均为 `VARCHAR(65533)`，持久化数据未被污染。
4. 导入前 CSV 重写结果包含 206 条数据，最后一条 `late-string` 完整保留。
5. 128 实际页面响应中包含字段类型输入、批量按钮及“全部设为 VARCHAR(65533)”文案。
6. `/api/v1/health` 返回 HTTP 200，MySQL 状态为 `ok`；API 最近 20 分钟无 `ERROR`、`CRITICAL`、`Traceback` 或 `Exception` 日志。
7. 三轮隔离任务、文件节点、任务日志、临时连接和精确暂存目录已全部清理，清理后隔离任务数及连接数均为 0。

## 5. 未完成的环境级验证

- 128 的 Doris 已按用户此前要求停止，当前未监听 8030/8040/9030/9050，服务器也未发现可临时启动的 Doris FE/BE 安装。因此本轮不能完成真实建表和 Stream Load；相关 DDL、类型持久化和导入前数据完整性已由自动化测试及 128 容器内真实服务代码覆盖，待 Doris 恢复后仍应补一次真实 Stream Load。
- 可见浏览器回归被本机 Codex Windows 沙箱初始化错误 `helper_unknown_error: setup refresh had errors` 阻断。没有把静态页面检查冒充为真实点击验证。

## 6. 状态恢复

验证结束后已停止本轮临时启动的 API、系统 MySQL 和 Redis。最终 `oracle-recovery-api`、`oracle-recovery-mysql`、`oracle-recovery-redis` 均为 `Exited (0)`，其余 Oracle Recovery 项目容器保持停止；未触碰独立运行的 `ai-catalog-suspect-service`。
