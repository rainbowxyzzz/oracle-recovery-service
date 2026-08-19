# 数据空间批量开通模块 PRD

## 1. 背景与目标

业务人员需要通过 Excel 批量提交姓名、部门和手机号。系统对每一行依次完成 Doris 用户创建、部门数据库创建、数据库授权以及外部数据连接注册，并提供批次级、行级、步骤级的可追踪日志。

本能力是独立业务模块，名称为“数据空间批量开通”。任务执行必须由独立的 `resource-provisioning` Worker 和 `resource_provisioning` 队列承担，不复用数据平台或批量授权 Worker。

## 2. Excel 模板与预览

首版支持 `.xlsx` 和 `.csv`，表头为：

| 字段 | 必填 | 规则 |
|---|---|---|
| 姓名 | 是 | 去除首尾空格，保留中文显示名 |
| 部门 | 是 | 去除首尾空格，用于生成数据库名 |
| 手机号 | 是 | 仅允许 11 位中国大陆手机号格式 `1[3-9][0-9]{9}` |

上传文件后必须先预览，不能直接执行。预览结果展示原始值、生成的用户名、数据库名、行级校验结果和冲突信息；无效行不得提交。

## 3. 命名规则

- Doris 用户名：姓名汉字转全拼小写并拼接手机号，例如 `张三 + 18888888888 -> zhangsan18888888888`。
- 数据库名：`部门_姓名`，例如 `财政一处_张三`。
- 外部连接 `name` 和 `defaultSchemaName` 均使用数据库名。
- 用户名只允许小写字母、数字和下划线，必须以字母或下划线开头，最长 64 个字符。
- 数据库名只允许字母、数字、下划线和中文，最长 128 个字符。
- 拼音结果、重名或名称冲突必须在预览中明确展示；页面允许执行前逐行修正生成的用户名和数据库名。

## 4. 批次配置

执行批次至少配置：

- Doris 数据连接。
- Doris 用户初始密码。密码不来自 Excel，不以明文写入系统元数据库或日志。
- 有数 `apiAdd` 接口地址、登录账号、登录密码、`projectId`、`paths`、Doris 服务地址和端口。
- 行并行度，默认 2，范围 1 至 10。

页面不再提供手工 Token 输入框。登录账号对应有数 `genToken` 请求中的 `email` 字段；当邮箱不唯一时允许填写有数用户 `uniqueId`。有数登录密码和 Doris 用户初始密码必须分开填写、分开加密保存，均不得出现在 API 返回或日志中。

新批次通过 `apiAdd` 地址推导同域登录地址：将 `/api/dash/dataConnection/apiAdd` 替换为 `/api/dash/util/genToken`。地址不符合该接口契约时阻止提交，不自行猜测跨域或其他上下文路径。

外部接口固定参数首版为：

```json
{
  "type": 124,
  "skipTest": false,
  "parameters": {
    "authType": "ldap",
    "dorisCatalog": "internal",
    "queryQueueSetting": {
      "totalQueueLength": 40,
      "highQueueLength": 1
    },
    "nullSafeEqual": false,
    "driver": "mysql-connector-5.1.49"
  }
}
```

`skipTest` 必须使用 JSON 布尔值 `false`，不得发送字符串。密码仅在执行时解密使用；自动生成的 Token 只存在于 Worker 内存，API 返回和日志必须脱敏。

## 5. 单行执行流程

每行内部严格串行执行：

1. `validate`：再次校验输入、命名和批次配置。
2. `create_user`：创建 Doris 用户。
3. `create_database`：创建 Doris 数据库。
4. `grant_database`：将该数据库权限授予完整生成用户名。
5. `register_connection`：调用外部 `POST /api/dash/dataConnection/apiAdd`。
6. `complete`：汇总该行执行结果。

多行之间按批次并行度执行；单行失败不阻塞其他行。每个步骤必须保存状态、开始/结束时间、耗时、脱敏 SQL 或请求摘要、脱敏响应摘要和错误信息。

`register_connection` 内部先执行有数 Token 管理：检查当前 Worker 内存中是否已有对应账号的 `youdata_token`；没有时调用 `genToken`，成功装载后再调用 `apiAdd`。日志需要显示 Token 来源为“内存复用”“首次生成”或“失效后刷新”，但不得显示 Token 值。

## 6. 幂等、冲突与重试

- Doris 用户、数据库或外部连接已存在且与预期一致时，步骤记为 `skipped`，视为幂等成功。
- 对象已存在但属性不一致时，记为 `conflict` 并停止该行后续步骤。
- 外部接口失败时保留已创建的 Doris 用户、数据库和授权，不自动回滚或删除。
- 支持从失败步骤重试。已成功或已跳过步骤不重复执行。
- 同一批次内用户名或数据库名重复时，预览阶段阻断。
- 批次状态包括 `pending/running/succeeded/partial/failed`；行和步骤状态包括 `pending/running/succeeded/skipped/conflict/failed`。

## 7. 外部接口判定

### 7.1 有数登录与内存 Token

登录接口固定为：

```http
POST /api/dash/util/genToken
```

请求体固定为：

```json
{
  "tokenType": "userPassword",
  "email": "页面填写的账号或 uniqueId",
  "password": "页面填写的有数密码"
}
```

- HTTP 2xx、业务 `code=200` 且 `result` 为非空字符串时，`result` 才能作为 `youdata_token`。
- `youdata_token` 只保存在独立 Worker 进程内存，不写入 MySQL、Redis、批次表、步骤日志或 API 响应。
- 缓存键使用“Token 接口地址 + 登录账号”，不同有数环境和账号不得共享 Token。
- 同一 Worker 内并发处理多行时，对同一缓存键执行单飞控制；首次无 Token 时只允许一个线程调用 `genToken`，其他线程复用结果。
- Worker 重启或进程替换后内存 Token 自然丢失，下一个任务自动重新登录。
- 有数登录账号随批次保存；登录密码必须使用系统凭证密钥加密保存，以便异步 Worker、失败重试和 Worker 重启后重新登录。
- 登录请求摘要中的密码、登录响应中的 `result`、后续请求中的 `token` 必须统一显示为 `******`。

### 7.2 Token 失效与受控刷新

- 仅在 HTTP `401/403`，或业务响应明确表示“请登录”“登录失效”“登录过期”“Token 失效/过期”“Unauthorized/invalid token/not logged in”时判定 Token 失效。
- 普通业务失败、连接冲突、参数错误、HTTP 5xx 不得触发重新登录。
- Token 失效后，先使当前内存 Token 失效，再调用一次 `genToken`，并只重试当前 `apiAdd` 请求一次。
- 多行同时发现同一旧 Token 失效时，只允许一个线程刷新；其他线程复用该线程生成的新 Token，避免登录风暴。
- 刷新后仍返回登录失效或登录接口失败时，当前 `register_connection` 节点失败，不得无限循环，也不得重复执行前面的 Doris 用户、建库和授权节点。

### 7.3 apiAdd 业务结果

- HTTP 非 2xx 一律失败。
- HTTP 2xx 后继续检查 JSON 业务返回。首版兼容常见 `success=true`、`code` 为 `0/200` 或 `status` 为 `success/ok`；响应结构无法判定时保留响应摘要并按失败处理，避免假成功。
- `skipTest=false` 表示外部系统必须执行连接测试；成功日志要同时表明注册和连接测试结果。
- 外部接口当前为 HTTP，生产存在密码和 Token 明文传输风险，应优先升级 HTTPS。
- `authType=ldap` 与 Doris 本地用户认证语义由外部系统负责；本模块按用户提供的接口契约发送，但日志必须保留该参数以便审计。

## 8. 权限与审计

新增模块权限：

- `resourceProvisioning:read`：查看预览、批次、行和步骤日志。
- `resourceProvisioning:execute`：提交和重试批次。

上传、提交、重试均写入审计日志。任何响应不得返回 Doris 密码或外部 Token；请求与 SQL 日志中的密码和 Token替换为 `******`。

## 9. 微服务与环境边界

- 统一 API 提供页面和 `/api/v1/resource-provisioning` 接口。
- 独立 `resource-provisioning` Worker 只消费 `resource_provisioning` 队列。
- 128 测试环境可临时部署独立的有数模拟服务，用于验证 `genToken`、`apiAdd`、Token 内存复用、并发单飞、登录失效刷新、`skipTest=false`、成功、失败和重试。
- 模拟服务源码、镜像、启动参数和模拟 URL 不进入正式 Docker Run 交付包，也不成为应用默认配置。
- 用户未明确说“打包”时，本需求只做本地开发和 128 发布验证，不生成完整部署包。
- 历史批次保留原 `api_token_enc` 字段和手工 Token 执行语义，用于已提交任务重试和日志回读；新页面不再展示 Token 输入框，新批次默认使用 `userPassword` 自动登录。旧字段暂不删除，后续数据库迁移不得破坏历史记录。

## 10. 验收标准

- Excel 正常、空值、手机号错误、重复行和命名冲突预览正确。
- 示例行生成 `zhangsan18888888888` 和 `财政一处_张三`。
- Doris 用户、数据库和授权真实创建成功，使用新用户可连接并访问自己的数据库。
- 外部请求字段完整且 `skipTest` 为布尔 `false`。
- 页面只填写有数账号和密码，不再填写 Token；新批次保存后 API 不返回有数密码或 Token。
- 首批并发只调用一次 `genToken`，后续行和后续任务在同一 Worker 进程中复用内存 `youdata_token`。
- 模拟 `401/403`、业务“请登录”和 Token 失效消息时自动刷新一次并成功重试；普通业务失败和 HTTP 5xx 不触发登录刷新。
- Worker 重启后能够使用加密保存的有数凭证重新生成 Token。
- 多行按配置并行执行，行内步骤保持顺序，单行失败不影响其他行。
- 批次、行、步骤日志在执行中可持续查看，敏感信息均脱敏。
- 失败步骤可重试，已成功步骤不重复破坏资源。
- 独立 Worker 的队列隔离验证通过，其他业务 Worker 不消费该任务。
- 128 可见 Chrome 完成上传、预览、提交、刷新、查看日志和重试闭环，无控制台错误或页面溢出。

## 11. 数据连接授权二级子应用

### 11.1 定位与执行边界

- 在“数据空间批量开通”模块内新增独立二级子应用“数据连接授权”，与原“批量开通”页面并列切换。
- 数据连接授权必须由用户手动选择来源开通批次并点击执行，不得在 `apiAdd` 成功后自动触发。
- 原批量开通的请求字段、保存结果、运行步骤、重试语义和日志保持不变；权限导入失败不得改变原开通批次状态。
- 授权任务使用独立的授权批次、授权行和步骤日志，不写入原 `resource_provisioning_batches/rows/step_logs`。
- 授权任务继续由独立 `resource-provisioning` Worker 和 `resource_provisioning` 队列执行，不新建跨模块 Worker，也不允许其他业务 Worker 消费。

### 11.2 数据来源与配置

- 用户选择一个已有开通批次，系统仅复制其中状态为 `succeeded` 的人员行，形成不可变的授权行快照。
- 每行沿用来源开通行的手机号和数据库名；手机号同时作为 `uniqueId` 及 `userExpireMap` 的键，数据库名作为 `roleName` 和资源名称查询值。
- 有数登录账号、加密登录密码、Token 地址、`projectId` 和 `path` 默认从来源开通批次复制到授权批次快照；Token 仍只存在于 Worker 内存。
- 权限导入接口默认由来源 `apiAdd` 同域地址推导为 `/api/dash/role/importDataPermissions`，页面允许在提交前修改并保存到授权批次快照。
- 资源 ID 查询连接可配置，默认使用来源开通批次的 Doris 连接。
- 资源查询位置可配置，默认数据库 `TESTS`、表 `data_connection`、名称字段 `name`、ID 字段 `id`。库、表、字段配置只允许安全标识符，不接受 SQL 片段。
- 授权到期时间为手动提交授权批次时的必填项；行并行度默认 2，范围 1 至 10。

### 11.3 单行执行流程

每个授权行严格串行执行：

1. `validate`：校验人员快照、查询配置、接口地址、到期时间和权限集合。
2. `lookup_resource`：在配置的 Doris 连接中按数据库名查询资源 ID。
3. `import_permissions`：调用 `POST /api/dash/role/importDataPermissions`。
4. `complete`：汇总当前授权行结果。

默认查询语义为：

```sql
SELECT `id`
FROM `TESTS`.`data_connection`
WHERE `name` = %s
LIMIT 2;
```

- 系统对资源表只执行 `SELECT`，不得执行 `INSERT`、`UPDATE` 或 `DELETE`。
- `apiAdd` 成功后外部系统可能异步落库，因此 `lookup_resource` 支持可配置的查询超时和查询间隔，默认超时 60 秒、间隔 2 秒。
- 查询为零行时在超时前继续轮询；超时后明确记录“未查询到数据连接资源”。
- 查询超过一行时按数据冲突失败，不得自行选择其中一个 ID。
- 查询到的 ID 必须能转换为正整数，并作为 `resourcePermissions[].resourceId` 传入权限接口。

### 11.4 权限导入契约

首版请求体为：

```json
{
  "token": "******",
  "projectId": 6,
  "uniqueId": "18888888888",
  "userExpireMap": {
    "18888888888": "2026-08-03 16:00:00"
  },
  "roleName": "财政一处_张三",
  "path": ["2026年7月培训"],
  "type": 0,
  "importResourceTypes": ["DATA_CONNECTION"],
  "resourcePermissions": [
    {
      "resourceType": "DATA_CONNECTION",
      "resourceId": 2487,
      "permissions": [
        "view",
        "addModel",
        "customSql",
        "sqlFetch",
        "sqlFetchCopyData",
        "sqlFetchExport",
        "sqlFetchShare",
        "updateData",
        "relationship"
      ],
      "isFolder": 0
    }
  ]
}
```

- 权限集合由页面多选配置，默认选中上述九项；提交时至少选择一项。
- `import_permissions` 复用现有 `youdata_token` 内存缓存、并发单飞和受控刷新策略。
- 仅 HTTP `401/403` 或明确登录失效业务响应刷新一次 Token 并重试当前权限请求；HTTP 5xx 和普通业务失败不刷新。
- 权限接口 HTTP 非 2xx 一律失败；HTTP 2xx 后仍需按现有外部接口成功判定规则核验业务响应，无法确认成功时按失败处理。
- `importDataPermissions` 确认成功时必须读取响应顶层 `result`，将其转换为正整数角色 ID；缺失、空值、非整数或非正数均按授权失败处理，不能产生无法追溯外部角色的假成功。
- 成功取得的角色 ID 持久化在对应授权行 `role_id`，API 和页面均返回/展示该值；响应摘要保留脱敏后的业务结果，Token 仍不得落库或进入日志。
- Token、登录密码和连接密码不得进入 API 返回或日志；请求日志中的 `token` 统一显示为 `******`。

### 11.5 角色删除契约

- 页面为每个已取得 `role_id` 且尚未删除的授权行提供“删除角色”操作，必须由用户逐行手动确认后触发。
- 删除接口固定使用权限接口同域地址派生的 `POST /api/dash/role/ext/delete`，请求体严格为：

```json
{
  "token": "******",
  "roleId": 123
}
```

- 删除不得绑定批次重试、授权失败处理、定时任务或自动补偿，避免误删外部已有角色；同一授权行删除成功后按钮禁用。
- 删除复用 `youdata_token` 内存缓存：仅 HTTP `401/403` 或明确登录失效业务响应刷新一次 Token 并只重试当前删除请求；HTTP 5xx 和普通业务失败不刷新。
- 授权行保留原始 `role_id` 作为审计依据，并保存 `role_delete_state`、`role_delete_message`、`role_deleted_at`；删除失败不改变授权行授权成功状态，允许用户再次手动删除。
- 删除请求、响应和异常均写入该行独立步骤日志 `delete_role`，Token 脱敏；删除成功后记录角色已删除状态。

### 11.6 幂等、重试与日志

- 多行按授权批次并行度执行，单行失败不阻塞其他行。
- 每行保存资源 ID；失败重试时，已成功步骤不得重复执行。`lookup_resource` 已成功时直接复用已保存的资源 ID。
- `import_permissions` 成功后步骤记为 `succeeded`；接口明确返回权限已存在时记为 `skipped` 并视为成功。
- 新增步骤均记录状态、尝试次数、起止时间、耗时、脱敏 SQL/请求/响应摘要和明确错误信息。
- 授权批次状态使用 `pending/running/succeeded/partial/failed`；授权行和步骤状态使用 `pending/running/succeeded/skipped/conflict/failed`。
- 失败重试只处理失败或冲突行，不重新执行原批量开通，不重复调用 `apiAdd`。
- 角色删除是独立人工操作，不属于授权批次失败重试范围。

### 11.7 验收标准

- 二级子应用切换不会改变原批量开通表单、批次列表、日志和重试行为。
- 未点击“执行授权”时不产生授权批次，也不调用资源查询或权限接口。
- 默认查询连接和 `TESTS.data_connection(name,id)` 正确回填，修改后提交、刷新和重新进入可回读。
- 资源查询零行轮询、唯一行成功、多行冲突、非整数 ID 均有明确日志。
- 权限请求字段、手机号映射、资源 ID、权限集合和到期时间符合契约。
- Token 首次生成、内存复用、失效刷新、普通失败不刷新及敏感信息脱敏通过回归。
- 授权批次、行和步骤日志在运行中可持续查看；失败行可独立重试。
- `importDataPermissions` 返回 `result` 后，数据库、API 和页面均能看到对应正整数 `role_id`；异常结果有明确失败日志。
- 页面手动删除成功后调用 `/api/dash/role/ext/delete`，请求仅包含 `token` 和 `roleId`，角色删除状态和 `delete_role` 日志可回读；重复点击不会再次调用。
- 删除 Token 失效时只刷新并重试一次，普通失败和 HTTP 5xx 不刷新。
- 128 使用测试专用 Doris 资源表和权限接口 mock 完成真实 API、MySQL、Redis、独立 Worker 闭环，测试资源随后清理。
