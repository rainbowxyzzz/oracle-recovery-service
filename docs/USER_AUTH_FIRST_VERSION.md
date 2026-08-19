# 用户登录与管理员用户体系

## 默认管理员

系统启动时如果 `users` 表为空，会自动创建默认管理员：

- 用户名：`DEFAULT_ADMIN_USERNAME`，当前默认 `admin`
- 密码：`DEFAULT_ADMIN_PASSWORD`，当前默认 `admin123`
- 显示名：`DEFAULT_ADMIN_DISPLAY_NAME`，当前默认 `系统管理员`

这些配置位于 `.env`。正式使用后建议第一时间重置默认管理员密码。

## 鉴权方式

第一版支持两种方式并存：

1. 用户登录后使用 `Authorization: Bearer <token>`
2. 旧系统兼容方式：`X-API-Key: <SECRET_KEY>`

`X-API-Key` 保留为兼容/应急管理员凭证；新页面默认走登录用户 token。

## 用户管理

管理员登录后，可在页面左侧进入“用户管理”：

- 创建用户
- 启用 / 禁用用户
- 切换管理员 / 操作员
- 重置密码

第一版角色：

- `admin`：管理员，可管理用户
- `operator`：操作员，可执行业务操作
- `viewer`：观察员，预留角色

## 任务归属

SM3 脱敏任务新增以下字段：

- `created_by_user_id`
- `created_by_username`
- `created_by_auth_type`

如果使用登录用户提交任务，会绑定具体用户；如果使用旧 API Key，会记录为 `api-key`。

## 行为审计

系统新增 `operation_audit_logs` 表，当前第一版记录：

- 登录成功 / 失败
- 用户创建、修改、重置密码
- SM3 任务提交、取消
- SM3 映射反查

敏感数据不会写入审计，例如密码、数据库密码、批量密文原文列表。

## API

登录：

```http
POST /api/v1/auth/login
Content-Type: application/json

{
  "username": "admin",
  "password": "admin123"
}
```

查看当前身份：

```http
GET /api/v1/auth/me
Authorization: Bearer <token>
```

用户列表：

```http
GET /api/v1/users
Authorization: Bearer <admin-token>
```

创建用户：

```http
POST /api/v1/users
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "username": "operator01",
  "password": "123456",
  "display_name": "操作员01",
  "role": "operator",
  "status": "active"
}
```
