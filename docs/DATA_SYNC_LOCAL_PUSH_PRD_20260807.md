# 数据同步本地推送模式 PRD 补充

更新时间：2026-08-07

## 1. 背景

目标 Doris 部署在阿里云并暴露公网访问地址，源 MySQL 位于本地机器或内网环境，源库没有独立公网入口。此时本地机器可以主动访问 Doris，但 Doris 无法通过 Catalog 反向访问本地 MySQL。

因此该场景不能使用 Doris Catalog `INSERT INTO ... SELECT ... FROM catalog.schema.table`。正确方式是由本地数据同步 Worker 主动读取 MySQL，并通过 Doris Stream Load 主动写入目标 Doris。

## 2. 目标

- 数据同步任务允许源连接选择 `mysql`，目标连接仍为 `doris`。
- 源为 MySQL 时，同步方式使用 `stream_load`，`auto` 自动解析为 `stream_load`。
- 源为 MySQL 时，识别表映射直接读取 MySQL 元数据，不依赖 Doris Catalog。
- 运行任务时，按表完成目标结构处理、可选清空、MySQL 分批读取、Stream Load 写入 Doris。
- 日志明确显示真实执行方式为本地推送 Stream Load，不得显示为 Catalog 联邦查询。

## 3. 业务规则

1. 源连接为 `doris` 时，保留现有 `auto` / `insert_select` / `stream_load` 语义。
2. 源连接为 `mysql` 时：
   - `source_catalog` 固定为 `local_mysql`。
   - `source_schema` 表示 MySQL database。
   - `sync_method=auto` 自动解析为 `stream_load`。
   - `sync_method=stream_load` 直接使用本地推送。
   - `sync_method=insert_select` 必须拒绝，并返回明确错误。
3. 目标连接必须是 Doris。
4. Stream Load 采用内存字节流上传，不要求先落地生成文件。
5. 表级日志必须记录源端查询 SQL、目标 Stream Load 批次、返回摘要、写入行数和失败原因。

## 4. 兼容边界

- 不改变既有 Doris Catalog `insert_select` 任务。
- 不改变既有 Doris 源 `stream_load` 任务。
- 不新增数据库表字段，任务配置继续存储在节点 JSON config 中。
- 不改变现有表级并行度、结构基准、写入策略和运行日志接口。

## 5. 验收标准

- 页面新建数据同步任务时，源连接可选择 MySQL，目标连接可选择 Doris。
- MySQL 源任务点击识别表映射后，可列出 MySQL database 下的表和字段。
- MySQL 源任务保存、刷新、重新编辑后配置可正确回读。
- MySQL 源任务运行成功后，Doris 目标表写入行数正确，日志显示 `stream_load`。
- 强制选择 `insert_select` 且源连接为 MySQL 时，后端返回明确错误。
- 现有 Doris Catalog 任务回归通过。
