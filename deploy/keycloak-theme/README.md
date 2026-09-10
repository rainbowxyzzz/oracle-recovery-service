# Keycloak 统一身份主题

`unified-identity` 是 Keycloak 24 的登录主题，仅追加样式和中文消息，不覆盖认证模板或协议逻辑。

部署时将 `unified-identity` 目录复制到 Keycloak 的 `/opt/keycloak/themes/`，并在 `oracle-recovery` Realm 设置：

- `Login theme`: `unified-identity`
- `Display name`: `统一身份与权限认证`

生产环境应保持 Keycloak 主题缓存开启；更新主题后重启 Keycloak 以刷新缓存。
