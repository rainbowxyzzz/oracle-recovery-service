# 接口编排节点单击与拖动交互发布验证记录

验证日期：2026-08-01

## 1. 范围与版本核对

- 范围：接口编排中心流程画布前端事件，不修改后端接口、数据库、迁移、权限、Worker、流程 JSON 或执行语义。
- 本地与更新前 128 页面 SHA256 均为 `01828d50145d62d08267ff8758a4adaa8ce9d0aa6c9f195fa05a579e9583f143`。
- 最近完整镜像 `oracle-recovery-service-api:20260730-api-orchestration-dbeaver-sqlapi-canvas-r1` 内页面 SHA256 为 `10a2ea28002d8bab334b19f283e16f0f4affe9269f85e32752045a9ddf50fd4f`，属于热更新前旧页面；本次没有用旧镜像覆盖 128 已验证能力。
- 候选与更新后 128 页面 SHA256 均为 `0e99531e8cb54a89832afe78e1ac5faa2a259ca00b1d7ce2ed48940f359f64d3`。

## 2. 修改内容

- 节点 `pointerdown` 只建立选中和拖动候选状态，不打开节点工作台。
- 指针移动达到 `5px` 后开始更新节点坐标；阈值内移动不改变坐标。
- 普通 `click` 打开节点工作台；拖动完成后的合成点击在 `500ms` 窗口内按节点 ID 抑制。
- 输出端口连线、输入端口目标检测、自环/重复边/环路校验保持原逻辑。

## 3. 本地验证

- 接口编排专项：`26 passed`。
- 完整回归：`183 passed, 1 skipped`。
- 内联 JavaScript：`UI_INLINE_SCRIPT_SYNTAX_OK scripts=1`。
- 页面文件为 UTF-8 无 BOM、LF 行尾。

## 4. 128 发布与真实闭环

- 更新前接口编排 Redis 队列为 0，`api_orchestration_runs` 的 queued/running 记录为 0。
- 更新前备份：`/root/codex-backups/20260801-workflow-node-pointer-interaction-r1`。
- 发布方式：只向 `oracle-recovery-api` 容器热更新 `static/ui.html`；未重建镜像、未重启容器、未执行数据库迁移。
- `DEMO_FLOW_TOKEN_MAPPING` 的 HTTP 节点在鼠标按下且未松开时保持画布，松开后打开三栏工作台。
- `2px` 移动不改变 `left:445px;top:388px`；移动 `42px/24px` 后变为 `left:487px;top:412px`，工作台保持关闭，流程显示未保存。
- 输出端口按下后出现 1 个 pending 端口和 1 条 preview 连线，未误开工作台。
- 隔离流程 `CODEX_NODE_POINTER_E2E_20260801` 的 Start 节点从 `left:445px;top:60px` 拖到 `left:509px;top:92px`；保存返回 201，刷新回读坐标一致，删除返回 204，残留数量为 0。

## 5. 可见 Chrome

| 窗口状态 | Outer | Inner | 页面横向溢出 | 节点工作台 |
|---|---:|---:|---|---|
| 最大化 | `1920x1032` | `1920x945` | 无 | 通过 |
| 左半屏 | `960x1032` | `944x937` | 无 | 通过 |
| 右半屏 | `960x1032` | `944x937` | 无 | 通过 |
| 还原 | `1440x850` | `1424x755` | 无 | 通过 |

- `devicePixelRatio=1`，screen 为 `1920x1080`，可用工作区为 `1920x1032`。
- Chrome console error 为 0，`pageerror` 为 0。
- 截图位于 `artifacts/128-releases/20260801-workflow-node-pointer-interaction-r1`。

## 6. 终检

- API `/api/v1/health` 正常，MySQL 连接成功。
- 接口编排队列和在途运行保持为 0。
- API 与接口编排 Worker 近期无 `ERROR`、`Traceback`、`Internal Server Error` 或 `CRITICAL`。
- 本轮未生成 Docker Run 部署包；后续完整打包必须从当前已验证源码派生，不能让旧完整镜像内页面覆盖本次修复。
