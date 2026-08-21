# 2026-08-21 全链路数据自动化流水线发布验证记录

## 1. 发布范围

- 发布模式：模式二（发布到 `192.168.150.128` 并完成真实链路验证）。
- 功能范围：Oracle DMP 目录发现与批次、自动恢复、Oracle 直连 Stream Load、Doris 原始层、离线标准化、四层数据资产、字段级血缘、自动分类分级、血缘反推 SM4 加密、批次时间线与管理页面。
- 兼容边界：旧恢复任务、数据同步节点、离线流程、调度、密钥版本和 SM4 任务定义不回写；自动监听默认关闭。

## 2. 发布前保护

- 确认 Celery 无在途和保留任务后执行发布。
- 对长期停留且没有节点运行记录的历史孤儿工作流运行完成备份后标记失败，未终止真实执行中的任务。
- 系统库备份：`/opt/oracle-recovery/releases/20260820-data-automation-pipeline-r1-20260821-094512/oracle_recovery_before.sql`。
- 发布中发现旧 Worker 镜像缺少部分新版依赖后，从发布前逐容器备份恢复完整旧源码，仅向受影响 Worker 增量发布必需文件。

## 3. 关键实现结论

- 自动恢复后的新 Oracle 用户不再依赖 Doris JDBC Catalog 元数据枚举；数据同步 Worker 使用恢复任务冻结的 Oracle 连接快照和加密口令直连恢复用户，通过 Stream Load 写入 Doris。
- 失败断点继续会创建新的恢复/同步/标准化运行 ID，不复用已失败运行记录。
- 原始层资产字段使用 `source_columns` 固化，字段血缘使用 `column_mappings` 固化，避免字段合同与映射结构混用。
- SM4 成功后登记 `secured` 资产，并为加密字段记录 `SM4_ENCRYPT` 表达式血缘；血缘证据仅保存密钥指纹，不保存密钥种子。
- 离线开发 Worker 使用其旧镜像的精简 Celery 任务清单，仅新增 `data_platform.workflow_run`，未引入资源授权等无关模块依赖。

## 4. 隔离 DMP 测试数据

- 文件：`/data/oracle-recovery/oracle19c/dmp/codex_data_auto_20260820.dmp`
- 大小：348160 字节。
- SHA256：`ccfb434e743d5b516bdf50672fa6168eb102aeaf4202bda9e47fcf5ce63deff4`
- 内容：`CUSTOMER_SOURCE`，6 个字段、3 行中文隔离测试数据；包含 `PHONE`、`ID_CARD` 两个敏感字段。
- 自动监听保持关闭，测试流水线不会继续扫描或处理生产目录。

## 5. 端到端结果

- 流水线：`ee97f11d-a591-4dad-aba5-f654e034d934`
- 数据批次：`09103410-ff68-43f7-b942-e0fec800a0db`，最终状态 `completed`。
- Oracle 恢复：`bc71c37a-9289-4256-b2ee-63b687972878`，实际 Schema `CODEX_DATA_AUTO_20260820_26082`，校验 1 张表、4 个对象、3 行。
- 原始层同步：`96915577-156a-4355-894c-02ab5d1db4ea`，成功 1 张表、写入 3 行。
- 标准化运行：`009d87e7-299e-4d1a-ac4a-68081cad8d1e`，成功 1 个节点。
- SM4 批次：`227f678b-5c1d-4649-9133-a174e70c1911`，成功 1 张表。
- 行数：`CUSTOMER_RAW=3`、`CUSTOMER_STANDARD=3`、`CUSTOMER_RAW_SECURED=3`。
- 加密核对：`PHONE` 3/3 已变化，`ID_CARD` 3/3 已变化；`NAME`、`REGION_CODE` 等非敏感字段 3/3 保持一致。
- 资产：`restored/raw/standard/secured` 共 4 个。
- 血缘：20 条，其中字段级 18 条，`SM4_ENCRYPT` 字段血缘 2 条。
- 临时 Catalog 残留：0。

## 6. 回归与运行健康

- 本地测试：`231 passed, 1 skipped, 32 subtests passed`。
- 128 API、Oracle Worker、数据同步 Worker、离线开发 Worker、SM4 Worker 均为 `running`，无 OOM，最终检查重启计数为 0。
- API 健康检查通过；最终端到端验收脚本：`artifacts/128-releases/20260820-data-automation-pipeline-r1/verify-data-automation-e2e-128.sh`。
- 含隔离测试明文口令的本地和远端临时初始化脚本已删除；DMP 和已完成批次保留用于页面核验。

## 7. 已验证回滚镜像

- `oracle-recovery-service-api:20260821-data-automation-pipeline-r2`：`sha256:d46e101088f04d741cc524ce83c4ab3d589bc6eecb4df761f9c8aaeaddc96807`
- `oracle-recovery-service-worker-data-sync:20260821-data-automation-pipeline-r2`：`sha256:38de7f07b7bb19ad6edc25592d3fed206b2a54a12bd4ebc9f047bfac023f35d8`
- `oracle-recovery-service-worker-data-platform:20260821-data-automation-pipeline-r2`：`sha256:82bbeaabdd4835735114ddb76c13501b4a243f92ed3c46f2620424e9e84e3c72`
