# 企业知识库部署说明

第一期知识库复用当前业务 API、MySQL、登录、权限和审计，不要求新增业务 Worker。Qdrant、Embedding 和回答模型作为独立服务提供兼容 HTTP 接口，不打入现有 Docker 17 业务镜像。

## 应用配置

1. 将 Obsidian Vault 以只读方式挂载到 API 容器，例如 `/data/knowledge-vault`。
2. 把 `knowledge.env.example` 中需要的变量追加到现有应用 `.env`，不要覆盖原文件。
3. 配置实际 Qdrant、Embedding 和 Chat 地址及密钥，再设置 `KNOWLEDGE_ENABLED=true`。
4. 重建 API 容器以加载新环境变量，并执行项目既有 `init_db.py` 创建知识库台账表。
5. 登录系统，在“企业知识库”中新增相对于 Vault 根目录的知识源并执行增量索引。

容器挂载示例仅表达目录关系，实际启动参数应追加到当前部署脚本：

```text
宿主机 Vault:/data/knowledge-vault:ro
```

## Markdown 约定

只有 `status: published` 的 UTF-8 Markdown 进入索引：

```markdown
---
title: 审批流自动授权操作说明
module: approvalAuthorization
owner: 数据平台主管
tags: [审批流, Doris, 授权]
status: published
updated_at: 2026-09-18
---

# 启动持续监听

保存配置并确认监听状态后启用任务。
```

不要在 Vault 中保存数据库密码、API Key、证书私钥、SM4 密钥种子或包含敏感业务数据的日志。
