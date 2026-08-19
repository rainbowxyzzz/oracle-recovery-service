# Oracle 19c 代替 Oracle 11g 导出与现有系统导入兼容性测试报告

- 测试日期：2026-07-14
- 现有系统：`http://192.168.150.128:8000`
- 数据库：Oracle Database 19c Standard Edition 2 19.3.0.0.0
- 字符集：`ZHS16GBK`，NCHAR 字符集：`AL16UTF16`
- 兼容口径：在 Oracle 19c 工具上使用 Oracle 11g 可用参数；这不是 Oracle 11g 二进制实机认证。
- 源数据：`0.995 GiB`，两个 schema、两个表空间、65 万业务行及 LOB/索引/程序对象。
- 导出日志：29/29 用例均未生成或保留导出日志。`expdp` 使用 `NOLOGFILE=YES`，`exp` 不设置 `LOG`。

## 1. 执行结论

共执行 29 个独立导出用例：`PASS=10`、`PASS_WITH_WARNINGS=4`、`FAIL_IMPORT=3`、`FAIL_VALIDATION=6`、`EXPORT_BLOCKED=6`。

核心结论：Data Pump 的默认/schema/table/FULL过滤/metadata/query/sample/flashback/`VERSION=11.2`/metadata compression 可以使用；传统 `exp` 不能视为可靠兼容能力，即使系统任务显示成功，也可能没有目标 schema、遗漏多 OWNER 或遗漏 SecureFile LOB。

## 2. 业务流程

```mermaid
flowchart LR
  A[0.995 GiB 基准数据] --> B[独立真实导出]
  B --> C[检查无导出日志]
  C --> D[embedded-oracle API]
  D --> E[系统探测与计划]
  E --> F[系统 impdp 或 imp]
  F --> G[行数与对象校验]
  G --> H[系统清理与最终审计]
```

## 3. 测试矩阵

| ID | 导出方式 | DMP MiB | 系统任务 | 校验 | 结论 | 关键证据 |
|---|---|---:|---|---|---|---|
| DP00_DEFAULT_SCHEMA | Default schema mode as the connected schema owner | 16.29 | succeeded | passed | PASS | Export, system import and data validation completed. |
| DP01_SCHEMA_SINGLE | SCHEMAS single schema, single dump file | 16.31 | succeeded | passed | PASS | Export, system import and data validation completed. |
| DP02_PARALLEL_SE2 | SCHEMAS multiple schemas with PARALLEL on Standard Edition 2 | 0.00 | 未创建 | 未执行 | EXPORT_BLOCKED | ORA-39002: invalid operation \| ORA-39094: Parallel execution not supported in this database edition. |
| DP02B_SCHEMA_MULTI_U | SCHEMAS multiple schemas with %U and FILESIZE, without PARALLEL | 693.39 | succeeded_with_warnings | passed | PASS_WITH_WARNINGS | [warn] impdp returned a non-zero code, but copied logs only contain ORA-39082 compilation warnings. |
| DP03_TABLE_SINGLE | TABLES single table | 406.71 | succeeded | passed | PASS | [retry] SCHEMAS import selected no objects (ORA-39039/ORA-31655); retrying once with FULL=Y. |
| DP04_TABLE_MULTI | TABLES multiple tables across schemas | 286.58 | succeeded | passed | PASS | [retry] SCHEMAS import selected no objects (ORA-39039/ORA-31655); retrying once with FULL=Y. |
| DP05_TABLESPACES | TABLESPACES mode | 693.18 | failed | not_run | FAIL_IMPORT | Import retry with FULL=Y failed with exit code 5. Check /opt/oracle-recovery-service-package/oracle-auto-import-runs/task_ddf7191177ba4006bdac70a5/import. |
| DP06_FULL_FILTERED | FULL=Y with INCLUDE=SCHEMA safety filter | 693.59 | succeeded_with_warnings | passed | PASS_WITH_WARNINGS | [warn] impdp returned a non-zero code, but copied logs only contain ORA-39082 compilation warnings. |
| DP07_METADATA_ONLY | CONTENT=METADATA_ONLY | 0.44 | succeeded_with_warnings | passed | PASS_WITH_WARNINGS | [warn] impdp returned a non-zero code, but copied logs only contain ORA-39082 compilation warnings. |
| DP08_DATA_ONLY | CONTENT=DATA_ONLY | 16.23 | failed | not_run | FAIL_IMPORT | unknown dump type |
| DP09_QUERY | TABLES with QUERY row filter | 5.98 | succeeded | passed | PASS | [retry] SCHEMAS import selected no objects (ORA-39039/ORA-31655); retrying once with FULL=Y. |
| DP10_SAMPLE | TABLES with SAMPLE=10 | 1.75 | succeeded | passed | PASS | [retry] SCHEMAS import selected no objects (ORA-39039/ORA-31655); retrying once with FULL=Y. |
| DP11_EXCLUDE_TABLE | SCHEMAS with EXCLUDE=TABLE filter | 270.81 | succeeded_with_warnings | passed | PASS_WITH_WARNINGS | [warn] impdp returned a non-zero code, but copied logs only contain ORA-39082 compilation warnings. |
| DP12_FLASHBACK_SCN | SCHEMAS with FLASHBACK_SCN | 16.31 | succeeded | passed | PASS | Export, system import and data validation completed. |
| DP13_FLASHBACK_TIME | SCHEMAS with FLASHBACK_TIME=SYSTIMESTAMP | 16.31 | succeeded | passed | PASS | Export, system import and data validation completed. |
| DP14_VERSION_11_2 | VERSION=11.2 compatible dump format | 16.26 | succeeded | passed | PASS | Export, system import and data validation completed. |
| DP15_COMPRESSION_METADATA | COMPRESSION=METADATA_ONLY | 16.31 | succeeded | passed | PASS | Export, system import and data validation completed. |
| DP16_TRANSPORT_TS | TRANSPORT_TABLESPACES dump without transport datafile input in the current system | 0.00 | 未创建 | 未执行 | EXPORT_BLOCKED | ORA-39123: Data Pump transportable tablespace job aborted \| ORA-00439: feature not enabled: Export transportable tablespaces |
| DP17_COMPRESSION_ALL | COMPRESSION=ALL on Standard Edition 2 | 0.00 | 未创建 | 未执行 | EXPORT_BLOCKED | ORA-39002: invalid operation \| ORA-00439: feature not enabled: Dump File Data Compression |
| DP18_PASSWORD_ENCRYPTION | Password-encrypted Data Pump dump | 0.00 | 未创建 | 未执行 | EXPORT_BLOCKED | ORA-39002: invalid operation \| ORA-00439: feature not enabled: Dump File Encryption |
| DP19_NETWORK_LINK | NETWORK_LINK export through a temporary loopback database link | 0.00 | 未创建 | 未执行 | EXPORT_BLOCKED | ORA-31631: privileges are required \| ORA-39149: cannot link privileged user to non-privileged user |
| DP19B_NETWORK_LINK_OWNER | NETWORK_LINK export as a non-privileged schema owner | 16.96 | failed | not_run | FAIL_IMPORT | Import command failed with exit code 1. Check /opt/oracle-recovery-service-package/oracle-auto-import-runs/task_adbc61fee5684c5da5be659d/import. |
| EXP00_DEFAULT_SCHEMA | Legacy exp default schema mode as the connected owner | 16.02 | succeeded | failed | FAIL_VALIDATION | No target schema mapping for C11_SRC_B |
| EXP01_OWNER_SINGLE | Legacy exp OWNER single schema with CONSISTENT=Y | 16.02 | succeeded | failed | FAIL_VALIDATION | No target schema mapping for C11_SRC_B |
| EXP02_OWNER_MULTI | Legacy exp OWNER multiple schemas | 698.20 | succeeded_with_warnings | failed | FAIL_VALIDATION | C11_SRC_A.LOB_CASES: expected 20000, got None; No target schema mapping for C11_SRC_B |
| EXP03_TABLES | Legacy exp TABLES mode | 190.51 | succeeded | failed | FAIL_VALIDATION | No target schema mapping for C11_SRC_A; No target schema mapping for C11_SRC_B |
| EXP04_ROWS_N | Legacy exp ROWS=N metadata only | 0.02 | succeeded_with_warnings | failed | FAIL_VALIDATION | C11_SRC_A.LOB_CASES: expected 0, got None |
| EXP05_DIRECT_Y | Legacy exp direct path | 15.93 | succeeded | failed | FAIL_VALIDATION | No target schema mapping for C11_SRC_B |
| EXP06_FULL | Legacy exp FULL=Y | 0.02 | 未创建 | 未执行 | EXPORT_BLOCKED | EXP-00058: Password Verify Function for ORA_STIG_PROFILE profile does not exist \| EXP-00000: Export terminated unsuccessfully |

## 4. Data Pump 结论

1. `SCHEMAS` 单/多 schema 均可用；多文件 `%U + FILESIZE` 可用。SE2 不支持 `PARALLEL=2`，返回 `ORA-39094`。
2. `TABLES` 单表和跨 schema 多表可以导入，但系统先按 `SCHEMAS` 执行，收到 `ORA-39039/ORA-31655` 后依赖一次 `FULL=Y` 回退。
3. `TABLESPACES` 导出成功，但当前系统最终将返回码 5 判为失败，不能列为生效。
4. `FULL=Y + INCLUDE=SCHEMA` 可恢复测试 schema；未执行无过滤 FULL，避免带出恢复库中无关现存 schema。
5. `CONTENT=METADATA_ONLY` 可用；`CONTENT=DATA_ONLY` 探测不到 schema，最终为 `unknown dump type`。
6. `QUERY` 精确恢复 5000 行；`SAMPLE=10` 落入预期范围；`EXCLUDE=TABLE` 的排除结果正确。
7. `FLASHBACK_SCN`、`FLASHBACK_TIME`、`VERSION=11.2`、`COMPRESSION=METADATA_ONLY` 均通过。
8. 非特权 NETWORK_LINK 能导出，但探测器把 `CONNECT TO` 中的 `TO` 误识别为 schema，导入报 `ORA-39165`。
9. SE2 拒绝 Data Pump 并行、分区、传输表空间、数据压缩和 Dump File Encryption；这些是 Edition/许可限制，不是 11g 参数语法错误。

## 5. 传统 exp 结论

1. 默认 schema、OWNER单 schema、TABLES、DIRECT=Y 均可能显示任务成功，但 `schema_map={}`，没有隔离目标 schema，数据校验失败。
2. 多 OWNER 只解析出 `C11_SRC_A`，遗漏 `C11_SRC_B`，并遗漏 `LOB_CASES`，属于部分恢复但任务仍显示 `succeeded_with_warnings`。
3. `ROWS=N` 可以恢复普通表结构，但 SecureFile LOB 表缺失。
4. FULL 原始导出在 19c 上因 `EXP-00058: Password Verify Function for ORA_STIG_PROFILE profile does not exist` 终止，没有可导入 DMP。
5. 结论：当前系统不能把传统 `exp` dump 的任务状态直接当成恢复成功，必须以 schema映射和数据级校验作为最终结果。

## 6. 发现的问题

| 优先级 | 问题 | 影响 |
|---|---|---|
| P0 | legacy dump 在 `schema_map={}` 时仍执行 FULL import 并返回成功 | 可能导入原 schema，破坏恢复隔离性，且任务状态失真 |
| P0 | legacy 多 OWNER 只解析一个 schema | 静默漏恢复 schema 和数据 |
| P1 | NETWORK_LINK 元数据把 `TO` 识别成 schema | 生成错误 `SCHEMAS=TO,...` 并导致 `ORA-39165` |
| P1 | TABLESPACES 回退后编译告警返回码 5被判失败 | 实际对象可能已导入，但系统状态失败 |
| P1 | DATA_ONLY 无 schema 元数据时停止为 unknown | 不能恢复合法 DATA_ONLY dump |
| P1 | 无目标用户的失败/伪成功任务会留下 10 GiB空表空间 | 快速耗尽磁盘 |
| P1 | 自动导入目录保留每次 DMP副本 | 本轮曾累计约 3.9 GiB，需测试后人工回收 |
| P2 | direct 模式要求 host path 与容器挂载源完全一致 | 不能直接选择挂载根目录下的隔离子目录 |

## 7. 完整导出方式目录

### expdp 基础模式

- 默认 schema模式：不指定 FULL/SCHEMAS/TABLES/TABLESPACES。
- `SCHEMAS`：单 schema、多 schema。
- `TABLES`：单表、多表、跨 schema、分区表或指定分区。
- `TABLESPACES`：逻辑表空间模式。
- `FULL=Y`：全库模式，可配 INCLUDE/EXCLUDE 限定对象。
- `TRANSPORT_TABLESPACES`：传输表空间，需要 datafile 和相应 Edition。
- `NETWORK_LINK`：通过 DB Link读取远端源库并在本地生成 dump。

### expdp 内容与修饰参数

- 内容：`CONTENT=ALL|DATA_ONLY|METADATA_ONLY`。
- 对象/数据过滤：`INCLUDE`、`EXCLUDE`、`QUERY`、`SAMPLE`。
- 一致性：`FLASHBACK_SCN`、`FLASHBACK_TIME`。
- 文件布局：单文件、`%U` 多文件、`FILESIZE`、`PARALLEL`。
- 兼容：`VERSION=11.2` 等。
- 压缩/加密：`COMPRESSION`、`ENCRYPTION`、`ENCRYPTION_PASSWORD`，受 Edition/许可约束。
- 调用方式：命令行或 `PARFILE`；本测试全部以 parfile执行并禁用导出日志。

### 传统 exp 模式

- 默认当前用户、`OWNER` 单/多用户、`TABLES`、`FULL=Y`。
- 内容/路径：`ROWS=N`、`DIRECT=Y`、`CONSISTENT=Y`。
- 其他 BUFFER/RECORDLENGTH/GRANTS/INDEXES/TRIGGERS 等参数不会形成新的 dump格式，需按业务需要组合。

`ESTIMATE_ONLY`、`HELP`、`STATUS`、`JOB_NAME`、`METRICS` 等不生成新的可导入格式，未伪造为独立导入用例。

## 8. 日志与清理证明

- 29个实际导出用例全部 `no_export_logs=true`。
- 导出根目录最终没有 `dp*.dmp`、`exp*.dmp`、对应 par 或 log。
- 最终无 `DP*`/`EXP*` 测试目标用户、无 `TBS_DP*`/`TBS_EXP*` 表空间、无测试 DB Link、无活动任务。
- 源数据最终仍为 `0.995 GiB`，核心表行数和聚合值完全不变，源对象全部 VALID，源表空间 ONLINE。
- 远端磁盘最终恢复到约 `28 GiB` 可用。

## 9. 建议

1. 正式支持范围优先限定为 Data Pump；传统 exp 默认进入强制人工确认和数据级校验，不允许只凭任务状态成功。
2. 当 legacy 探测不到 schema 时必须停止，禁止自动生成 `FULL=Y IGNORE=Y`。
3. 修正 schema解析器，避免把 `CONNECT TO` 等关键字中的 `TO` 当成 schema。
4. DATA_ONLY 支持显式提供源 schema和目标 schema；没有人工输入时停止并给出明确提示。
5. TABLE/TABLESPACE 模式应根据 dump master table识别原始 export mode，减少依赖失败回退。
6. 任务完成后提供受控的 DMP副本和空表空间清理策略，避免 10 GiB级资源泄漏。

## 10. 证据文件

- 结构化结果：`docs\test-reports\oracle19c_11g_syntax_export_import_results_20260714.json`
- 用例矩阵：`docs\test-reports\oracle19c_11g_syntax_export_import_matrix_20260714.csv`
- 原始采证：`tmp\oracle_export_import_matrix_20260714\evidence\results.json`
- PDF报告：`output\pdf\oracle19c_11g_syntax_export_import_test_report_20260714.pdf`
