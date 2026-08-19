# Doris SQL/ETL 编排中心 PRD

更新时间：2026-07-13

## 1. 模块定位

Doris SQL/ETL 编排中心用于在现有数据连接、任务队列、调度中心和审计体系基础上，提供面向 Doris 的 SQL 执行、Oracle 到 Doris 的数据抽取写入、ETL 任务编排和定时调度能力。

它不是单纯的 SQL 输入框，而是一个可治理、可复用、可调度、可审计的数据处理模块。

核心目标：

- 支持直接执行 Doris SQL，用于建表、插入、清理、校验、查询预览等。
- 支持通过 Oracle 自定义查询逻辑抽取数据，并写入 Doris 指定表。
- 支持单任务节点独立运行。
- 支持多个任务节点组合运行，形成 ETL 流程。
- 支持手动执行、定时调度、失败重试、运行日志和审计追踪。
- 支持大数据量场景下的分批抽取、分批写入、进度跟踪和失败定位。

建议模块名称：

```text
Doris SQL/ETL 编排中心
```

## 2. 业务背景

当前系统已经具备：

- 数据连接管理
- Oracle DMP 自动导入
- Doris CSV 导入
- Doris SM3 / SM4 数据处理
- 批量授权
- 离线开发与调度中心
- Celery Worker 队列执行

后续需要补齐一种更通用的数据加工能力：

用户不一定总是通过 DMP 或 CSV 文件导入数据，而是希望直接从 Oracle 中编写特定查询逻辑，例如关联查询、筛选、字段转换、聚合等，然后将结果写入 Doris 表中，作为部门库、分析库或后续脱敏加密流程的数据源。

示例：

```sql
SELECT
  a.ID,
  a.NAME,
  b.ORG_NAME,
  a.UPDATE_TIME
FROM ORA_SCHEMA.PERSON_INFO a
LEFT JOIN ORA_SCHEMA.ORG_INFO b ON a.ORG_ID = b.ORG_ID
WHERE a.UPDATE_TIME >= :start_time
```

写入 Doris：

```text
DWH_某处.PERSON_INFO_ETL
```

## 3. 目标用户

- 数据管理员：配置 Oracle / Doris 连接、维护任务、查看运行状态。
- 数据加工人员：编写 Oracle 查询 SQL、配置字段映射和写入策略。
- 系统管理员：控制权限、查看审计、处理失败任务。
- 业务处室用户：使用最终写入 Doris 的结果表。

## 4. 需求范围

### 4.1 必须支持

- Doris SQL 执行能力
- Oracle 查询 SQL 预览能力
- Oracle 查询结果写入 Doris
- 字段映射和类型映射
- 单节点任务执行
- 多节点任务编排
- 手动运行
- 定时调度
- 运行日志
- 失败重试
- 任务停止
- 审计记录

### 4.2 首版建议支持

- Oracle -> Doris 单源单目标表 ETL
- Doris SQL 独立执行节点
- 多节点串行编排
- 按天、按周、按月、间隔分钟调度
- 分批抽取、分批写入
- 行数校验
- 错误日志下载/查看

### 4.3 后续增强

- 多 Oracle 源合并
- 多目标表写入
- 节点条件分支
- 增量水位自动管理
- 复杂 DAG 并行执行
- 数据质量规则
- 自动建表推荐
- 任务版本发布审批

## 5. 核心功能设计

### 5.1 Doris SQL 工作台

用于直接对 Doris 执行 SQL。

功能：

- 选择 Doris 数据连接
- 选择默认数据库
- 输入 SQL
- SQL 类型识别：
  - SELECT
  - INSERT
  - CREATE TABLE
  - DROP TABLE
  - TRUNCATE TABLE
  - ALTER TABLE
  - GRANT / REVOKE，默认首版不开放或仅管理员开放
- 执行前预检查
- 查询结果预览
- 执行耗时、影响行数、错误信息展示
- SQL 执行日志保存
- 危险 SQL 二次确认

安全规则：

- 普通用户默认只能执行 SELECT。
- INSERT / DDL / DROP / TRUNCATE 需要单独权限。
- DROP / TRUNCATE / DELETE 无 WHERE 等高风险语句必须二次确认。
- 所有 SQL 都要保存执行人、执行时间、连接、数据库、SQL 文本、结果摘要。

### 5.2 Oracle 查询预览

用于验证 Oracle 查询逻辑。

功能：

- 选择 Oracle 数据连接
- 输入 Oracle 查询 SQL
- 支持参数：
  - 日期参数
  - 字符串参数
  - 数字参数
  - 系统内置参数，例如调度日期、上次成功时间
- 限制预览行数，默认 100 行
- 获取字段名、字段类型、样例数据
- 统计预估行数，可选
- SQL 语法错误提示

约束：

- Oracle 预览 SQL 首版只允许 SELECT。
- 不允许执行 DDL / DML。
- 预览必须自动包裹行数限制，避免页面卡死。

### 5.3 Oracle -> Doris ETL 任务

用于将 Oracle 查询结果写入 Doris。

配置项：

- 任务名称
- Oracle 源连接
- Oracle 查询 SQL
- 查询参数
- Doris 目标连接
- Doris 目标库
- Doris 目标表
- 字段映射
- 写入策略
- 分批大小
- 并发配置
- 校验规则

写入策略：

```text
append
追加写入目标表。

truncate_insert
先清空目标表，再写入本次结果。

drop_create_insert
删除目标表，按字段映射重建目标表，再写入。

create_if_not_exists_insert
目标表不存在则创建，存在则追加。
```

首版建议默认：

```text
truncate_insert
```

原因：

- 比 append 更可控，避免重复数据。
- 比 drop_create_insert 更安全，避免误删表结构和权限。
- 如果用户明确需要重建表，再选择 drop_create_insert。

### 5.4 字段映射

系统应在 Oracle 查询预览后自动生成字段映射。

字段映射内容：

- Oracle 字段名
- Oracle 字段类型
- Doris 字段名
- Doris 字段类型
- 是否写入
- 默认值
- 空值处理
- 表达式转换，后续增强

类型映射示例：

| Oracle 类型 | Doris 推荐类型 |
|---|---|
| VARCHAR2 | VARCHAR(n) |
| CHAR | VARCHAR(n) |
| NUMBER(p,0) | BIGINT / LARGEINT |
| NUMBER(p,s) | DECIMAL(p,s) |
| DATE | DATETIME |
| TIMESTAMP | DATETIME |
| CLOB | STRING 或 VARCHAR(65533) |

注意：

- Doris 表首列、Key 列和 STRING 类型组合需要谨慎处理。
- 如果目标表要作为明细表，首版建议使用 DUPLICATE KEY。
- 如果用户指定 UNIQUE KEY，需要配置 key 字段。

### 5.5 Doris 目标表管理

目标表可以有三种来源：

1. 使用已有表
2. 根据字段映射自动建表
3. 使用用户自定义建表 SQL

自动建表配置：

- 表模型：
  - DUPLICATE KEY
  - UNIQUE KEY，后续增强
  - AGGREGATE KEY，后续增强
- 分桶字段
- 分桶数量
- 副本数
- 分区字段，后续增强
- 表属性

首版默认：

```sql
DUPLICATE KEY(...)
DISTRIBUTED BY HASH(首个字段) BUCKETS 10
PROPERTIES ("replication_num" = "1")
```

具体默认值应允许在系统配置中调整。

### 5.6 数据写入方式

可选技术路线：

#### 方案 A：应用层分批读取 Oracle，再通过 Doris Stream Load 写入

优点：

- 适合较大数据量。
- 写入性能较好。
- 可分批重试。
- 可记录每批状态。

缺点：

- 实现复杂度略高。
- 需要处理 CSV/JSON 转义、NULL、日期格式。

#### 方案 B：应用层分批读取 Oracle，再使用 Doris MySQL 协议 INSERT

优点：

- 实现简单。
- 便于首版落地。

缺点：

- 大数据量性能较差。
- INSERT SQL 太大时容易失败。
- 分批大小需要严格控制。

#### 方案 C：落地临时文件，再走 Doris Broker/Stream Load

优点：

- 更适合大批量。
- 便于失败后保留中间文件。

缺点：

- 需要文件目录管理和清理。
- 复杂度较高。

首版建议：

```text
优先支持方案 A：Oracle 分批读取 + Doris Stream Load。
保留方案 B 作为小数据量兜底能力。
```

### 5.7 分批与进度

任务执行时应按批次处理。

配置：

- fetch_size，默认 5000
- load_batch_size，默认 5000 或按文件大小切分
- 最大批次数
- 单批超时时间
- 全任务超时时间

运行指标：

- Oracle 已读取行数
- Doris 已写入行数
- 当前批次号
- 成功批次数
- 失败批次数
- 当前耗时
- 平均吞吐

### 5.8 校验能力

首版必须支持：

- 源端行数统计
- 目标端写入行数统计
- 批次行数累计校验
- 失败行记录
- 错误批次日志

后续增强：

- 主键去重校验
- 抽样校验
- 字段空值率校验
- 业务规则校验

## 6. 单任务节点运行

任务节点类型建议：

### 6.1 Doris SQL 节点

输入：

- Doris 连接
- 数据库
- SQL
- 参数

输出：

- 执行状态
- 影响行数
- 查询结果预览，若为 SELECT
- 执行日志

### 6.2 Oracle 查询节点

输入：

- Oracle 连接
- 查询 SQL
- 参数

输出：

- 字段结构
- 样例数据
- 查询行数，若启用

### 6.3 Oracle 到 Doris 写入节点

输入：

- Oracle 连接
- Oracle 查询 SQL
- Doris 连接
- 目标库表
- 字段映射
- 写入策略

输出：

- 写入批次
- 写入行数
- 校验结果

### 6.4 数据校验节点

输入：

- Doris 连接
- 校验 SQL
- 期望条件

输出：

- 通过/失败
- 实际值
- 错误说明

## 7. 多任务节点组合运行

需要复用或扩展现有离线开发/数据平台编排能力。

流程示例：

```text
节点1：Doris SQL，清理临时表
节点2：Oracle -> Doris，抽取并写入临时表
节点3：Doris SQL，聚合写入正式表
节点4：数据校验，检查目标表行数
节点5：SM4 加密任务，可选
```

首版组合规则：

- 支持串行执行。
- 支持节点失败后停止流程。
- 支持从失败节点重跑。
- 支持查看每个节点日志。
- 支持复制已有流程。

后续增强：

- 节点并行执行
- 条件分支
- 参数透传
- 节点输出作为下游输入

## 8. 调度能力

调度类型：

- 手动执行
- 每日
- 每周
- 每月
- 间隔分钟
- 指定时间窗口，后续增强

调度配置：

- 是否启用
- 执行时间
- 失败重试次数
- 重试间隔
- 超时时间
- 并发限制
- 是否允许上一轮未结束时跳过本轮

建议默认：

```text
同一个 ETL 任务同一时间只允许一个运行实例。
如果上一轮未完成，下一轮默认跳过并记录日志。
```

## 9. 参数体系

任务应支持参数化，避免每次改 SQL。

参数来源：

- 手动输入
- 调度日期
- 当前日期
- 上次成功时间
- 固定配置

内置参数示例：

```text
${biz_date}
${biz_date_minus_1}
${current_time}
${last_success_time}
```

SQL 示例：

```sql
WHERE UPDATE_TIME >= TO_DATE('${biz_date}', 'YYYY-MM-DD')
```

注意：

- 参数替换必须保存最终执行 SQL 快照。
- 敏感参数不能明文展示。

## 10. 权限与安全

权限项建议：

```text
dorisSql:read
dorisSql:executeSelect
dorisSql:executeDml
dorisSql:executeDdl
dorisEtl:read
dorisEtl:create
dorisEtl:update
dorisEtl:execute
dorisEtl:schedule
dorisEtl:delete
dorisEtl:logs
```

安全要求：

- SQL 执行必须记录审计。
- 高危 SQL 必须二次确认。
- Oracle 查询节点首版只允许 SELECT。
- Doris DDL/DML 权限必须和 SELECT 分开。
- 任务删除不删除运行历史。
- 密码、连接串等敏感信息不进入前端。

## 11. 数据模型建议

### 11.1 ETL 任务定义表

```text
doris_etl_task_definitions
```

字段：

- id
- name
- description
- task_type
- status
- source_connection_id
- target_connection_id
- config_json
- created_by
- created_at
- updated_at

### 11.2 ETL 节点定义表

```text
doris_etl_task_nodes
```

字段：

- id
- task_id
- node_key
- node_type
- name
- config_json
- sort_order

### 11.3 ETL 节点关系表

```text
doris_etl_task_edges
```

字段：

- id
- task_id
- from_node_key
- to_node_key
- condition_json

### 11.4 ETL 运行批次表

```text
doris_etl_runs
```

字段：

- id
- task_id
- trigger_type
- state
- started_at
- finished_at
- total_rows
- success_rows
- failed_rows
- message
- error_message

### 11.5 ETL 节点运行表

```text
doris_etl_node_runs
```

字段：

- id
- run_id
- node_key
- node_type
- state
- started_at
- finished_at
- input_snapshot
- output_snapshot
- row_count
- error_message

### 11.6 ETL 批次明细表

```text
doris_etl_load_batches
```

字段：

- id
- run_id
- node_run_id
- batch_index
- state
- source_rows
- loaded_rows
- started_at
- finished_at
- error_message
- load_label

### 11.7 ETL 日志表

```text
doris_etl_logs
```

字段：

- id
- run_id
- node_run_id
- level
- stage
- message
- sql_text
- duration_ms
- payload_json
- created_at

## 12. 页面设计建议

### 12.1 一级入口

建议新增导航：

```text
Doris SQL/ETL 编排中心
```

### 12.2 页签

- SQL 工作台
- ETL 任务
- 编排画布
- 调度计划
- 运行记录
- 执行日志

### 12.3 SQL 工作台页面

布局：

- 左侧：连接、数据库、SQL 类型、参数
- 中间：SQL 编辑器
- 下方：结果预览、执行日志

### 12.4 ETL 任务页面

功能：

- 新建任务
- 编辑任务
- 复制任务
- 删除任务
- 立刻执行
- 生成调度
- 查看运行记录

### 12.5 Oracle -> Doris 配置页面

分区：

- 源端 Oracle 查询
- 查询预览
- 目标 Doris 表
- 字段映射
- 写入策略
- 分批配置
- 校验配置

### 12.6 编排画布

应尽量复用现有离线开发/数据平台画布。

节点：

- Doris SQL
- Oracle 查询
- Oracle -> Doris 写入
- 数据校验
- SM3 任务
- SM4 任务

## 13. 执行流程

### 13.1 新建 Oracle -> Doris ETL

1. 选择 Oracle 连接。
2. 编写 Oracle 查询 SQL。
3. 点击预览。
4. 系统返回字段结构和样例数据。
5. 选择 Doris 连接、目标库、目标表。
6. 配置字段映射。
7. 选择写入策略。
8. 保存任务。
9. 手动执行或配置调度。

### 13.2 执行 ETL

1. 创建运行批次。
2. 渲染参数。
3. 预检查 Oracle 查询。
4. 预检查 Doris 目标表。
5. 分批读取 Oracle 数据。
6. 分批写入 Doris。
7. 记录每批结果。
8. 执行行数校验。
9. 更新运行状态。

## 14. 异常处理

异常类型：

- Oracle 连接失败
- Oracle SQL 语法错误
- Oracle 查询超时
- Doris 连接失败
- Doris 表不存在
- Doris 类型不兼容
- Doris Stream Load 失败
- 字段映射缺失
- 行数校验失败
- 用户手动停止

处理原则：

- 节点失败后，默认停止后续节点。
- 保存失败原因和原始错误。
- 支持从失败节点重跑。
- 支持下载错误明细，后续增强。

## 15. 审计与日志

必须记录：

- 谁创建任务
- 谁修改任务
- 谁执行任务
- 执行了什么 SQL
- 写入了哪个 Doris 表
- 写入多少行
- 是否失败
- 错误原因

SQL 日志策略：

- 保存原始 SQL 模板。
- 保存参数渲染后的 SQL 快照。
- 对敏感参数脱敏。

## 16. 性能与容量要求

首版目标：

- 支持百万级数据抽取写入。
- 支持分批执行，不一次性加载全部数据到内存。
- 支持任务级停止。
- 支持任务运行进度展示。

后续目标：

- 支持千万级以上数据分区并发写入。
- 支持断点续跑。
- 支持增量水位。

## 17. 与现有模块关系

### 17.1 数据连接

复用现有数据连接管理：

- Oracle 连接
- Doris 连接

### 17.2 调度中心

复用现有调度思想，但建议为 ETL 增加独立运行记录。

### 17.3 离线开发

编排画布应复用现有数据平台/离线开发的节点、版本、发布、调度能力。

### 17.4 离线任务目录树

- 左侧目录使用紧凑树形布局，每个文件夹或任务独占一行。
- 文件夹支持多级嵌套、展开收起和固定高度滚动。
- 任务单击打开，不展示“选择”“删除”等行内按钮。
- 新建任务、新建子文件夹、重命名、复制、移动和删除统一放入右键菜单。
- 空白区域右键用于在根目录创建任务或文件夹。
- 任务复制只复制最新设计为新开发版，不复制上线状态、调度状态和历史运行记录。
- 文件夹只允许在空目录状态删除，并禁止移动到自身或后代目录。

### 17.5 离线调度管理

- 离线任务上线并启用调度后，必须立即出现在调度管理列表，即使尚未产生任何运行实例。
- 调度管理聚合任务、目录、上线版本、调度配置、下次运行时间和最近运行结果。
- 状态区分等待调度、排队中、运行中、未启用和异常，避免把尚未到期的计划误认为已经进入执行队列。
- 支持立即运行、编辑、停用/启用、打开任务和查看运行日志。
- 调度计划停用后保留版本配置和历史实例，允许重新启用。

### 17.6 SM3 / SM4

ETL 结果可以作为 SM3 / SM4 后续处理输入。

示例流程：

```text
Oracle -> Doris ETL
-> 数据校验
-> SM4 自动加密
-> 授权给部门用户
```

## 18. 首版里程碑

### 阶段 1：SQL 工作台

- Doris SQL 执行
- Oracle SELECT 预览
- SQL 审计日志

### 阶段 2：Oracle -> Doris 单任务 ETL

- Oracle 查询预览
- 字段映射
- Doris 目标表配置
- 分批读取写入
- 行数校验
- 运行记录

### 阶段 3：多节点编排

- Doris SQL 节点
- Oracle -> Doris 节点
- 数据校验节点
- 串行执行
- 从失败节点重跑

### 阶段 4：调度与治理

- 定时调度
- 并发控制
- 失败重试
- 审计增强

## 19. 验收标准

首版验收建议：

1. 可以在页面配置 Oracle 查询 SQL 并预览字段和样例数据。
2. 可以选择 Doris 目标库表。
3. 可以自动生成字段映射。
4. 可以将 Oracle 查询结果写入 Doris。
5. 可以选择追加、清空后写入、重建后写入策略。
6. 可以看到任务进度、成功行数、失败原因。
7. 可以停止正在运行的任务。
8. 可以手动重跑失败任务。
9. 可以把多个节点组合为串行流程。
10. 可以配置定时调度。
11. 所有执行都有日志和审计。

## 20. 待确认问题

1. Oracle -> Doris 写入首版是否必须使用 Stream Load，还是可以先用 INSERT 小批量落地？
2. Doris 目标表是否允许系统自动创建？
3. 默认写入策略是 `truncate_insert` 还是 `drop_create_insert`？
4. 是否需要首版支持增量水位，例如按 `UPDATE_TIME` 或 ID 记录上次成功值？
5. 单任务最大数据量预期是多少：百万、千万还是更高？
6. 是否允许普通用户执行 Doris DDL？
7. 编排画布是否直接复用现有离线开发，还是新增独立 ETL 画布？
8. 调度失败后是否需要告警，例如页面通知、邮件、接口回调？
