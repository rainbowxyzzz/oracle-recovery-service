# 审批流自动授权模块 PRD

更新日期：2026-08-17

2026-08-19 补充自动监听与审批状态回写闭环：审批流自动授权配置支持启用定时监听，系统按配置周期自动调用 `getMyTodoList`，筛选 `auditStatus=0` 和空值申请后直接进入同一处理链；默认每轮只处理 1 个申请，避免生产中重复授权和外部状态竞争。每个申请在内部 Doris 授权、有数 `apiAdd` 和 `importDataPermissions` 全部成功后，必须调用审批系统 `POST /api/market/dataModelApplyFlow/auditStatus`，Header 继续传入 `workflow_token`，Body 为 `{ "id": applyFlowId }`，将外部待办状态回写。若授权已成功但状态回写失败，后续监听不得重新执行整套授权流程，应优先只重试状态回写。

## 1. 背景

实际业务中，数据授权来自外部审批系统。系统需要先登录审批系统获取待处理申请，再逐个申请读取详情、授权数据列表、Doris 部门数据库映射和表元数据，最终完成内部 Doris 批量授权、外部有数数据连接创建和人员权限导入。

该流程具备固定业务语义、状态持久化、幂等重试和高权限授权动作，不作为普通接口编排画布直接执行，而新增独立业务模块“审批流自动授权”。模块可以复用接口编排中心的 HTTP、SQL、动态参数和日志思路，但后端路由、服务、运行记录与步骤测试能力独立实现。

## 2. 目标

- 新增独立模块“审批流自动授权”。
- 支持配置审批系统接口：`login`、`getMyTodoList`、`getDetail`、`getList`。
- 支持配置有数接口：`genToken`、`apiAdd`、`importDataPermissions`。
- 支持选择 Doris 数据连接，并配置 `TESTS.单位与数据库映射表`、`TESTS.授权信息表`。
- 支持每一步独立运行测试，便于核对返回值读取规则是否准确。
- 完整运行时按 `applyFlowId` 逐个处理，并记录每个节点的原始请求摘要、原始响应、结构化提取结果、SQL、SQL 返回结果和状态。
- 支持配置级自动监听，定时发现并处理 `auditStatus=0` 和空值的新申请。
- 支持授权成功后自动回写审批系统申请状态，避免同一申请长期保持待办并被重复消费。
- Docker 日志必须输出关键步骤日志，便于外部部署环境直接排查。
- 保留现有批量授权中心能力，不改动已有批量授权入口的默认业务语义。

## 3. 业务规则

### 3.1 审批系统登录

调用 `POST /api/market/login`，请求体包含用户名和密码。成功后从响应 `data.token` 提取审批系统 `workflow_token`，后续审批系统接口通过 Header 传入。
默认 Header 形式为 `token: <workflow_token>`，前缀为空；如果目标审批系统确实要求 `Authorization: Bearer <token>`，可在配置中显式切换。

### 3.2 待办申请列表

调用 `POST /api/market/dataModelApplyFlow/getMyTodoList`，Header 使用 `workflow_token`，Body 包含分页参数。返回后从 `data.list` 中筛选 `auditStatus = 0` 或空值的集合，提取每条记录的 `id`。该 `id` 即后续处理的 `applyFlowId`。

### 3.3 申请详情

对每个 `applyFlowId` 调用 `POST /api/market/dataModelApplyFlow/getDetail`，Header 使用 `workflow_token`，Body 为 `{ "id": applyFlowId }`。

详情解析规则：

- `createUserDepartment` 若以 `重庆市审计局/` 开头，则取最后一个 `/` 后的部门名称；否则取第一个 `/` 前的内容作为部门名称。
- 使用该部门名称查询 Doris `TESTS.单位与数据库映射表`，条件为 `部门 = 截取后的部门`，返回 `数据库` 字段作为目标数据库。
- `queryUserList` 中所有对象的 `tel` 组成 `uniqueIds`。
- `queryEndTime` 追加 `23:59:59` 形成 `userExpireMap` 的过期时间。
- `createUserName` 与 `createUserMobile` 用于生成内部授权用户：`createUserName_createUserMobile后四位_MMDD`，密码使用默认密码。字段名以实际返回 `createUserName` 为准，用户口述中的 `createUserNmae` 视为笔误。

用户已确认当前业务不存在不同库同名表，因此表元数据关联可以按 `dataTitle` 唯一匹配；如果实际查询返回 0 条或多条，系统仍需记录异常日志并阻断当前 `applyFlowId`，避免静默误授权。

### 3.4 授权数据列表

调用 `POST /api/market/dataModelApplyData/getList`，Header 使用 `workflow_token`，Body 包含 `applyFlowId` 和分页参数。返回后提取 `data.list[*].dataTitle` 和 `data.list[*].dataLevel`。

对每条授权数据：

- 通过 Doris `information_schema.tables` 根据 `dataTitle` 查询 `schema_name`，查询范围必须限制为 `TABLE_SCHEMA LIKE 'DWD_%'`，避免同名表存在于非 DWD 业务层时导致匹配结果不是 1 条。
- 写入 Doris `TESTS.授权信息表`，字段至少包含：`applyFlowId`、`datatitle`、`dataLevel`、`schema_name`。

### 3.5 内部批量授权

复用批量授权中心已有授权能力，但本流程不再使用现有部门映射生成的授权对象，而是创建并使用本次申请生成的内部授权用户。授权目标为上一步解析出的目标库、目标表和字段/表权限策略。
同时，无论本次申请数据包含哪些授权表，系统都必须先对步骤 3.3 解析得到的映射数据库授予基础库访问权限，确保 `apiAdd` 在使用该 `DWH_` 基础库作为 `defaultSchemaName` 时能够通过连接测试。基础库授权属于前置必需步骤，不得依赖业务表清单是否包含该库。

### 3.6 有数数据连接与人员授权

调用 `POST /api/dash/util/genToken` 获取有数 `youdata_token`。

调用 `POST /api/dash/dataConnection/apiAdd`：

- `token` 使用 `youdata_token`。
- `userName` / `password` 使用本流程创建的内部授权用户与默认密码。
- `defaultSchemaName` 使用 `TESTS.单位与数据库映射表` 查询到的 `数据库` 字段。
- `server` 自动使用当前选择的 Doris 数据连接 `host`，即本流程创建的 Doris 授权用户实际登录 Doris 时使用的 IP 或域名；配置中留空时不得继续向接口提交空 `server`。
- 目录参数使用 `paths` 字段，不使用 `path` 字段。
- 其他入参按配置默认值或页面配置填写。
- 成功后提取返回 `id`，作为 `resourceId`。

调用 `POST /api/dash/role/importDataPermissions`：

- `token` 使用 `youdata_token`。
- `roleName` 默认使用上一步 `apiAdd` 创建的数据连接名称；若步骤测试上下文未显式传入，则回退为本次 `apiAdd` 的默认命名规则，确保不会出现空角色名。
- `uniqueIds` 为所有 `tel`。
- `userExpireMap` 为 `tel -> queryEndTime 23:59:59`。
- `resourceId` 为 `apiAdd` 返回的 `id`。

### 3.7 审批状态回写

当单个 `applyFlowId` 已完成内部 Doris 授权、有数数据连接创建和有数人员权限导入后，系统必须调用审批系统状态更新接口：

```http
POST /api/market/dataModelApplyFlow/auditStatus
Header:
  token: <workflow_token>

Body:
{
  "id": "<applyFlowId>"
}
```

默认路径由配置项 `audit_status_update_path` 提供，默认值为 `/api/market/dataModelApplyFlow/auditStatus`；默认请求体只包含当前申请 ID。该步骤必须作为完整流程最后一步，前置任一步失败时不得执行，避免未完成授权的申请被误标记为已处理。

如果 `importDataPermissions` 已经成功但 `auditStatus` 回写失败，运行记录应体现为失败或部分失败，并在日志中明确 `audit_status_update` 失败。后续自动监听再次发现同一 `applyFlowId` 时，应优先识别已有 `import_permissions=success` 且 `audit_status_update` 未成功的记录，只重试状态回写，不重新创建 Doris 用户、不重复授权、不重复调用 `apiAdd` 和 `importDataPermissions`。

### 3.8 自动监听

配置支持以下监听参数，存放于高级配置 JSON，不新增系统表结构：

- `auto_watch_enabled`：是否启用自动监听，默认 `false`。
- `auto_watch_interval_minutes`：扫描间隔，默认 `5` 分钟，允许范围 `1-1440`。
- `auto_watch_max_items_per_scan`：每轮最多处理申请数，默认 `1`。
- `auto_watch_skip_status_updated`：发现已有 `audit_status_update=success` 的 `applyFlowId` 时跳过，默认 `true`。
- `update_audit_status_after_success`：完整流程成功后是否执行审批状态回写，默认 `true`。

自动监听流程：

1. API 服务启动审批流自动授权监听线程。
2. 定时扫描状态为 `active` 且 `auto_watch_enabled=true` 的配置。
3. 同一配置已有 `created/running` 运行记录时跳过本轮，避免并发重复消费。
4. 到达扫描间隔后创建 `mode=auto_watch` 的运行记录。
5. 登录审批系统并调用 `getMyTodoList`。
6. 筛选 `auditStatus=0` 的申请，按 `auto_watch_max_items_per_scan` 截取。
7. 对每个申请按完整流程处理；若发现该申请只缺状态回写，则只执行 `audit_status_update`。
8. 更新运行记录的成功、失败、跳过数量和日志。

## 4. 状态与日志

任务状态：

```text
created
running
partial_failed
success
failed
update_failed
```

步骤状态：

```text
pending
running
success
failed
skipped
```

每个步骤日志必须包含：

- step key 与中文名称。
- `applyFlowId`，全局步骤可为空。
- 请求方法、URL、Header 摘要、Body 摘要。
- 原始响应 JSON 或错误信息。
- 结构化提取结果。
- SQL 文本、SQL 参数和 SQL 返回结果。
- 开始时间、结束时间、耗时、状态。
- 自动监听运行还需记录扫描配置、命中的 `auditStatus=0` 数量、实际处理 ID、跳过原因和状态回写结果。

敏感字段如密码、token 需要在页面和日志摘要中脱敏；Docker 日志默认只显示结构化字段名和截断后的响应，但允许在测试环境显式开启“诊断日志模式”后输出完整请求、响应和 token 便于排查。该模式不得作为生产默认值；开启后 token、Authorization 等会明文显示，但 password、pwd、secret 仍必须脱敏。

## 5. 页面能力

- 配置管理：保存外部系统地址、用户名、密码、接口路径、Doris 连接、映射表、授权信息表、默认密码、项目 ID、Doris server/port/path 等参数。
- 配置管理：保存外部系统地址、用户名、密码、接口路径、Doris 连接、映射表、授权信息表、默认密码、项目 ID、Doris server/port/path 等参数，并可显式开启仅测试环境使用的诊断日志模式。
- 配置管理：可开启或关闭自动监听，配置扫描间隔和每轮最大处理数量。
- 步骤测试：每一步可单独点击测试。依赖上一步 token 或 `applyFlowId` 的步骤允许手动输入测试上下文。
- 步骤测试上下文 JSON 必须随当前测试步骤动态切换模板：无上游依赖的步骤显示 `{}`，依赖审批 token、`applyFlowId`、部门、授权数据列表、schema 记录、有数 token 或资源 ID 的步骤只展示该步骤所需字段，避免用户面对固定且无解释的通用 JSON。
- 完整运行：一键执行完整流程。
- 监听扫描：允许手动触发一次监听扫描，用于验证当前配置的待办发现、去重和状态回写链路。
- 运行记录：展示总任务、每个 `applyFlowId` 子流程、每个步骤节点状态。
- 日志查看：可查看原始响应、结构化提取结果、SQL 和 SQL 返回结果。

## 6. 验收标准

- 可保存并回读配置。
- `login`、`getMyTodoList`、`getDetail`、`getList`、部门映射 SQL、授权信息写入 SQL、`genToken`、`apiAdd`、`importDataPermissions` 均可单步测试。
- `auditStatus` 状态回写可单步测试，请求 Header 使用审批系统 token，Body 使用当前 `applyFlowId`。
- 切换单步测试步骤时，测试上下文 JSON 自动更新为该步骤模板，不再固定显示同一份 `apply_flow_id/workflow_token/youdata_token` 占位。
- 完整运行可处理多个 `auditStatus = 0` 或空值的 `applyFlowId`，每个 ID 独立记录成功或失败。
- 启用自动监听后，系统可按周期发现新增 `auditStatus=0` 或空值申请；默认每轮只处理 1 个，且不会并发重复运行同一配置。
- 单个申请全流程成功后必须产生 `audit_status_update=success` 日志；若状态回写失败，后续监听只重试回写，不重复前置授权。
- Docker 日志默认能看到步骤开始、接口响应结构、提取结果、SQL 和 SQL 返回摘要；开启诊断日志模式后，可额外看到完整请求、响应和 token 明文，便于定位 117 一类接口问题，但密码类字段仍需脱敏。
- 任一步骤读取字段不准确时，可以通过单步测试结果定位。
- 不影响现有批量授权中心、接口编排中心和 Doris CSV 导入模块。

## 7. 2026-08-19 运行日志分层窗口优化

### 7.1 背景与问题

当前页面在主界面右侧直接堆叠某次运行的全部步骤日志，每个步骤又同时内联结构化提取结果、原始响应和 SQL 结果。当一次运行包含多个 `applyFlowId`、多个步骤或较大 JSON 时，日志内容在主页面上挤占成超长列表，无法快速定位申请、失败步骤和具体请求或 SQL。

### 7.2 交互层级

运行日志调整为三级信息架构：

1. 一级主页面只展示运行记录摘要：配置名、运行模式、状态、成功/失败/跳过数量、时间和消息。
2. 点击“查看日志”打开二级“运行日志”窗口，展示运行概览、按 `applyFlowId` 分组的快速导航和紧凑步骤时间线。全局步骤归入“全局步骤”分组，失败步骤需有明确状态标识。
3. 点击单个步骤的“查看详情”打开三级“步骤日志详情”窗口，通过分页切换“概览、请求、响应、提取结果、SQL”，一次只聚焦一类内容。

### 7.3 兼容性边界

- 本次只调整审批流自动授权前端展示，不改变现有运行和日志 API、数据库表、任务状态、日志字段、脱敏规则和自动监听逻辑。
- 二级和三级窗口只展示后端已返回的日志，不额外请求或暴露未脱敏凭据。
- 三级窗口关闭后应回到原二级运行上下文；按 `Escape` 应先关闭三级窗口，再关闭二级窗口。
- 不影响流程配置、单步测试、完整运行、监听扫描和原有权限点。

### 7.4 验收要求

- 主页面不再内联展示所有步骤的大 JSON 和 SQL。
- 二级窗口可以按 `applyFlowId` 筛选，并清晰展示步骤状态、消息、开始/结束时间。
- 三级窗口的请求、响应、提取结果和 SQL 分页独立滚动，长 JSON 不撑大窗口。
- 最大化、左右半屏和还原窗口下均无页面级横向溢出，标题、分组导航、步骤列表、分页和关闭操作可见。
- 必须回归运行记录刷新、单步测试、完整运行和监听扫描入口的原有行为。

## 8. 2026-08-20 自动监听状态可视化

### 8.1 背景与目标

自动监听开关、监听间隔和每轮处理数量目前位于较长的配置表单中。保存后，用户无法在页面主视区和运行记录中直接确认当前配置是否已经启用监听，也容易把“监听扫描一次”或“完整运行”误认为持续监听开关。

页面需要把配置级持续监听状态提升到主视区，并在运行记录中补充当前配置的监听状态注释。该状态表示“配置是否已保存为启用监听”，不表示某一历史运行仍在执行。

### 8.2 展示与交互规则

- 审批流自动授权页面顶部增加“自动监听状态”卡片，显示当前配置名称、`监听已开启 / 监听已停用 / 配置已禁用 / 未选择配置`、扫描间隔、每轮处理数量、最近扫描时间和下次预计扫描时间。
- 状态卡提供“修改监听设置”入口，点击后定位到原有监听配置区域；配置保存仍复用现有“保存配置”接口和按钮，不新增绕过整体验证的独立更新接口。
- 用户修改监听开关、间隔或每轮处理数量但尚未保存时，状态卡必须明确显示“有未保存的监听设置”，避免把表单暂存值误认为服务器已生效值。
- 每条运行记录增加“当前监听：已开启 / 已停用 / 配置已禁用 / 配置不存在”注释；运行模式继续独立显示“自动监听 / 完整运行 / 单步测试”，不得混淆历史运行来源和当前监听状态。
- “监听扫描一次”只执行一轮；“完整运行”只执行一次完整流程；持续监听仅在保存 `auto_watch_enabled=true` 后生效。

### 8.3 兼容性与验收

- 本次只修改前端展示，不改变调度线程、监听周期、配置接口、数据库结构、运行接口和权限点。
- 保存监听启用和停用状态后必须刷新配置并按服务端返回值回读；页面刷新或重新进入模块后状态保持一致。
- 需验证未选择配置、监听停用、监听开启和配置禁用四类展示，以及间隔和每轮处理数量修改前后的未保存提示。
- 需回归现有配置保存、完整运行、监听扫描一次、单步测试和运行日志分层窗口。
