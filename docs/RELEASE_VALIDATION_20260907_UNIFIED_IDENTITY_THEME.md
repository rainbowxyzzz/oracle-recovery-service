# 2026-09-07 统一身份与权限认证主题发布验证

## 范围

- 发布方式：128 Keycloak 最小热更新，仅更新 `unified-identity` 登录主题和 Realm 外观配置。
- Realm：`oracle-recovery`。
- 不修改 OIDC Client、API、Worker、数据库或任务数据。

## 发布内容

- 新增 Keycloak 24 主题 `unified-identity`，继承 `keycloak.v2`，只追加 CSS 和消息包，不覆盖 FreeMarker 模板。
- Realm `loginTheme` 设置为 `unified-identity`。
- Realm `displayName` 设置为“统一身份与权限认证”。
- 默认、英文和中文消息包均使用中文认证文案，避免浏览器语言回退为英文。

## 验证

1. 发布前 API 健康检查正常，9 个 Celery Worker 在线且无活动任务。
2. 已备份发布前 Realm 配置和登录页响应到：`/root/codex-release/20260907-unified-identity-theme/backup`。
3. Keycloak 重启后，授权入口 HTTP 200；HTML 的标题和页面标题为“统一身份与权限认证”，并包含“账号或邮箱”“密码”“登录”。
4. `unified-identity.css` 已被登录页引用，资源 HTTP 200。
5. 主题初次加载因目录符号链接与复制权限触发过 500；已改为直接主题目录并赋予 Keycloak 读取权限，最终日志无主题加载异常。
6. Keycloak 容器运行正常、`RestartCount=0`；API 健康检查仍正常。

## 运维说明

本次主题源码保留在仓库 `deploy/keycloak-theme/unified-identity` 和服务器发布目录中。若未来重建 Keycloak 容器，需要按 `deploy/keycloak-theme/README.md` 重新复制主题目录并恢复 Realm 的主题配置。
