# OpenMetadata 统一认证 PRD

## 目标

当前系统和 OpenMetadata 使用同一个 OIDC 身份源。用户通过统一认证进入当前系统后，打开 OpenMetadata 原生目录时复用身份源会话，不再要求再次输入 OpenMetadata 的独立账号密码。

## 规则

- 当前系统原有用户名密码登录保留，作为隐藏的应急回退；正常登录主入口必须使用 OIDC，不能以本地密码登录冒充 OIDC 会话。
- OIDC 身份源登录页对用户展示的产品名称固定为“统一身份与权限认证”，不得出现某一业务系统（例如 Oracle 恢复）的产品名；视觉采用当前工作台的深色顶栏、浅灰页面背景、白色表单卡片与蓝色主操作，不改变 Keycloak 的认证、错误提示、OTP、密码重置和会话协议行为。
- 统一认证使用 Authorization Code flow。当前系统后端保存 OIDC client secret，浏览器不接触 OpenMetadata 服务端 PAT 或 client secret。
- OIDC 回调必须校验短时签名 `state`、同源状态 Cookie、授权码和用户信息；登录后只写入短时 HttpOnly 会话 Cookie，再由同源页面换取当前系统 JWT。
- “安全退出”必须同时删除当前系统会话和浏览器中的 OIDC/Keycloak SSO 会话：平台将登录响应中的 `id_token` 仅保存在短时 HttpOnly Cookie，点击退出后浏览器跳转至 OIDC discovery 的 `end_session_endpoint`，携带 `id_token_hint`、`client_id` 和受控的 `post_logout_redirect_uri`。IdP 不可用时必须明确提示仅完成本系统退出，不得伪称已完成统一认证退出。
- 默认只允许 OIDC 用户映射到已有当前系统用户，禁止静默创建管理员。`OIDC_AUTO_PROVISION_USERS=true` 时新用户只能以 `viewer` 角色创建。
- 当前系统权限仍由本地用户角色和权限表控制；OpenMetadata 权限由 OpenMetadata 的角色与 authorizer 控制，两者共享身份，不共享权限令牌。
- OpenMetadata 的数据同步继续使用服务端 PAT；PAT 不得放入页面、URL、localStorage 或 OIDC 回调参数。

## 用户与权限管理扩展（2026-09-07）

- 当前系统继续作为统一配置入口，维护本系统 `User.permissions` 与 OpenMetadata 用户绑定的目标关系；两者不合并为一套伪权限。
- 当前系统管理员可为一个本地用户配置 OpenMetadata 用户名、OpenMetadata 原生 `Role` 和 `Team`，并查看最近一次同步状态。保存后由服务端使用受保护的 OpenMetadata API Token 调用原生 Users/Teams/Roles API。
- OpenMetadata 的实际授权仍由其原生 RBAC/ABAC、Role、Policy 和 Team 层级决定；当前系统只保存目标映射和同步审计，不在前端自行判定 OpenMetadata 资源权限。
- OIDC 用户名默认使用 `preferred_username`，必须与绑定的 OpenMetadata FQN 一致；生产用户应先通过统一认证进入 OpenMetadata，由 SSO 完成用户建立后再同步角色/团队，避免创建脱离身份源的孤立用户。验收环境允许管理员预创建隔离用户以验证权限绑定接口。
- OpenMetadata API Token 只存在于 API/Worker 运行环境，不返回给浏览器，不写入用户配置和审计明文。未配置 Token 时允许保存 `pending` 映射，但必须明确显示未同步，不得伪造成功。
- 本轮保留本地密码登录兼容；生产切换到 Casdoor 或其他 OIDC 提供方时，只替换 OIDC discovery/client 配置，用户权限映射契约不变。
- OIDC 已启用时，登录页只展示统一认证主入口；本地账号字段默认隐藏，仅通过“本地应急登录”显式展开。这样从当前系统进入 OpenMetadata 前，浏览器已经持有同一身份源会话。

### 验收基线

1. 管理员可以创建至少两个本地用户，并分别配置不同的当前系统权限。
2. 管理员可以为两个用户配置不同的 OpenMetadata Role/Team 映射；保存、刷新后目标关系和同步状态可回读。
3. 在 128 的实际 OpenMetadata 1.12.6 中，两个隔离用户可回读不同的原生角色/团队关系；当前系统接口分别按本地权限返回 200/403。
4. 同步失败、Token 缺失或用户尚未在 OpenMetadata 登录时，当前系统不得显示“同步成功”，并保留可诊断的错误摘要。

## 配置

- 当前系统：`OIDC_ENABLED`、`OIDC_ISSUER_URL`、`OIDC_CLIENT_ID`、`OIDC_CLIENT_SECRET`、`OIDC_REDIRECT_URI`；可选 `OIDC_POST_LOGOUT_REDIRECT_URI`。未配置后者时，系统从 `OIDC_REDIRECT_URI` 的同源地址推导 `/ui`；该地址必须配置为 Keycloak Client 的 Valid Post Logout Redirect URI。
- OpenMetadata：`AUTHENTICATION_PROVIDER=custom-oidc`、`AUTHENTICATION_AUTHORITY`、`AUTHENTICATION_CLIENT_ID`、`AUTHENTICATION_CALLBACK_URL`、`AUTHENTICATION_PUBLIC_KEYS` 和对应 OIDC discovery/client 配置。
- 128 使用 Keycloak realm `oracle-recovery` 作为测试身份源；正式环境应替换为企业已有 Keycloak、Entra ID、Okta 或其他 OIDC 提供方，并修改回调地址和密钥。

## Docker 20 完整离线交付（2026-09-09）

- 交付固定采用双服务器结构：A 服务器运行现有业务系统，兼容 Docker 17.03；B 服务器独立运行 OpenMetadata，要求 Linux x86_64、Docker Engine 20.10 或更高版本、Docker Compose 2.2.3 或更高版本。两份离线包、启动脚本、配置模板和校验文件必须物理分开，避免部署人员误启动无关组件。
- OpenMetadata 固定使用 `1.13.0`，依赖 `docker.getcollate.io/openmetadata/db:1.13.0` 和 `docker.elastic.co/elasticsearch/elasticsearch:9.3.0`。所有镜像必须以离线归档和 SHA-256 校验文件交付，不在目标服务器临时联网拉取。
- 本期采用现有平台主动推送元数据和血缘的轻量模式，不打包 OpenMetadata Ingestion/Airflow；OpenMetadata 的 Pipeline Service Client 必须关闭，避免显示不可用的采集调度能力。
- 业务 API 和 Worker 复用已经验证的 `20260909-data-automation-reliable-dispatch-r1` 镜像，不因新增编排文件、环境变量模板或 OpenMetadata 依赖而重新构建业务镜像。
- OpenMetadata 元数据库和搜索数据必须使用独立持久卷，不复用当前平台系统 MySQL；元数据库与搜索端口默认不暴露到宿主机，浏览器只开放 OpenMetadata UI 端口，管理端口仅绑定回环地址。
- A、B 两台服务器不得依赖共享 Docker 网络或跨主机容器名称。B 对 A 和管理员浏览器开放 `8585`；A 使用 `OPENMETADATA_URL=http://B_SERVER_IP:8585` 调用 OpenMetadata `/api`。服务端 API Token 由管理员首次登录 B 后创建并写入 A 的 `.env`，Token 缺失时同步保持关闭，不得以无认证模式绕过。
- A 包一键启动范围只包含业务 API、各 Worker、系统 MySQL 和 Redis；B 包一键启动范围只包含 OpenMetadata 元数据库、Elasticsearch 和 OpenMetadata Server。停止脚本默认保留各自持久卷，不得跨服务器控制另一侧容器。
- 首次部署顺序固定为：先在 B 校验并启动 OpenMetadata，创建服务端 Token；再在 A 配置 B 的地址和 Token，启动或重建业务容器；最后从 A 执行 OpenMetadata 连接检查与元数据/血缘推送冒烟验证。

## 验收

1. 未配置 OIDC 时，原有本地登录和 OpenMetadata 服务端同步不受影响。
2. 配置 OIDC 后，当前系统登录页显示“统一认证登录”。
3. 统一认证回调能映射已有本地用户，并恢复当前系统权限。
4. OpenMetadata 使用同一身份源完成登录，浏览器不再显示 OpenMetadata Basic 登录页。
5. 禁用用户、未绑定用户、篡改 state、过期 state 和无效授权码均被拒绝。
6. OIDC 用户点击“安全退出”后，平台会话 Cookie 与 Keycloak SSO Cookie 均被注销；再次从统一认证进入时应显示认证页面，不得静默复用已退出的 SSO 会话。
