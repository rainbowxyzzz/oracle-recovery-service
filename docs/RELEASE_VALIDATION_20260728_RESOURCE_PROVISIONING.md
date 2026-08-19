# 2026-07-28 数据空间批量开通模块发布验证

## 1. 验证范围

本次验证覆盖独立业务模块“数据空间批量开通”：

- Excel/CSV 上传、预览、逐行校验和生成名称编辑。
- Doris 用户创建、数据库创建、库级授权。
- 外部 `POST /api/dash/dataConnection/apiAdd` 注册。
- 批次并行、行内串行、步骤日志、失败重试和敏感字段脱敏。
- 独立 `resource-provisioning` Worker 与 `resource_provisioning` 队列。
- 共享 UI 和原有业务读取接口回归。

本次没有生成 Docker Run 完整部署包；测试 mock 只存在于 128 验证期间，验证完成后已经删除。

## 2. 源码与备份

开发前源码备份：

```text
artifacts/source-snapshots/resource-provisioning-prechange-20260728-155231.zip
SHA256: B5C48BDAE086B92EE81FF9E4BCA9335BE8B9F484D74579978F86E9AC523604F3
```

本地 128 发布材料：

```text
artifacts/128-releases/20260728-resource-provisioning-r1
artifacts/128-releases/20260728-resource-provisioning-r2
artifacts/128-releases/20260728-resource-provisioning-r3
```

## 3. 128 当前运行版本

| 容器 | 镜像 | 模式 / 队列 |
|---|---|---|
| `oracle-recovery-api` | `oracle-recovery-service-api:20260728-resource-provisioning-r3` | `APP_SERVICE_MODE=monolith` |
| `oracle-recovery-worker-resource-provisioning` | `oracle-recovery-service-worker-resource-provisioning:20260728-resource-provisioning-r2` | `resource-provisioning` / `resource_provisioning` |
| 原 6 个业务 Worker | `20260728-microservice-phase1-r1` 对应业务镜像 | 各自独立业务队列 |

API 使用 r3、独立 Worker 使用 r2 是有意的版本边界：r3 只修复前端连续编辑输入框时的重绘问题，没有修改 Worker 执行代码。

最终哈希核对确认：本地 `resource_provisioning.py` 服务、API 路由、Worker 任务与 128 API/Worker 完全一致；本地 `ui.html` 和新增前端回归用例与 r3 API 完全一致。r2 Worker 内的 `ui.html` 和前端回归用例仍是 r2 内容，但 Worker 不提供页面，且其业务执行文件与本地完全一致。

## 4. 自动化测试

| 检查 | 结果 |
|---|---|
| `python -m compileall` | 通过 |
| 本地完整测试 | `122 passed, 1 skipped` |
| 128 当前 r3 API 镜像完整测试 | `Ran 123 tests`，`OK (skipped=1)` |
| `test_resource_provisioning.py` | 通过 |
| `test_microservice_modes.py` | 通过 |
| API 健康检查 | HTTP 200，MySQL 连接成功 |
| 7 个 Worker `inspect ping` | 全部 `pong` |
| Worker 活跃队列 | 7 个 Worker 各自只消费一个业务队列 |

## 5. 真实业务闭环

在 128 使用隔离 Doris 资源执行 4 行、并行度 3 的完整任务：

1. 首次执行结果为 3 行成功、1 行外部注册失败。
2. 两个慢请求的时间区间发生重叠，证明行级并行生效。
3. 对失败批次执行重试后，4 行全部成功。
4. 重试只重新执行失败的 `register_connection`，没有重复创建用户、数据库和授权。
5. 4 个新 Doris 用户均可使用初始密码连接各自数据库并执行 `SELECT 1`。
6. 批次、行和步骤日志均可持续查询，SQL、请求摘要、响应摘要和耗时完整。

测试期间共记录 7 次 mock 请求。每次请求均包含：

```text
name, projectId, type, paths, server, port, userName, password, token,
defaultSchemaName, skipTest, parameters
```

固定参数验证结果：

- `type = 124`
- `skipTest = false`，JSON 类型为布尔 `bool`
- `authType = ldap`
- `dorisCatalog = internal`
- `totalQueueLength = 40`
- `highQueueLength = 1`
- `nullSafeEqual = false`
- `driver = mysql-connector-5.1.49`
- API 返回、系统日志和归档请求日志中的密码、Token 均为 `******`

脱敏 mock 请求日志已归档到 128：

```text
/opt/oracle-recovery/releases/20260728-resource-provisioning-r3/apiadd-requests-redacted.jsonl
SHA256: 2d9dca552e534997d8c2bc9946d9ae8bb500f163a675bc32546fff9b919c5f41
```

## 6. 可见 Chrome 验收

主流程已完成登录、模块导航、刷新、上传 `.xlsx`、识别预览、连续编辑用户名和数据库名、提交任务、查看行节点日志和请求参数。

| 窗口状态 | Outer | Inner | 页面横向溢出 | Console / Page Error |
|---|---:|---:|---|---|
| 最大化 | `1920x1032` | `1920x945` | 无 | 无 |
| 左半屏 | `960x1032` | `944x937` | 无 | 无 |
| 右半屏 | `960x1032` | `944x937` | 无 | 无 |
| 还原窗口 | `1280x850` | `1264x755` | 无 | 无 |

截图归档：

```text
artifacts/ui-audit/20260728-resource-provisioning-r3
```

另外逐个点击了 14 个当前可见业务模块导航，均切换到对应活动视图，无控制台错误或页面错误。

## 7. 原有能力回归

以下读取接口均返回 HTTP 200：

```text
/api/v1/auth/me
/api/v1/database-connections
/api/v1/resource-provisioning/batches
/api/v1/data-platform/dashboard
/api/v1/data-platform/nodes
/api/v1/data-platform/workflows
/api/v1/data-platform/schedules
/api/v1/doris-encryption/batches
/api/v1/doris-encryption/task-definitions
/api/v1/doris-sm3/tasks
/api/v1/tasks
```

原 6 个业务 Worker 和新增 Worker 日志均显示正确 `service_mode`、业务队列和 `ready` 状态；验证结束前 10 分钟无 `ERROR`、`Traceback` 或 `CRITICAL`。

## 8. 验证中发现并修复的问题

1. r1 在 Doris 参数化 SQL 中同时使用 `%s` 和用户主机 `'%'`，PyMySQL 会把 `'%'` 误当作格式字符。
   - r2 将参数化 SQL 内的主机写为 `'%%'`，实际发送给 Doris 仍为 `'%'`。
2. r2 页面连续编辑用户名和数据库名时，`change` 事件重绘整行，导致第二个输入值丢失。
   - r3 改为只刷新校验单元格，不替换正在编辑的输入框。

## 9. 测试资源清理

验证完成后已执行并复核：

- 删除 6 个 `codexrp20260728*` Doris 测试用户。
- 删除 6 个隔离 Doris 测试数据库。
- 删除 7 个测试批次及对应行、步骤日志，复核剩余测试行数为 0。
- 删除 `resource-provisioning-apiadd-mock` 容器。
- 删除 128 上的 mock 专用脚本目录。
- API 健康正常，7 个 Worker 全部 `pong`。

清理脚本：

```text
artifacts/128-releases/20260728-resource-provisioning-r3/cleanup-resource-provisioning-test-data-128.sh
```

## 10. 已知边界与风险

- 用户未提供可访问的真实 `apiAdd` 地址，本次外部接口只完成契约级 mock 验证；上线前仍需在目标内网使用真实接口做一次验收。
- `apiAdd` 当前按 HTTP 地址设计，真实环境应优先使用 HTTPS，避免密码和 Token 在网络中明文传输。
- 128 系统元数据库容器是历史 `mysql:latest`，不代表交付包规则；后续完整打包仍必须固定为 `mysql:8.4` 并保留资源调优参数。
- 本次未打包，测试 mock、测试 URL 和测试数据均不会进入后续交付包。

## 11. r4 有数 Token 自动管理发布验证

### 11.1 变更范围

2026-07-28 在 r3 基础上发布 `20260728-resource-provisioning-r4`：

- 页面取消手工 Token 输入，改为填写有数登录账号和密码。
- 独立 Worker 调用同域 `POST /api/dash/util/genToken` 获取 `youdata_token`。
- Token 仅保存在 Worker 进程内存，按 Token URL 和登录账号隔离，并发首次登录和刷新均执行单飞控制。
- 仅 HTTP `401/403` 或明确的登录失效业务响应触发一次刷新和当前 `apiAdd` 重试；HTTP 500 不刷新。
- 有数密码加密落库，Token 不落库、不进入 API 响应或业务日志。
- 历史 `api_token_enc` 批次继续按手工 Token 方式执行。

开发前备份：

```text
artifacts/source-snapshots/resource-provisioning-youdata-token-prechange-20260728-205520.zip
SHA256: B806E175186ED7217C261C7449C9B9D2C247BF58C4EA911590BF52B89CC5698E
```

r4 源码归档：

```text
artifacts/128-releases/20260728-resource-provisioning-r4/resource-provisioning-source-20260728-resource-provisioning-r4.tar.gz
SHA256: A31478141FEF11E553D8A3A9B4DF93EFFDA79B40D4EFEB00F06A2DDFAC4247A6
```

### 11.2 发布与数据库迁移

128 当前运行版本：

| 容器 | 镜像 | 模式 / 队列 |
|---|---|---|
| `oracle-recovery-api` | `oracle-recovery-service-api:20260728-resource-provisioning-r4` | `APP_SERVICE_MODE=monolith` |
| `oracle-recovery-worker-resource-provisioning` | `oracle-recovery-service-worker-resource-provisioning:20260728-resource-provisioning-r4` | `resource-provisioning` / `resource_provisioning` |

发布前确认资源开通 Redis 队列为 0，7 个 Celery Worker 均无 active、reserved 或 scheduled 任务，并生成 13MB 的 MySQL 备份：

```text
/opt/oracle-recovery/releases/20260728-resource-provisioning-r4/oracle_recovery_before_20260728-resource-provisioning-r4_20260728-211157.sql
```

`init_db()` 连续运行两次均成功，`resource_provisioning_batches` 已幂等增加：

```text
youdata_login_name
youdata_password_enc
youdata_token_url
```

迁移临时进程退出时出现 `aiomysql` 在事件循环关闭后执行连接析构的非阻断告警；两次迁移均返回 0，字段完整，API 启动和 MySQL 健康检查正常。旧 r3 API 和 r2 独立 Worker 容器已停止保留，可用于发布回滚。

### 11.3 自动化与真实链路

候选镜像验证结果：

- `test_resource_provisioning.py`：API、Worker 各 21 项通过。
- `test_microservice_modes.py`：API、Worker 各 8 项通过。
- 完整测试：`Ran 133 tests`，`OK (skipped=1)`。
- 已部署 UI 静态契约通过：存在有数账号、密码字段及对应请求参数，不存在旧手工 Token 输入框和请求字段。
- 按用户要求，本次 r4 没有调用 Chrome；页面验证仅使用已部署 HTML/JavaScript 契约和真实 API 闭环。

128 临时 mock 同时实现 `genToken` 和 `apiAdd`，真实 API、MySQL、Redis、独立 Worker、Doris 完成以下验证：

1. 首批 4 行并行任务只调用一次 `genToken`，4 个 `apiAdd` 请求复用同一个内存 Token。
2. 后续批次继续复用 Worker 内存 Token，登录次数不增加。
3. HTTP 401 后只刷新一次并只重试当前 `apiAdd`。
4. 业务返回“请登录”后只刷新一次并只重试当前 `apiAdd`。
5. HTTP 500 任务失败但不刷新 Token。
6. Worker 重启后内存 Token 丢失，下一个任务自动重新登录。
7. 所有 `apiAdd` 请求的 `skipTest` 均为 JSON 布尔值 `false`。
8. 有数密码均为密文，自动 Token 没有写入 `api_token_enc`；API、系统日志和测试证据中均未出现密码或 Token 明文。
9. 历史手工 Token 批次执行成功，mock 登录次数保持为 4，证明该路径未误调用 `genToken`。

真实链路证据：

```text
artifacts/128-releases/20260728-resource-provisioning-r4/resource-provisioning-r4-e2e-result.json
SHA256: 079E48439B21BAE44267C8C04E8E40AADF222AF0085122ED8D4992AC0095AECA

artifacts/128-releases/20260728-resource-provisioning-r4/resource-provisioning-r4-legacy-result.json
SHA256: D24EC64DF3B6A50B78201F13B4DE0422CADED62A3331B11E32337B1BF09C7CDA

artifacts/128-releases/20260728-resource-provisioning-r4/youdata-token-mock-r4-stats.json
SHA256: AC190F02AD6741D4547BC258B88879B4E1FD46D3C5174AA41244E24454D517FF
```

### 11.4 最终回归与清理

- API 健康检查返回 HTTP 200，MySQL 连接成功。
- 7 个业务 Worker 全部 `pong`；资源开通队列为 0。
- `auth/me`、数据源、资源开通、数据平台、调度、SM3、SM4 和恢复任务等 11 个读取接口均返回 HTTP 200。
- r4 API 与独立 Worker 最近 300 行日志无 `ERROR`、`Traceback` 或 `CRITICAL`。
- 已删除 10 个隔离 Doris 用户和 10 个隔离数据库。
- 已删除自动 Token 与历史 Token 测试批次、行和步骤日志，复核 mock URL 关联批次数为 0。
- 已删除 `resource-provisioning-youdata-mock` 容器；mock 仅存在于 128 测试期间，不进入源码镜像或完整部署包。

清理脚本：

```text
artifacts/128-releases/20260728-resource-provisioning-r4/cleanup-resource-provisioning-r4-test-data-128.sh
SHA256: D6DAD04F584242AF6E65A9482439CC82BD2CCCDC4EED58DB7227F4268C32C047
```

本次按“修改后优先发布 128、用户明确说打包才生成完整包”的规则执行，没有生成 Docker Run 完整部署包。

## 12. 数据连接授权二级子应用发布验证

### 12.1 变更范围

2026-07-29 在数据空间批量开通模块内新增独立二级子应用“数据连接授权”，版本为 `20260729-resource-data-permissions-r1`：

- 用户手动选择已成功的开通批次后创建独立授权批次，不自动接在 `apiAdd` 后。
- 按页面选择的 Doris 连接和可配置资源表查询 `name`、`id`，默认读取 `TESTS.data_connection`；系统不创建、不更新该资源表。
- 资源名称必须唯一匹配且资源 ID 必须为正整数，否则当前行以明确状态和步骤日志结束。
- 取得资源 ID 后调用 `POST /api/dash/role/importDataPermissions`，复用 r4 的有数账号密码、内存 Token、失效刷新和敏感信息脱敏能力。
- 授权批次、授权行和步骤日志独立保存；授权结果不改变原开通批次状态。
- 失败行支持手工重试，已成功步骤不重复执行；资源冲突修正后只重新查询资源并继续授权。

开发前源码备份：

```text
artifacts/source-snapshots/resource-data-permissions-prechange-20260729-114535.zip
SHA256: 157FBA41278BABA3765FD8A469D5AED347785033C0EEE77A03153CD5569577EC
```

发布源码归档：

```text
artifacts/128-releases/20260729-resource-data-permissions-r1/resource-data-permissions-source-20260729-resource-data-permissions-r1.tar.gz
SHA256: 3BBD041746DA9E7903443621870728D20A6C1D2000840E1166D5845E4F5A4379
```

### 12.2 128 发布与迁移

128 当前切换的容器如下，其余 6 个业务 Worker 保持原版本：

| 容器 | 镜像 | 镜像 ID |
|---|---|---|
| `oracle-recovery-api` | `oracle-recovery-service-api:20260729-resource-data-permissions-r1` | `sha256:1d5636953f57f432ba82e25bccd4b2f44e8e755fe2a93d1aee62cccc08697f48` |
| `oracle-recovery-worker-resource-provisioning` | `oracle-recovery-service-worker-resource-provisioning:20260729-resource-data-permissions-r1` | `sha256:90d33e7f61e5c9f0447b658fad9edcc0337cf8c2c4689a654db90683a6ab9099` |

发布前确认无在途资源开通任务，并生成系统 MySQL 备份：

```text
/opt/oracle-recovery/releases/20260729-resource-data-permissions-r1/oracle_recovery_before_20260729-resource-data-permissions-r1_20260729-123927.sql
大小: 12674418 bytes
```

`init_db()` 连续执行两次均成功，以下三张表存在：

```text
resource_permission_batches
resource_permission_rows
resource_permission_step_logs
```

迁移临时进程退出时仍有既有的 `aiomysql Event loop is closed` 析构告警，但两次迁移返回码均为 0，表结构完整，API、MySQL 和 Worker 启动正常。

回滚容器保留为：

```text
oracle-recovery-api-pre-20260729-resource-data-permissions-r1-20260729-123927
oracle-recovery-worker-resource-provisioning-pre-20260729-resource-data-permissions-r1-20260729-123927
```

### 12.3 自动化验证

- 本地完整测试结果为 `150 passed, 1 skipped`。
- 候选镜像内完整 `unittest` 结果为 `Ran 151 tests`、`OK (skipped=1)`。
- Python `compileall`、内嵌 JavaScript 语法、静态 DOM 引用、UI 事件契约、API 路由和三张模型表契约均通过。
- 页面事件闭环覆盖二级子应用切换、来源批次回填、手工提交授权、批次列表、行与步骤日志、失败行重试和顶部统一刷新。

### 12.4 128 真实链路

使用 4 行隔离数据验证：

| 场景 | 首次结果 | 重试结果 |
|---|---|---|
| 正常授权 | 成功 | 无需重试 |
| Token 失效 | 刷新 Token 后成功 | 无需重试 |
| 外部接口 HTTP 500 | 失败，未刷新 Token | 复用已查询资源 ID 后成功 |
| 同名资源存在两条记录 | `conflict` | 修正资源数据后重新查询并成功 |

首次授权汇总为成功 2 行、失败 2 行；修正测试条件并点击失败行重试后，最终成功 4 行、失败 0 行。已成功行未重复执行，原开通批次成功后也没有自动生成授权批次。

mock 总计登录 2 次，证明仅 Token 失效场景触发刷新；HTTP 500 未触发刷新。所有归档请求中的密码和 Token 均已脱敏。

真实链路证据：

```text
artifacts/128-releases/20260729-resource-data-permissions-r1/resource-data-permissions-e2e-result.json
SHA256: 200D4C8FD9525B6EBB04E7D7655ADD5A24B20619506A1304EA5EBAC0B31DDC87

artifacts/128-releases/20260729-resource-data-permissions-r1/resource-permission-mock-stats.json
SHA256: 0186857F0EDC7FF1E81A5574BD0B16E6ED7695FECFE5CACBD7AFC3D05FC9F31D
```

### 12.5 清理与最终审计

- 已删除 4 个隔离 Doris 用户、4 个隔离数据库和临时 `TESTS` 数据库。
- 已删除原开通测试批次以及授权批次、授权行和步骤日志；最终测试残留计数均为 0。
- 已删除测试 mock 容器，mock 地址和测试实现未写入产品源码镜像。
- API 健康检查返回 HTTP 200，MySQL 连接成功；12 个只读回归接口均返回 HTTP 200。
- 7 个业务 Worker 全部 `pong`，`resource_provisioning` 队列为 0。
- API 和资源开通 Worker 最近 300 行日志无 `ERROR`、`Traceback` 或 `CRITICAL`。

最终审计证据：

```text
artifacts/128-releases/20260729-resource-data-permissions-r1/final-health.json
SHA256: 270CA7AE27383C9CA222184F7D2AFF53FD2FCAAEDB6B62A3BBCA3B4175F1FAD5

artifacts/128-releases/20260729-resource-data-permissions-r1/final-containers.txt
SHA256: 597505C9B281DA1DFE28E345AF7D1F9D0ABAF5BEC25AA35B7D8953B1792DEF8B

artifacts/128-releases/20260729-resource-data-permissions-r1/final-worker-ping.json
SHA256: C5895B7414AEDC8C958D830CADFEAEAB6E8EF44C45D37182EDF4872D787B4247

artifacts/128-releases/20260729-resource-data-permissions-r1/final-database-audit.txt
SHA256: 86DBFC5E3E3F3452BCC1F00D2068F67822615CE82351822DC02E0AB37C9D1EE3
```

按用户此前要求，本次没有调用 Chrome；页面验证采用静态 HTML/JavaScript 契约和 128 真实 API/Worker 链路。本次仅热更新发布到 128，没有生成新的 Docker Run 完整部署包。
