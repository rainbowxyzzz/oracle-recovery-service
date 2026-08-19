# 接口编排中心按钮级 UI 与交互合理性审计报告

## 1. 审计结论

- 审计对象：`192.168.150.128:8000/ui` 当前运行版本的“接口编排中心”。
- 审计方式：可见 Google Chrome + CDP，逐项真实点击并在每次状态变化后截图。
- 本轮性质：只审计、分析和形成建议，不修改业务代码，不发布，不打包。
- 共执行 94 个按钮/状态动作，生成 94 张逐动作截图；另外生成 18 张最大化、左右半屏和还原窗口的重点复测截图。
- Connector、SQL API、流程保存/发布/运行、节点测试、运行记录及重试的业务请求均可完成。
- Console error：0；Page error：0；Request failure：0。
- 发现 3 个高优先级交互问题、3 个中优先级体验问题。用户反馈的“提示位置错误、内容堆叠”已稳定复现，并已定位到明确代码根因。

总体判断：接口编排中心的基础业务闭环已经存在，但流程设计器的浮层状态管理和消息反馈机制不满足可用性要求。问题集中在前端交互层，不是接口执行失败。

## 2. 环境与证据

### 2.1 可见 Chrome 环境

| 窗口状态 | `outerWidth x outerHeight` | `innerWidth x innerHeight` | DPR | 页面级横向溢出 |
| --- | --- | --- | --- | --- |
| 最大化 | `1920 x 1032` | `1920 x 945` | 1 | 无 |
| 左半屏 | `960 x 1032` | `944 x 937` | 1 | 无 |
| 右半屏 | `960 x 1032` | `944 x 937` | 1 | 无 |
| 还原窗口 | `960 x 1032` | `944 x 937` | 1 | 无 |

屏幕参数：`1920 x 1080`，可用工作区 `1920 x 1032`。

页面级没有意外横向溢出；流程画布自身的横向和纵向滚动属于无限画布的预期行为。半屏时系统顶部模块导航会出现横向滚动条，功能仍可访问，但当前模块定位感较弱。

### 2.2 证据目录

- 逐按钮截图：`artifacts/128-releases/20260801-api-orchestration-button-audit/chrome/`
- 多窗口重点截图：`artifacts/128-releases/20260801-api-orchestration-button-audit/layout-states/`
- 结构化结果：`artifacts/128-releases/20260801-api-orchestration-button-audit/button-audit-results.json`
- 审计脚本：`artifacts/128-releases/20260801-api-orchestration-button-audit/audit-buttons.js`

## 3. 问题清单

### F-01 高：消息定位类被通用方法删除，提示压入画布工具栏

**复现步骤**

1. 进入“流程设计”。
2. 点击“新建”。
3. 点击“自动排布”。
4. 再点击缩放、适配、节点或其他按钮。

**实际结果**

- 绿色提示“已按执行顺序重新排布，保存后生效。”出现在画布顶部。
- 提示与“添加节点 / 自动排布 / 分支类型”工具栏发生实际重叠。
- 提示不会自动消失，执行后续动作时仍长期保留。
- 最大化、左半屏、右半屏和还原窗口均可复现。

**关键证据**

- [056-workflow-auto-layout.png](../artifacts/128-releases/20260801-api-orchestration-button-audit/chrome/056-workflow-auto-layout.png)
- [057-workflow-zoom-out.png](../artifacts/128-releases/20260801-api-orchestration-button-audit/chrome/057-workflow-zoom-out.png)
- [082-workflow-auto-layout-base.png](../artifacts/128-releases/20260801-api-orchestration-button-audit/chrome/082-workflow-auto-layout-base.png)
- [left-half-06-auto-layout-message.png](../artifacts/128-releases/20260801-api-orchestration-button-audit/layout-states/left-half-06-auto-layout-message.png)
- [restored-06-message-after-4s.png](../artifacts/128-releases/20260801-api-orchestration-button-audit/layout-states/restored-06-message-after-4s.png)

**代码根因**

`ui.html` 为提示元素定义了结构类：

```html
<div id="apiWorkflowMessage" class="message orchestration-workflow-message"></div>
```

但通用消息函数执行时直接覆盖整个 `className`：

```javascript
function setMessage(id, text, type = "") {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  el.className = `message ${type}`;
}
```

运行后真实 DOM 变成 `class="message ok"`，`orchestration-workflow-message` 被删除。浏览器实测计算样式由预期的 `position:absolute; z-index:7; right:12px; bottom:12px` 退化为：

```text
position: static
z-index: auto
right/bottom: auto
width: 1376px
```

因此这不是单纯 CSS 参数不合适，而是共享状态函数破坏结构类。该写法也可能影响项目内其他带专用结构类的消息元素，修复前应全局检索 `setMessage()` 的调用对象。

**合理性判断**：不合理。状态颜色可以变化，定位和布局类不应被状态更新方法删除。

### F-02 高：运行输入面板位于节点工作台后方，关闭按钮不可点击

**复现步骤**

1. 点击“新建”，系统自动打开开始节点工作台。
2. 点击顶部 `Input`。
3. 尝试关闭运行输入。

**实际结果**

- `Input` 的 `aria-expanded` 已变为 `true`，运行输入面板也已打开。
- 节点工作台使用 `z-index:16` 全画布覆盖；运行输入仅为 `z-index:9`。
- 用户看到的是节点工作台，无法操作后方 Input 面板。
- 对运行输入关闭按钮中心点做命中测试，实际命中的是节点工作台的 `Schema` 区域。
- 自动化正常点击被前层拦截，只有强制事件才能关闭。

**关键证据**

- [038-workflow-new.png](../artifacts/128-releases/20260801-api-orchestration-button-audit/chrome/038-workflow-new.png)
- [039-workflow-input-open.png](../artifacts/128-releases/20260801-api-orchestration-button-audit/chrome/039-workflow-input-open.png)
- [040-workflow-input-close-forced-because-inspector-overlays.png](../artifacts/128-releases/20260801-api-orchestration-button-audit/chrome/040-workflow-input-close-forced-because-inspector-overlays.png)
- [left-half-03-input-behind-inspector.png](../artifacts/128-releases/20260801-api-orchestration-button-audit/layout-states/left-half-03-input-behind-inspector.png)
- [right-half-03-input-behind-inspector.png](../artifacts/128-releases/20260801-api-orchestration-button-audit/layout-states/right-half-03-input-behind-inspector.png)

**代码根因**

- `newApiWorkflow()` 固定设置 `apiOrchestrationInspectorOpen = true`。
- `setApiWorkflowRunInput(true)` 只关闭节点库，不关闭节点工作台。
- 节点工作台 `z-index:16`，运行输入面板 `z-index:9`。

**合理性判断**：不合理。按钮反馈与用户可操作状态不一致，属于功能不可达问题。

### F-03 高：节点库与运行输入可同时打开，左右面板共同挤占画布

**复现步骤**

1. 打开 `Input`。
2. 再点击“添加节点”。

**实际结果**

- 左侧节点库与右侧运行输入同时存在。
- 半屏下两块面板几乎占据全部有效画布，节点和连线操作被遮挡。
- `setApiWorkflowRunInput(true)` 会关闭节点库，但 `setApiWorkflowNodeLibrary(true)` 不会反向关闭运行输入，互斥规则是单向的。

**关键证据**

- [042-workflow-node-library-open.png](../artifacts/128-releases/20260801-api-orchestration-button-audit/chrome/042-workflow-node-library-open.png)
- [left-half-05-library-and-input.png](../artifacts/128-releases/20260801-api-orchestration-button-audit/layout-states/left-half-05-library-and-input.png)
- [right-half-04-library-and-input.png](../artifacts/128-releases/20260801-api-orchestration-button-audit/layout-states/right-half-04-library-and-input.png)

**合理性判断**：不合理。节点库、运行输入、节点工作台属于同一画布上的任务面板，应由统一状态机控制互斥。

### F-04 中：新建流程立即进入开始节点工作台，画布反馈被遮住

点击“新建”后，系统已经创建开始/结束节点并提示“已创建空白流程”，但同时自动打开开始节点工作台。用户第一眼看不到新建后的画布，也无法自然进入添加节点流程。

**关键证据**：[038-workflow-new.png](../artifacts/128-releases/20260801-api-orchestration-button-audit/chrome/038-workflow-new.png)

**合理性判断**：业务上可解释，但交互不合理。新建后应展示完整画布并聚焦开始节点；只有用户再次点击节点时才打开工作台。

### F-05 中：成功提示没有生命周期，4 秒后仍残留

自动排布后等待 4 秒，提示仍然存在；后续缩放、适配、打开节点、删除节点时也继续显示旧操作结果。顶部已经有“未保存”状态，因此长期保留“保存后生效”属于重复且过期的反馈。

**合理性判断**：不合理。成功提示应 3 至 5 秒自动消失；错误提示才应保持到问题处理或用户关闭。

### F-06 中：半屏布局功能可用，但信息层级和画布面积不足

- 编辑栏在 `<=1280px` 时分两行，按钮未溢出，属于合理响应式处理。
- 系统全局模块导航出现横向滚动，当前模块仍高亮，但用户不容易感知左右还有多少模块。
- 流程列表固定占据约 210px，画布工具栏又固定占用一行；叠加双浮层后有效画布几乎消失。

**合理性判断**：编辑栏换行合理；全局导航和画布侧栏需要进一步适配半屏工作场景。

## 4. 逐功能合理性报告

### 4.1 全局入口与页签

| 功能 | 截图 | 结果 | 合理性 |
| --- | --- | --- | --- |
| 全局刷新 | `002` | 数据重新加载，无请求失败 | 合理 |
| 连接器页签 | `001`、`094` | 可进入并恢复列表/编辑区 | 合理；会记忆上次子页签属于可接受行为 |
| SQL API 页签 | `025` | 可进入 SQL 工作台 | 合理 |
| 流程设计页签 | `037`、`091` | 可进入并恢复流程 | 合理 |
| 运行中心页签 | `087` 前置状态 | 可进入运行列表和详情 | 合理 |

### 4.2 Connector

| 功能点 | 截图 | 测试结果 | 合理性判断 |
| --- | --- | --- | --- |
| 新建 | `003` | 表单清空并进入新建状态 | 合理 |
| Params / Authorization / Headers / Body / Input / Success | `004`、`009`、`010`、`015`、`017`、`019` | 六个页签均可切换 | 合理 |
| Query Bulk Edit 开/关 | `005`、`006` | 状态可切换 | 合理 |
| Query 参数新增/删除 | `007`、`008` | 行可增删，布局稳定 | 合理 |
| Header Bulk Edit 开/关 | `011`、`012` | 状态可切换 | 合理 |
| Header 新增/删除 | `013`、`014` | 行可增删，布局稳定 | 合理 |
| Body Beautify | `016` | JSON 格式化有反馈 | 合理 |
| Input Beautify | `018` | JSON 格式化有反馈 | 合理 |
| 保存 | `021` | 隔离连接器创建成功 | 合理 |
| 发送测试 | `022` | 健康接口调用成功并显示结果 | 合理 |
| 列表编辑 | `023` | 已保存值正确回填 | 合理 |
| 列表删除 | `024` | 确认后删除成功 | 合理 |

Connector 本轮未发现阻断问题。

### 4.3 SQL API

| 功能点 | 截图 | 测试结果 | 合理性判断 |
| --- | --- | --- | --- |
| 新建 | `026` | 清空并进入 SQL API 新建状态 | 合理 |
| 连接树展开/收起 | `027`、`028` | 状态正确变化 | 合理 |
| Input Schema 页签 | `029` | 可切换 | 合理 |
| Schema Beautify | `030` | JSON 格式化成功 | 合理 |
| SQL 页签 | `031` | 可返回 SQL 编辑区 | 合理 |
| 测试输入格式化 | `032` | JSON 格式化成功 | 合理 |
| 保存 | `033` | 隔离 SQL API 创建成功 | 合理 |
| 运行 | `034` | 参数化 SQL 执行并返回结果 | 合理 |
| 列表编辑 | `035` | 已保存定义正确回填 | 合理 |
| 列表删除 | `036` | 确认后删除成功 | 合理 |

SQL API 本轮未发现按钮失效或布局阻断。

### 4.4 流程设计

| 功能点 | 截图 | 测试结果 | 合理性判断 |
| --- | --- | --- | --- |
| 新建 | `038` | 创建开始/结束节点，但自动打开工作台 | 功能成功，交互需改进，见 F-04 |
| Input 打开/关闭 | `039`、`040` | 在工作台打开时不可正常关闭 | 不合理，见 F-02 |
| 关闭节点工作台 | `041` | 可返回画布，Input 仍留在后方 | 单按钮有效，跨面板状态不合理 |
| 节点库打开/关闭 | `042`、`043` | 可开关，但可与 Input 并存 | 不合理，见 F-03 |
| 添加 HTTP 节点 | `044` 至 `046` | 添加成功并进入节点工作台 | 合理 |
| 添加 SQL API 节点 | `047` 至 `049` | 添加成功并进入节点工作台 | 合理 |
| 添加数据映射节点 | `050` 至 `052` | 添加成功并进入节点工作台 | 合理 |
| 添加条件判断节点 | `053` 至 `055` | 添加成功并进入节点工作台 | 合理 |
| 自动排布 | `056`、`082` | 节点位置正确变化，提示位置和生命周期错误 | 核心功能合理，反馈不合理，见 F-01/F-05 |
| 缩小/放大/适配 | `057` 至 `059` | 缩放值和画布状态正确变化 | 合理；旧提示不应继续残留 |
| 打开 HTTP 节点 | `060` | 进入 HTTP 节点工作台 | 合理 |
| HTTP 节点测试 | `061` | 测试成功并显示状态 | 合理 |
| 返回画布 | `062` | 正常关闭工作台 | 合理 |
| 映射节点数据选择器 | `063` 至 `066` | 打开、选择视图、关闭均可用 | 合理 |
| 条件节点数据选择器 | `067` 至 `070` | 打开、选择视图、关闭均可用 | 合理 |
| 删除条件节点 | `071`、`072` | 节点和关联边同步删除 | 合理 |
| 删除映射节点 | `073`、`074` | 节点和关联边同步删除 | 合理 |
| 删除 SQL API 节点 | `075`、`076` | 节点和关联边同步删除 | 合理 |
| 删除 HTTP 节点 | `077`、`078` | 节点和关联边同步删除 | 合理 |
| 开始节点删除保护 | `079`、`080` | 删除按钮正确隐藏 | 合理 |
| 关闭开始节点工作台 | `081` | 可返回画布 | 合理 |
| 保存流程 | `083` | 隔离流程创建成功 | 合理 |
| 流程列表选择 | `084` | 保存内容正确回填 | 合理 |
| 发布 | `085` | 修订号和状态更新 | 合理 |
| 运行 | `086` | 创建运行记录并进入运行中心 | 合理 |
| 选择后删除流程 | `091` 至 `093` | 确认后删除成功 | 合理 |

### 4.5 运行中心

| 功能点 | 截图 | 测试结果 | 合理性判断 |
| --- | --- | --- | --- |
| 刷新 | `087` | 运行状态和列表重新加载 | 合理 |
| 选择运行记录 | `088` | 详情与节点轨迹显示 | 合理 |
| 展开节点日志 | `089` | 节点输入、输出和状态可查看 | 合理 |
| 重新执行 | `090` | 创建新的重试运行记录 | 合理 |

运行中心本轮未出现 500、空白详情或按钮无响应。

## 5. 总体改造建议

### 第一优先级：统一画布面板状态机

只允许以下状态之一存在：

```text
canvas | node-library | run-input | node-inspector | data-picker
```

打开任一状态时必须关闭其他状态；禁止由每个按钮各自维护部分互斥逻辑。建议提供统一入口，例如 `setWorkflowSurfaceMode(mode)`，同步更新 `hidden`、`aria-expanded`、焦点恢复和 ESC 行为。

### 第一优先级：修复消息组件的结构类保护

- `setMessage()` 只能增删状态类，例如 `ok/error`，不能覆盖整个 `className`。
- 结构类由 HTML 持久保留。
- 流程画布成功信息改为固定区域 toast，避开顶部工具栏和底部缩放控件。
- 全局排查所有 `setMessage()` 目标，确认是否还有结构类被误删。

### 第二优先级：建立消息生命周期

- 成功：3 至 5 秒自动消失。
- 普通信息：可自动消失或由下一动作替换。
- 错误：保持显示，并提供关闭按钮。
- “未保存”只由顶部状态栏持续表达，不再让旧 toast 长期重复。
- 新消息替换旧消息，不允许多个提示堆叠。

### 第二优先级：调整新建和节点操作流程

- 新建后展示完整开始/结束画布，不自动打开开始节点工作台。
- 新节点添加后可打开节点工作台，这是用户明确触发后的合理行为。
- 关闭工作台后恢复到原画布滚动位置，并把焦点返回被点击节点。
- 自动排布后执行一次 `fit` 或至少保证全部节点位于当前可视区域。

### 第三优先级：半屏工作区优化

- 流程列表支持折叠，半屏默认缩窄或自动折叠。
- 顶部操作栏保持两行，但流程名称/描述和状态之间应明确主次。
- 节点库在半屏可改为窄抽屉；节点工作台维持单独工作模式，不与其他浮层共存。
- 全局模块导航保留横向滚动时增加边缘渐隐或左右导航按钮，提示还有隐藏模块。

## 6. 建议验收标准

1. 新建流程后直接看到完整画布，节点工作台默认关闭。
2. 节点库、运行输入、节点工作台、数据选择器在任意顺序点击时始终互斥。
3. 所有浮层关闭按钮都能通过普通点击命中，不允许使用强制点击通过测试。
4. 自动排布提示不遮挡工具栏、节点、连线、缩放控件。
5. 成功提示 3 至 5 秒消失；错误提示保持并可关闭。
6. 自动排布后顶部持续显示“未保存”，保存后变为已保存状态。
7. 最大化、左半屏、右半屏、还原窗口均无页面级横向溢出和不可达按钮。
8. 逐按钮回归继续保持 Console error、Page error、Request failure 均为 0。
9. Connector、SQL API、流程保存/发布/运行/删除、运行详情/重试全部执行真实闭环。

## 7. 截图编号索引

- `001-024`：连接器基线、刷新、各编辑页签、Bulk Edit、参数增删、格式化、保存、发送、编辑、删除。
- `025-036`：SQL API 页签、新建、连接树、Schema/SQL 页签、格式化、保存、运行、编辑、删除。
- `037-043`：流程页签、新建、Input、工作台关闭、节点库开关。
- `044-055`：HTTP、SQL API、数据映射、条件判断四类节点的添加与工作台关闭。
- `056-062`：自动排布、缩放、适配、HTTP 节点打开、测试、返回画布。
- `063-070`：映射与条件节点的数据选择器。
- `071-081`：四类普通节点删除、开始节点删除保护、关闭工作台。
- `082-086`：基础自动排布、保存、列表选择、发布、运行。
- `087-090`：运行刷新、选择、节点日志展开、重试。
- `091-094`：返回流程、选择流程、删除流程、返回连接器。
- `layout-states/left-half-*`：左半屏 7 个重点状态。
- `layout-states/right-half-*`：右半屏 5 个重点状态。
- `layout-states/restored-*`：还原窗口 6 个重点状态，其中包含提示 4 秒后仍存在的证据。

## 8. 测试资产与清理

审计过程中创建了隔离资产：

- Connector：`d1a67c8d-d1fd-4b03-a537-38b75dbb2882`
- SQL API：`fc360909-aeaa-4b06-81b4-fc8a9fe5bff8`
- Workflow：`4df17e2c-015c-4fb7-a193-7f54ea7e1387`
- Run：`31252738-36c2-4a2b-95fa-a1475d634e56`
- Retry Run：`a90e1cde-d661-435f-a130-0991d223e899`

Connector、SQL API 和 Workflow 已通过页面删除。运行记录按运行中心审计语义保留，用于核对原始运行和重试链路。

## 9. 2026-08-04 整改复核附录

本报告确认的消息定位、提示生命周期、面板互斥、新建默认画布、自动排布适配和节点单击/拖动分离问题已完成整改。硬刷新后在 128 可见 Chrome 中复核：最大化、左半屏、右半屏和还原窗口均可访问连接器、流程设计、SQL API、运行中心；页面级横向溢出为 0，正向操作后的 `pageerror` 和非预期 console error 为 0。

本轮真实闭环包含连接器创建/保存/测试/停用/启用/搜索/删除、SQL API 创建/保存/参数化查询/停用/启用/搜索/删除、流程保存/发布/运行/节点日志，以及离线开发和数据变化画布 5px 拖动阈值。截图和结构化验证记录见 [`RELEASE_VALIDATION_20260804_AUDIT_REMEDIATION.md`](RELEASE_VALIDATION_20260804_AUDIT_REMEDIATION.md)。
