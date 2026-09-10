# 2026-09-07 OIDC 全局安全退出发布验证

## 范围

- 发布方式：128 最小热更新，重启 `oracle-recovery-api`；未更新或重启 Worker。
- 修复范围：平台会话清理、Keycloak RP-Initiated Logout、OIDC 登录回调的短时 `id_token` Cookie、退出页前端跳转。
- Keycloak Client：仅新增精确的 Valid Post Logout Redirect URI：`http://192.168.150.128:8000/ui`。

## 发布前检查

- API 健康检查正常。
- 9 个 Celery Worker 在线；`active` 与 `reserved` 均为空。
- 已备份 API 容器内的 `auth.py`、`oidc.py`、`settings.py` 和 `ui.html` 至：`/root/codex-release/20260907-oidc-global-logout/backup`。

## 发布与验证

1. 将上述 4 个运行时文件热更新到 `oracle-recovery-api`，随后仅重启该 API 容器。
2. `POST /api/v1/auth/oidc/logout` 返回 Keycloak discovery 的 `end_session_endpoint`，并删除 `ors_oidc_session`、`ors_oidc_id_token` 与 `ors_oidc_state` Cookie。
3. 在携带 `ors_oidc_id_token` Cookie 的接口验证中，生成的注销地址包含 `id_token_hint`。
4. Keycloak 对注销地址返回 `302` 至 `/ui`，未出现无效参数或无效回跳地址。
5. API 发布后健康检查正常，容器处于 `running`，`RestartCount=0`，最近日志未发现异常关键字。

## 限制

- 本次未在自动化工具中提交用户凭据。浏览器自动化读取既有 128 标签页超时，因此未将交互式“输入账号密码 → 点击退出 → 再次登录”列为已自动通过；该流程应由当前登录用户按验收项完成一次人工确认。
