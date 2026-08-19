# 2026-07-30 接口编排中心首版及 Mapping 增量发布验证

## 1. 发布范围

本次新增独立业务模块“接口编排中心”，范围包括：

- HTTP 连接器、认证、请求模板、成功规则、测试和启停。
- SQL 包装为 API，支持 Doris、MySQL、Oracle 参数绑定、Schema、行数限制和读写权限隔离。
- Start、HTTP、Condition、SQL API、End 画布节点，以及保存、发布和版本快照。
- Mapping 节点，支持字段提取、重命名、嵌套/数组输出、原类型保持和缺失字段策略。
- 画布连线选中/删除、新增节点初始布局防重叠，以及运行详情随刷新同步更新。
- 异步运行、逐节点日志、运行详情和创建新运行的重新执行。
- 独立 `api_orchestration` 队列与接口编排 Worker。
- 接口编排页面在最大化、左右半屏和还原窗口下的响应式修正。

本次没有接入批量授权、角色授权等业务能力节点，没有生成完整 Docker Run 部署包。

## 2. 128 运行版本

```text
API=oracle-recovery-service-api:20260730-api-orchestration-mapping-ui-r4
API_IMAGE_ID=sha256:9fac525862ba0084b589f1a5139769b5a7c7c79bcc83c628ce94e5c73b13f1bf
WORKER=oracle-recovery-service-worker-api-orchestration:20260730-api-orchestration-mapping-r1
WORKER_IMAGE_ID=sha256:4bd78ff4b8b0331d008aa9425eb4486bd2b03a43bf8570555c64b24f523af3c0
UI_SHA256=1506682e50653304051a7a59fdc78b151722c9136503b1a5ef9f6e4e496e0ff2
```

API 继续以 `APP_SERVICE_MODE=monolith` 提供原页面与全部兼容路由；接口编排任务由独立 Worker 消费 `api_orchestration` 队列。

最新保留的 API 回滚容器：

```text
oracle-recovery-api-pre-20260730-api-orchestration-mapping-ui-r4-20260730-094802
```

## 3. 自动化验证

本地源码与候选镜像测试结果：

```text
完整测试：177 passed, 1 skipped
接口编排专项：19 passed
微服务模式：9 passed
前端内联脚本语法：UI_INLINE_SCRIPT_SYNTAX_OK
画布专项回归：2 passed
```

接口编排专项覆盖模板类型保持、认证、业务成功规则、动态 URL 主机限制、HTTP/非法 JSON 错误、敏感 Header 拦截、无环与可达性、分支汇聚、条件默认分支、上下文加密、SQL 参数绑定、Schema、行数限制和写权限。

Mapping 专项 128 E2E：

```text
API_ORCHESTRATION_MAPPING_E2E_OK checks=17
```

覆盖映射类型保持、嵌套对象、数组组合、文本插值、存在的 `null`、缺失字段 `error/null`、非法配置 400、引用日志和敏感输出脱敏。

128 API/Worker/Redis/MySQL/Doris Mock E2E：

```text
API_ORCHESTRATION_E2E_OK checks=22
```

覆盖连接器认证和异常状态、SQL 注入字符按数据绑定、禁用 SQL API、只读副作用拦截、写权限拒绝、Doris DDL/DML、条件分支、发布快照冻结、异步运行和逐节点日志。

## 4. 可见 Chrome 验证

核心闭环共 `24` 项通过：连接器编辑回读与 Mock 调用、SQL API 新建/调用/停用/启用、画布新增/拖动/选择/连线/删除、流程保存/发布/运行、逐节点日志和重新执行。

四种真实窗口状态共 `32` 项布局检查通过，均无根级横向溢出，刷新后的布局检查无 `console error` 或 `pageerror`：

| 状态 | `outer` | `inner` |
|---|---|---|
| 最大化 | `1920x1032` | `1920x945` |
| 左半屏 | `960x1032` | `944x937` |
| 右半屏 | `960x1032` | `944x937` |
| 还原窗口 | `1280x850` | `1264x755` |

运行摘要的流程、状态、信息已分区显示；长流程名称在最大化、半屏和还原窗口中均能在列表内换行。核心闭环中唯一 HTTP 400 是验证“停用 SQL API 不可调用”的预期结果，不是页面异常。

页面删除清理模式另有 `3` 项通过，验证流程、SQL API 和连接器均可从页面删除。

Mapping 增量继续验证了映射节点新增、模板编辑、缺失策略、连线删除/重连、保存、刷新回读、发布、运行和逐节点日志；发现并修复运行列表刷新后详情仍停留在 `running` 的问题。最终节点坐标为 Start `60,120`、Mapping `320,120`、End `860,120`，四种窗口均无节点重叠或页面级横向溢出，控制台和 `pageerror` 均为空。

截图和机器可读布局结果位于：

```text
artifacts/128-releases/20260730-api-orchestration-mapping-ui-r4/
```

## 5. 最终清理与审计

清理脚本只匹配 `CODEX_API_ORCH_E2E%` 和 `CODEX_CHROME_%`，执行后结果：

```text
node_runs  0
runs       0
workflows  0
sql_apis   0
connectors 0
api_keys   0
queue      0
active_runs 0
mock_absent yes
csv_test.CODEX_API_ORCH_E2E 0
```

Mapping 增量清理额外只匹配 `CODEX_API_ORCH_MAPPING_E2E%` 和 `CODEX_UI_MAPPING_E2E%`；流程、运行记录和节点日志均已清零，接口编排队列和活动运行数均为 0。

API 健康检查正常，Worker 日志包含 `service_mode=api-orchestration queues=api_orchestration` 和 `ready.`；API 与 Worker 最近 20 分钟日志未发现 Traceback、500、ERROR 或 CRITICAL。容器内 UI 哈希与本地源码一致。

首版清理脚本：

```text
artifacts/128-releases/20260730-api-orchestration-ui-r3/cleanup-api-orchestration-test-data-128.sh
```

Mapping 增量清理脚本：

```text
artifacts/128-releases/20260730-api-orchestration-mapping-ui-r4/cleanup-api-orchestration-mapping-e2e-128.sh
```

## 6. 环境风险

最终审计时，128 可用内存约 `597MB`，无 Swap。Doris 对 `information_schema` 查询触发 `MEM_ALLOC_FAILED`，错误显示可用内存约 `500MB`，低于 Doris `742MB` 低水位；改用 FE `SHOW TABLES` 后确认测试表不存在。

该问题不是接口编排逻辑错误，但会使后续 Doris 实际查询在高内存压力下不稳定。当前主机同时运行 Doris、Oracle 19c/21c、SQL Server、两个 MySQL 和多组 Worker；本次没有调整 Doris、数据库容器或主机资源配置。

Mapping 开发前完成项目临时文件清理，本地释放约 `10.27GB`，128 释放约 `7.44GB`。最终系统盘占用 `82%`，剩余约 `14GB`；未删除运行镜像、数据库卷、回滚容器或其他项目内容。

## 7. 打包与回滚边界

本次只完成本地开发、候选镜像构建和 128 热发布验证，没有生成或修改完整 Docker Run 部署包。

r4 仅替换 API 前端，接口编排 Worker 使用 Mapping r1。需要回滚 r4 时，停止并移除当前 API，将上述 r4 回滚容器改名为 `oracle-recovery-api` 后启动；回滚前仍必须确认无在途任务并保留当前容器检查信息。

## 8. n8n 风格工作台 r4 收口验证

### 8.1 发布内容

本轮只调整 `.orchestration-designer` 的桌面最小高度：从 `560px` 改为 `420px`，使 `1280x850` 还原窗口中的画布底部缩放控制保持在首屏内。`900px` 以下窄屏规则、流程 JSON、接口、权限、发布快照和 Worker 执行语义均未改变。

```text
API=oracle-recovery-service-api:20260730-api-orchestration-workbench-ui-r4
WORKER=oracle-recovery-service-worker-api-orchestration:20260730-api-orchestration-mapping-r1
UI_SHA256=6e31a1945843d6982896d496753e2e681be3a5b68759a049029a806723bc7564
ROLLBACK_API=oracle-recovery-api-pre-20260730-api-orchestration-workbench-ui-r4-20260730-131708
```

### 8.2 自动化与页面闭环

```text
完整测试：176 passed, 1 skipped
接口编排专项：19 passed
保存：HTTP 200
发布：HTTP 200
运行：HTTP 202
运行结果：succeeded，Start/Mapping/End 3 个节点全部成功
console error：0
pageerror：0
```

连接器、流程设计、SQL API、运行中心四个入口均可切换，且无页面级横向溢出。可见 Chrome 布局结果：

| 状态 | `outer` | `inner` | 工作台高度 | 控制条 |
|---|---|---|---|---|
| 最大化 | `1920x1032` | `1920x945` | `605px` | 首屏内 |
| 左半屏 | `960x1032` | `944x937` | `597px` | 首屏内 |
| 右半屏 | `960x1032` | `944x937` | `597px` | 首屏内 |
| 还原窗口 | `1280x850` | `1264x755` | `420px` | 首屏内，底部 `716px` |

截图与发布、清理、审计脚本位于：

```text
artifacts/128-releases/20260730-api-orchestration-workbench-ui-r4/
```

### 8.3 清理与终检

隔离流程只使用 `CODEX_N8N_UI_E2E%` 命名空间。流程通过页面删除后，清理脚本删除其独立运行记录和节点日志；最终流程、运行、节点日志、队列和活动运行均为 `0`。

API 健康检查正常，Worker 保持运行且未重启；最近 20 分钟 API/Worker 日志无 `Traceback`、`Internal Server Error`、`CRITICAL` 或异步任务异常。终检时系统盘占用 `82%`、剩余约 `14GB`，可用内存约 `679MB`、无 Swap，资源风险继续保留。

本轮只进行 128 最小热更新，没有生成完整 Docker Run 部署包。

## 9. Postman 风格连接器工作台

### 9.1 设计依据与范围

本轮实际打开本机 Postman `12.21.2` 的 `Untitled Request` 页面进行视觉和交互核对，采用其真实的“左侧 Collection/资产区、请求名称与保存、Method/URL/Send、请求配置标签、底部全宽 Response”信息架构。项目仍使用自身品牌色、字段契约和权限体系，不复制 Postman 品牌素材或未实现能力。

本系统额外保留 `Input` 标签用于模拟流程传入的上下文，保留 `Success` 标签用于 HTTP 与业务状态联合判定；没有虚构 Collection、Environment、Cookie、Scripts、Tests、multipart 或 form-data 后端能力。

### 9.2 发布版本

```text
API=oracle-recovery-service-api:20260730-api-orchestration-connector-workbench-r4
WORKER=oracle-recovery-service-worker-api-orchestration:20260730-api-orchestration-mapping-r1
UI_SHA256=501bc1b19366d831a45fd3c83512a6fa6a4e76b234b0eb6d76bfb2445266773d
ROLLBACK_API=oracle-recovery-api-pre-20260730-api-orchestration-connector-workbench-r4-20260730-144524
```

r1 完成首版结构；r2 修复还原窗口标签点击区域被 URL 输入覆盖；r3 按实际 Postman 页面将 Input 纳入标签、Response 改为全宽并调整编辑/响应比例；r4 修复 `body_template=null` 回填兼容问题。接口编排 Worker 始终没有重建或重启。

### 9.3 自动化与业务闭环

```text
完整测试：177 passed, 1 skipped
接口编排专项：20 passed
内联 JavaScript：通过
连接器 ID 唯一性：通过
新建保存：HTTP 201
编辑、启停与恢复：HTTP 200
正常发送：HTTP 200，页面显示 HTTP 200 与耗时
失败规则发送：HTTP 502，页面显示明确业务错误
删除：HTTP 204
console error：0
pageerror：0
```

真实页面覆盖资产搜索无结果/命中、Params/Auth/Headers/Body/Input/Success 标签值保留、Basic 与 Bearer 字段变化、密码保存后不回显、空密码再次保存不丢失、认证切回 none 后清除密钥、`null` Body 刷新和启停后保持 `null`、成功响应和失败响应展示。

### 9.4 可见 Chrome 布局

| 状态 | `outer` | `inner` | 工作台高度 | Response 高度 | 结果 |
|---|---|---|---|---|---|
| 最大化 | `1920x1032` | `1920x945` | `665px` | `239px` | 通过 |
| 左半屏 | `960x1032` | `944x937` | `657px` | `245.5px` | 通过 |
| 右半屏 | `960x1032` | `944x937` | `657px` | `245.5px` | 通过 |
| 还原窗口 | `1280x850` | `1264x755` | `475px` | `154.5px` | 通过 |

四种窗口均无页面级横向溢出，标签栏点击区域稳定为 `39px`，Method、URL、发送、保存、资产列表和 Response 均无重叠或截断。截图及发布、审计脚本位于：

```text
artifacts/128-releases/20260730-api-orchestration-connector-workbench-r4/
```

### 9.5 清理与终检

隔离连接器 `CODEX_CONNECTOR_WORKBENCH_E2E` 已通过页面删除，数据库匹配数量为 `0`；接口编排队列和活动运行均为 `0`。API 健康，API/Worker 最近 30 分钟日志无 `Traceback`、`Internal Server Error`、`CRITICAL` 或异步任务异常。

终检时系统盘占用 `82%`、剩余约 `14GB`，可用内存约 `680MB`、无 Swap。资源风险继续保留。本轮只进行 128 最小热更新，没有生成完整 Docker Run 部署包。

## 10. 流程设计与 SQL API 工作台 r7 全模块收口

### 10.1 发布内容

本轮在 Postman 风格连接器工作台基础上完成流程设计和 SQL API 工作台优化。流程设计新增独立运行输入面板、资产数量/搜索结果计数，以及 HTTP、Mapping、Condition、SQL API 节点绑定摘要；SQL API 新增资产搜索、数据库客户端式上下文栏、SQL/Input Schema 标签、行号、Tab 缩进、Schema Beautify、动态调用路径和运行状态反馈。后端接口、数据模型、权限和 Worker 执行语义未改变。

```text
API=oracle-recovery-service-api:20260730-api-orchestration-workbenches-r7
WORKER=oracle-recovery-service-worker-api-orchestration:20260730-api-orchestration-mapping-r1
UI_SHA256=3ac2fa81c1a0085181768cb3f9a1fe08bb1a2df22039fff2248f98c8eea5c117
ROLLBACK_API=oracle-recovery-api-pre-20260730-api-orchestration-workbenches-r7-20260730-180541
```

本轮仅最小热发布 API，接口编排 Worker 未重建、未重启，未生成完整 Docker Run 部署包。

### 10.2 自动化与 Chrome 业务闭环

```text
完整测试：178 passed, 1 skipped
接口编排专项：21 passed
连接器真实调用：HTTP 200，健康接口返回 status=ok
SQL API 真实调用：HTTP 200，SELECT :probe AS probe 返回 probe=sql-e2e
成功流程：Start -> HTTP -> Mapping -> Condition -> SQL API -> End，6 个节点全部成功
失败流程：缺少 input.probe，准确停在 Mapping
重新执行：已生成新的运行记录并完成状态回读
console error：0（成功闭环与最终布局检查）
pageerror：0
```

Chrome 隔离样例：

| 类型 | 名称 | ID / 标识 |
|---|---|---|
| 数据连接 | `CODEX_API_ORCH_SQL_E2E` | `6732abe3-49fc-42d4-9e9f-0944a7953d3f` |
| 连接器 | `CODEX_CONNECTOR_EDITORS_E2E` | `061e87b7-e2b4-4ca2-8131-e4a89453f9d1` |
| SQL API | `CODEX_SQL_API_E2E` | `438f6694-9648-442f-a973-6cf9fcc1e177` / `codex-sql-api-e2e` |
| 流程 | `CODEX_WORKFLOW_FULL_E2E` | 发布修订 R2 |

连接器覆盖搜索、Params/Headers 键值编辑、Body/Input、保存回读、启停和发送；SQL API 覆盖搜索、SQL/Schema 编辑、保存回读、调用、停用拦截和重新启用；流程覆盖六类节点、连线、节点属性、保存、发布、成功/失败运行和重新执行；运行中心覆盖运行摘要、六节点状态和 SQL API 请求/响应日志。

### 10.3 可见 Chrome 布局

| 状态 | `outer` | `inner` | 结果 |
|---|---|---|---|
| 最大化 | `1920x1032` | `1920x945` | 四模块及节点属性面板通过 |
| 左半屏 | `960x1032` | `944x937` | 四模块及节点属性面板通过 |
| 右半屏 | `960x1032` | `944x937` | 四模块及节点属性面板通过 |
| 还原窗口 | `1280x850` | `1264x755` | 四模块、成功 Response 和节点日志通过 |

四种窗口均无根级、模块级和子面板级非预期横向溢出。窄屏流程画布使用自身横向滚动，属性面板以右侧抽屉显示；节点、属性字段、SQL 编辑器、Response 和日志没有互相遮挡。截图位于：

```text
artifacts/128-releases/20260730-api-orchestration-workbenches-r7/chrome/
```

### 10.4 清理与终检

流程、SQL API、连接器和临时数据连接均通过页面删除；独立运行记录和节点日志使用只匹配 `CODEX_WORKFLOW_FULL_E2E%` 的脚本清理。最终终检：

```text
test_assets=0
api_orchestration_queue=0
active_runs=0
API health=ok
API image=oracle-recovery-service-api:20260730-api-orchestration-workbenches-r7
Worker image=oracle-recovery-service-worker-api-orchestration:20260730-api-orchestration-mapping-r1
API/Worker recent fatal errors=0
```

清理和终检脚本：

```text
artifacts/128-releases/20260730-api-orchestration-workbenches-r7/cleanup-api-orchestration-workbenches-r7-e2e-128.sh
artifacts/128-releases/20260730-api-orchestration-workbenches-r7/audit-api-orchestration-workbenches-r7-128.sh
```

终检时 128 系统盘占用 `83%`、剩余约 `13GB`，可用内存约 `603MB`、无 Swap。系统元数据库实际版本为 MySQL `9.3.0`，与后续部署包固定 `mysql:8.4` 的项目规则存在环境差异；本轮没有调整系统库或数据卷，后续完整打包和新环境部署仍必须使用 `mysql:8.4`，不能让 9.x 数据卷直接降级启动。

### 10.5 可见 Chrome 保留演示样例复验

用户反馈前次验收后页面没有留下可查看的任务。核查确认前次资产使用 `CODEX_*_E2E` 隔离命名，并按第 10.4 节规则在验收后清理。本轮改用 `DEMO_*` 命名，在 128 可见 Chrome 中重新创建真实资产、完成调用和流程运行，并按用户要求保留。

保留连接器：

| 名称 | ID | 请求地址 | Chrome 调用结果 |
|---|---|---|---|
| `DEMO_HTTP_系统健康检查` | `9efa21fe-4e9e-4729-b016-195c6c370f65` | `http://oracle-recovery-api:8000/api/v1/health` | HTTP 200，最近一次 14 ms |
| `DEMO_HTTP_OpenAPI文档` | `1f5e8e67-fd89-454b-a1e0-736ec8f8fb64` | `http://oracle-recovery-api:8000/openapi.json` | HTTP 200，约 2510 ms |

保留 SQL API：

| 名称 | ID / slug | SQL | Chrome 调用结果 |
|---|---|---|---|
| `DEMO_SQL_服务器时间` | `617829f7-cab9-44de-86b0-0ad91d9a7393` / `demo-sql-server-time` | `SELECT NOW() AS server_time, 'mysql' AS engine` | 成功返回一行 MySQL 服务器时间 |
| `DEMO_SQL_参数回显` | `af96a32c-d05a-4850-86e2-a5bd54ac046c` / `demo-sql-parameter-echo` | `SELECT :message AS message, :request_id AS request_id` | 成功返回 `Chrome保留样例` 和 `2026073003` |

保留流程和运行记录：

| 流程 | 流程 ID | 发布版本 | 运行 ID | 结果 |
|---|---|---|---|---|
| `DEMO_FLOW_健康检查与数据库时间` | `36c0a951-86af-466a-b17d-9069b65d373f` | R2 | `5a289807-d9b6-4f6b-842d-66d9aeadb049` | Start、HTTP、SQL API、End 共 4 个节点全部成功 |
| `DEMO_FLOW_运行入参与参数SQL` | `10204cef-bfe1-46bb-8c5b-c06367705381` | R2 | `3c2c3b6b-9abe-402c-ba5a-edee2989c97a` | Start、SQL API、End 共 3 个节点全部成功，节点日志完整回显运行参数 |

第二个流程的运行输入为：

```json
{
  "message": "Chrome保留任务验证",
  "request_id": 2026073002
}
```

页面与服务终检：

```text
Chrome outer=1920x1032
Chrome inner=1920x945
console error=0
pageerror=0
root horizontal overflow=none
api_orchestration_queue=0
active_runs=0
retained DEMO connectors=2
retained DEMO SQL APIs=2
retained published DEMO workflows=2
retained succeeded DEMO runs=2
API health=ok
API/Worker recent fatal errors=0
RETAINED_DEMO_AUDIT_OK
```

截图位于：

```text
artifacts/128-releases/20260730-api-orchestration-workbenches-r7/chrome-retained-demo/
```

本轮未修改源码、镜像或容器，未重启服务，也未生成部署包。上述 `DEMO_*` 资产和运行记录不进入 `CODEX_*_E2E` 清理范围，除非用户后续明确要求，否则保持可见。
