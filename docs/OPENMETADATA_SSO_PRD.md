# OpenMetadata 统一认证 PRD

## 目标

当前系统和 OpenMetadata 使用同一个 OIDC 身份源。用户通过统一认证进入当前系统后，打开 OpenMetadata 原生目录时复用身份源会话，不再要求再次输入 OpenMetadata 的独立账号密码。

## 规则

- 当前系统原有用户名密码登录保留，作为兼容回退；它不会伪造 OIDC 会话。
- 统一认证使用 Authorization Code flow。当前系统后端保存 OIDC client secret，浏览器不接触 OpenMetadata 服务端 PAT 或 client secret。
- OIDC 回调必须校验短时签名 `state`、同源状态 Cookie、授权码和用户信息；登录后只写入短时 HttpOnly 会话 Cookie，再由同源页面换取当前系统 JWT。
- 默认只允许 OIDC 用户映射到已有当前系统用户，禁止静默创建管理员。`OIDC_AUTO_PROVISION_USERS=true` 时新用户只能以 `viewer` 角色创建。
- 当前系统权限仍由本地用户角色和权限表控制；OpenMetadata 权限由 OpenMetadata 的角色与 authorizer 控制，两者共享身份，不共享权限令牌。
- OpenMetadata 的数据同步继续使用服务端 PAT；PAT 不得放入页面、URL、localStorage 或 OIDC 回调参数。

## 配置

- 当前系统：`OIDC_ENABLED`、`OIDC_ISSUER_URL`、`OIDC_CLIENT_ID`、`OIDC_CLIENT_SECRET`、`OIDC_REDIRECT_URI`。
- OpenMetadata：`AUTHENTICATION_PROVIDER=custom-oidc`、`AUTHENTICATION_AUTHORITY`、`AUTHENTICATION_CLIENT_ID`、`AUTHENTICATION_CALLBACK_URL`、`AUTHENTICATION_PUBLIC_KEYS` 和对应 OIDC discovery/client 配置。
- 128 使用 Keycloak realm `oracle-recovery` 作为测试身份源；正式环境应替换为企业已有 Keycloak、Entra ID、Okta 或其他 OIDC 提供方，并修改回调地址和密钥。

## 验收

1. 未配置 OIDC 时，原有本地登录和 OpenMetadata 服务端同步不受影响。
2. 配置 OIDC 后，当前系统登录页显示“统一认证登录”。
3. 统一认证回调能映射已有本地用户，并恢复当前系统权限。
4. OpenMetadata 使用同一身份源完成登录，浏览器不再显示 OpenMetadata Basic 登录页。
5. 禁用用户、未绑定用户、篡改 state、过期 state 和无效授权码均被拒绝。
