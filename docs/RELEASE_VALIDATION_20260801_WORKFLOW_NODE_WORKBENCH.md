# 接口编排节点工作台发布验证记录

验证日期：2026-08-01

## 1. 范围与发布方式

- 范围：接口编排中心流程设计前端交互，不修改后端接口、数据库模型、迁移、权限、发布快照或 Worker。
- 发布方式：Windows 本地开发与测试后，最小热更新到 `192.168.150.128`。
- 未执行：镜像构建、容器重启、数据库迁移、Docker Run 打包和回滚演练。
- 128 更新前备份：`/root/codex-backups/20260801-n8n-node-workbench-r1`。

## 2. n8n 实机校准

- 本地参考镜像：`sha256:cb31f87d5a8f72e3e3f1e9fb7fc89e08dd8564e35cbba349e1fac433b7e41d85`。
- n8n 版本：`1.88.0`。
- 隔离容器：`codex-n8n-ui-reference`。
- 本地地址：`http://127.0.0.1:5678`。
- 参考流程：Manual Trigger -> HTTP Request -> HTTP Request。
- 实机行为：把第一节点的 `args.token` 从左侧 Schema 树拖到第二节点 `Authorization` 值后，自动进入 Expression 并生成 `{{ $json.args.token }}`；字段下方即时显示解析结果，第二节点 Output 显示实际收到的 Header。
- n8n 容器和数据卷只作本地参考，不进入项目、128 或部署包。

## 3. 变更文件与哈希

| 文件 | SHA256 |
|---|---|
| `src/recovery_service/static/ui.html` | `01828d50145d62d08267ff8758a4adaa8ce9d0aa6c9f195fa05a579e9583f143` |
| `src/recovery_service/static/vendor/lucide.min.js` | `9723deed8adf7dbc3a2579927d7b724e44001ba12280025d66defc57d1cd8316` |
| `src/recovery_service/static/vendor/LUCIDE_LICENSE.txt` | `b495047bd93a9b06913511076f504daba17d5bbeb3e0650f3bb53a4220329c57` |

128 容器内哈希与本地一致。Lucide 为 `1.28.0`，许可证为 ISC，静态资源不带 UTF-8 BOM。

## 4. 自动化验证

- 内联 JavaScript：`UI_INLINE_SCRIPT_SYNTAX_OK`。
- 接口编排与画布专项：`27 passed`。
- 完整测试：`182 passed, 1 skipped`。
- 新增契约覆盖：三栏工作台、Input/Output 模式、固定值/表达式、字段拖拽、自定义 MIME、解析预览、本地 Lucide 和许可证。

## 5. 128 真实业务闭环

### 5.1 Token 参数传递

1. 打开 `DEMO_FLOW_TOKEN_MAPPING`。
2. 流程输入使用隔离值 `demo-user/demo-pass`，密码只存在当前浏览器内存。
3. 登录节点测试成功，Output 的 `body.data.token` 显示为 `******`。
4. 将 `nodes.http-1785490911940-4823.body.data.token` 从 Input 拖到下游 `token` 参数。
5. 页面生成 `{{ nodes.http-1785490911940-4823.body.data.token }}`，解析结果保持脱敏。
6. 下游节点测试成功并返回 2 条模拟列表数据，页面和持久化流程均无真实 Token。
7. 保存后刷新回读表达式一致，流程发布为 R4。
8. 完整运行 ID `a4ebe456bf59423c9c8b5b5abc6db7ae` 为 `succeeded`，4 个节点全部成功。

### 5.2 复用模块

- 连接器 `DEMO_HTTP_系统健康检查`：打开旧值、发送 HTTP 200、原值保存 PUT 200、刷新回读启用状态正常。
- SQL API `DEMO_SQL_服务器时间`：真实查询成功，返回 MySQL 服务器时间，原值保存 PUT 200。
- Mapping 节点：三栏工作台、模板 JSON 和拖拽接收区存在且可操作。
- Condition 节点：三栏工作台、路径输入和字段拖拽接收区存在且可操作。
- SQL API 节点：三栏工作台、SQL API 绑定和固定值/表达式映射控件正常。
- 运行中心：R4 运行状态、4 个逐节点状态和耗时均可查看。

## 6. 可见 Chrome 验收

| 窗口状态 | Outer | Inner | 页面横向溢出 |
|---|---:|---:|---|
| 最大化 | `1920x1032` | `1920x945` | 无 |
| 左半屏 | `960x1032` | `944x937` | 无 |
| 右半屏 | `960x1032` | `944x937` | 无 |
| 还原 | `1440x850` | `1424x755` | 无 |

- 最大化三栏宽度约为 `510 / 539 / 567px`。
- 半屏三栏宽度约为 `263 / 337 / 277px`。
- 左半屏首次检查发现旧顶部栏挤压状态文字，已改为元数据和操作按钮两行并复验通过。
- 四种窗口均无工作台遮挡、不可点击控件或页面级横向滚动。
- Chrome console error：0；`pageerror`：0。

截图位于 `artifacts/128-releases/20260801-workflow-node-workbench-r1`：

- `chrome-maximized.png`
- `chrome-left-half-r2.png`
- `chrome-right-half.png`
- `chrome-restored.png`
- `chrome-token-drag-output-success.png`

## 7. 服务状态

- API `/api/v1/health`：`status=ok`，MySQL 连接成功。
- `api_orchestration` Redis 队列：0。
- `api_orchestration_runs` queued/running：0。
- API 与接口编排 Worker 最近 20 分钟无 `ERROR`、`Traceback` 或 `Exception`。
