# 2026-09-07 顶部 Mega Menu 发布验证

## 范围与方式

- 模式：发布验证（模式二），仅热更新 `oracle-recovery-api` 容器内的 `static/ui.html` 与 `static/ui-unified.css`。
- 不重建镜像、不重启 API / Worker / MySQL / Redis、不执行迁移、不生成完整 Docker Run 包。
- 变更只覆盖顶部业务域 Mega Menu 的静态布局、交互动画与既有模块入口接入；不改变 API、请求、保存、任务或权限语义。

## 发布前保护

- API 健康检查返回 `status=ok`，MySQL 连接成功。
- 9 个 Celery Worker 的 `active`、`reserved`、`scheduled` 均为空。
- 更新前文件备份位于：`/root/codex-release/20260907-mega-navigation-r1/backup/`。
- 候选文件与更新前 128 文件逐段对比，差异只包含本轮 Mega Menu 实现，未覆盖 OIDC 已发布改动。

## 发布结果

- 线上候选与 API 容器内文件 SHA-256 一致：
  - `ui.html`：`d8edb1fed250bb979a740240c3a3052f3db28ad99841a430095caa9c2d7029e6`
  - `ui-unified.css`：`af60ec91240b03aec0433151342563f2627f6113b0363208012faac128ff2813`
- `GET /ui?megaNavigation=20260907r1` 与 `GET /static/ui-unified.css?megaNavigation=20260907r1` 均返回 HTTP 200，且包含覆盖面板、动画和 `showModule(moduleId)` 接入标识。
- API 发布后健康检查仍返回 `status=ok`，`RestartCount=0`；近 20 分钟 API 日志未发现 `Traceback`、`CRITICAL` 或 `Internal Server Error`。

## 本地验证

- 前端内联脚本语法与 Phase 3 接入检查通过。
- `python -m pytest tests --ignore=tests/test_microservice_modes.py`：`323 passed, 1 skipped`。
- `git diff --check` 通过。

## 限制

- 128 可见浏览器自动化在读取页面时两次超时并重置，未将 hover、锁定、模块点击和多窗口视觉检查标记为通过。线上 HTML/CSS 资源与容器文件已验证，但仍建议后续在浏览器中完成该项人工验收。

## 二次视觉修正（r2）

- 删除无功能的 Global bar 占位文字，以及与全部模块卡片重复的“常用入口”。
- Mega Menu 改为全宽白色底板和居中双栏内容，移除居中浮层的左右突出边与外壳阴影。
- 更新前 9 个 Worker 的 `active`、`reserved`、`scheduled` 均为空；API 健康和 MySQL 连接正常。
- 备份位于：`/root/codex-release/20260907-mega-navigation-layout-r2/backup/`。
- 候选与 API 容器内文件 SHA-256 一致：
  - `ui.html`：`3ec1c4ab72651327a50443da09abf70501dd17a26b9fa44212a534a51266f491`
  - `ui-unified.css`：`90287e4f636356fbf7fe95fa8b5795780996054ba4a8a1270c3878e215e4abac`
- 线上 `/ui?megaNavigation=20260907r2` 和样式资源均返回 HTTP 200；已确认页面不含“常用入口”、`mega-menu-quick`、资源中心或帮助文档等已移除占位项。API `RestartCount=0`，近期未发现关键错误。
