# 2026-07-31 流程可视化数据映射与动态 Token 热更新验证

## 1. 发布边界

本次为 128 最小热更新，不生成 Docker Run 包，不修改数据库结构，不切换其他业务 Worker。更新范围：

- API：流程设计 UI、连接器请求 Schema、接口编排调用服务。
- API Orchestration Worker：接口编排调用服务。
- 测试环境：`/tmp/token-flow-mock-20260731.py`，只用于登录和动态 Bearer 列表接口验证，不进入源码镜像和后续部署包。

更新前确认接口编排 Redis 队列为 0，8 个 Celery Worker 均无 active/reserved 任务。原文件备份位于：

```text
/root/codex-backups/20260731-workflow-data-mapping-r1
```

## 2. 文件与哈希

```text
ui.html                           815a38a9ed79b1753634d1b5754cd500c04fdfbf7ddca5e8bd19e867471dbe4f
api/schemas/api_orchestration.py ae6e8b250a2253c26b91fd58307b6a84c17483cec649aec5aa8695fd92f05d1e
services/api_orchestration.py    56c9db1a85d7c4ef61665247f89137bcb7df2a2ecd11d90b5edcf82bc36916d1
```

API 与 Worker 容器内哈希和本地源码一致。

## 3. 自动化验证

```text
接口编排专项：24 passed
完整测试：181 passed, 1 skipped
内联 JavaScript：UI_INLINE_SCRIPT_SYNTAX_OK
```

覆盖动态 Bearer Schema、运行时 Token 注入、缺少 Token 报错、原静态认证、映射节点、SQL API、画布和前端契约。

## 4. 可见 Chrome 闭环

保留资产：

| 类型 | 名称 | ID |
|---|---|---|
| 登录连接器 | `DEMO_TOKEN_FLOW_登录` | `4caa437a-0294-4ffc-a9d9-d5ba627c65fa` |
| 列表连接器 | `DEMO_TOKEN_FLOW_获取列表` | `90627803-4194-4342-90de-66ed6a12f558` |
| 流程 | `DEMO_FLOW_TOKEN_MAPPING` | `3d5cfd2d-b295-41cc-a5af-07df7e0dd43b` |
| 成功运行 | `DEMO_FLOW_TOKEN_MAPPING` R2 | `ea4ed2bc-0fa6-4829-84df-7ae0003b4c5c` |

闭环结果：

1. 运行输入 `username/password` 通过数据选择器绑定到登录节点。
2. 登录节点返回 `body.data.token`，数据树将其标记为敏感字段并显示 `******`。
3. 第二节点映射为 `{{ nodes.http-1785490911940-4823.body.data.token }}`，动态 Bearer 调用成功。
4. 列表接口返回 owner=`demo-user` 与 2 条记录；完整流程 4 个节点全部 succeeded。
5. 流程保存数据不包含 `demo-pass` 或真实 Token；运行日志中的 password/token 均脱敏。
6. 刷新回读后映射令牌和 `output_schema` 保持，真实节点测试值按设计清除。

## 5. 布局与终检

| 窗口 | outer | inner | 根级溢出 | 属性面板 |
|---|---|---|---|---|
| 最大化 | `1920x1032` | `1920x945` | 无 | 340px，完整可见 |
| 左半屏 | `960x1032` | `944x937` | 无 | 360px，完整可见 |
| 右半屏 | `960x1032` | `944x937` | 无 | 360px，完整可见 |
| 还原 | `1280x850` | `1264x755` | 无 | 360px，完整可见 |

```text
Chrome console error=0
Chrome pageerror=0
API health=ok
API recent fatal errors=0
Worker recent fatal errors=0
api_orchestration queue=0
```

截图位于：

```text
artifacts/128-releases/20260731-workflow-data-mapping-r1/
```

## 6. 已知边界

- 当前实现单次流程内 Token 传递，不包含跨运行缓存、TTL、401/403 自动刷新或失败节点自动重试。
- 节点测试真实调用目标连接器；测试样例值只保存在当前浏览器会话，刷新后需要重新填写运行输入并测试上游节点。
- 测试 Mock 只存在于 128 当前测试环境，不进入任何镜像或部署包。
