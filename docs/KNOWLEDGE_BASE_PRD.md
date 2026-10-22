# 企业知识库模块 PRD

状态：第一期 MVP 开发基线  
日期：2026-09-18

## 1. 背景与目标

当前系统已经具备帮助与架构中心、OpenMetadata 数据目录和 Harness 智能助手，但缺少面向内部人员持续维护、可被 AI 检索问答的业务知识库。

第一期新增相对独立的“企业知识库”模块，复用当前系统登录、用户、权限和审计机制。工作人员继续使用 Obsidian 或其他 Markdown 编辑器维护受控 Vault，系统负责增量索引、语义检索和带来源回答。

## 2. 模块边界

- 企业知识库：负责 Markdown 规则、流程、接口说明和案例的索引、检索与问答。
- OpenMetadata：继续负责数据库、表、字段和血缘等数据资产事实。
- Harness：继续负责经用户确认后的任务规划和执行。
- 帮助与架构中心：继续提供随版本发布的固定功能说明。
- 第一版知识库不执行 Oracle、Doris、SM4 或 Harness 写操作，不提供 Tool Agent。

## 3. 第一版范围

### 3.1 知识源

- 管理员可以登记位于 `KNOWLEDGE_VAULT_ROOT` 下的 Markdown 知识源。
- 知识源路径必须是相对路径；解析后的真实路径必须仍位于配置根目录内。
- 只扫描 `.md` 文件，忽略隐藏目录、`.git`、`.obsidian`、临时文件和超过配置大小的文件。
- Markdown 使用 YAML Front Matter 描述 `title`、`module`、`owner`、`tags`、`status` 和 `updated_at`。
- 只有 `status: published` 的文档进入正式索引；缺少状态或状态不是 `published` 的文档跳过并计数。

### 3.2 增量索引

- 管理员手工触发单个知识源的增量索引。
- 系统通过相对路径和 SHA-256 判断新增、修改、未变化及删除。
- Markdown 按标题和有限长度分段，向兼容 OpenAI Embeddings 的接口请求向量，并写入 Qdrant。
- 新版本向量写入成功后再删除旧向量；文档删除或撤销发布时同步删除旧向量。
- 每次索引记录扫描、更新、未变化、跳过、删除和失败数量及错误摘要。
- 第一版不依赖实时文件监听，也不自动创建新的 Celery Worker；知识量上限由配置控制，超过限制明确失败。

### 3.3 检索与问答

- 检索先生成问题向量，再从 Qdrant 取回 Top K 片段。
- 问答只把检索片段和必要元数据发送给配置的大模型兼容接口。
- 回答必须返回独立的引用列表，包含文档标题、相对路径、章节和更新时间。
- 检索无有效结果时明确回复知识库依据不足，不让模型自由编造项目规则。
- 第一版不保存聊天正文和知识正文到操作审计；审计只记录操作人、来源数量、引用数量和结果状态。

## 4. 权限与审计

新增统一权限模块 `knowledgeBase`：

- `knowledgeBase:read`：浏览已索引文档和执行检索。
- `knowledgeBase:chat`：使用知识问答。
- `knowledgeBase:manage`：新增、修改知识源。
- `knowledgeBase:index`：触发索引并查看索引运行记录。

管理员继续拥有全部权限；非管理员按当前用户权限结构授权。知识源变更、索引触发和问答均写入既有操作审计表。

## 5. 数据结构

- `knowledge_sources`：知识源名称、相对路径、启用状态及创建信息。
- `knowledge_documents`：文档路径、标题、元数据、SHA-256、Qdrant 点 ID、分段数和索引状态。
- `knowledge_index_runs`：每次增量索引状态、统计、消息和操作者。

知识正文与向量保存在受控 Vault 和 Qdrant；系统 MySQL 只保存索引台账和引用元数据。

## 6. 接口

- `GET /api/v1/knowledge/sources`
- `POST /api/v1/knowledge/sources`
- `PUT /api/v1/knowledge/sources/{source_id}`
- `POST /api/v1/knowledge/sources/{source_id}/index`
- `GET /api/v1/knowledge/index-runs`
- `GET /api/v1/knowledge/documents`
- `POST /api/v1/knowledge/search`
- `POST /api/v1/knowledge/chat`

所有接口复用当前 Bearer Token / API Key 鉴权和 `require_permission`。

## 7. 配置

- `KNOWLEDGE_ENABLED`
- `KNOWLEDGE_VAULT_ROOT`
- `KNOWLEDGE_MAX_FILES`
- `KNOWLEDGE_MAX_FILE_BYTES`
- `KNOWLEDGE_CHUNK_CHARS`
- `KNOWLEDGE_CHUNK_OVERLAP_CHARS`
- `KNOWLEDGE_QDRANT_URL`
- `KNOWLEDGE_QDRANT_API_KEY`
- `KNOWLEDGE_QDRANT_COLLECTION`
- `KNOWLEDGE_EMBEDDING_BASE_URL`
- `KNOWLEDGE_EMBEDDING_API_KEY`
- `KNOWLEDGE_EMBEDDING_MODEL`
- `KNOWLEDGE_SCORE_THRESHOLD`
- `KNOWLEDGE_CHAT_BASE_URL`
- `KNOWLEDGE_CHAT_API_KEY`
- `KNOWLEDGE_CHAT_MODEL`
- `KNOWLEDGE_REQUEST_TIMEOUT_SECONDS`

密钥只通过环境变量配置，不进入页面、数据库、日志和版本库。

## 8. 兼容性与风险

- 不改变现有业务 API、任务结构、调度、Oracle、Doris、SM4、OpenMetadata 或 Harness 行为。
- 知识服务未配置或不可达时，模块展示明确错误，不影响其他系统功能。
- Vault 路径越界、符号链接越界、超大文件、非 UTF-8 文件和未发布文档必须阻断或跳过。
- 第一版所有拥有知识库读取权限的用户可访问同一批已发布知识；文档级密级隔离不在本期范围，禁止把分级敏感内容放入首版 Vault。
- Qdrant 和模型服务建议独立部署；本期不把模型运行时打入现有 Docker 17 业务镜像。

## 9. 验收标准

1. 管理员可新增受控根目录内的知识源，路径越界请求被拒绝。
2. 首次索引能写入已发布 Markdown；重复索引未变化文件不重复向量化。
3. 修改文档后只更新对应文档；删除或撤销发布后旧向量不再参与检索。
4. 检索结果包含分数、标题、相对路径、章节和片段。
5. 问答返回中文答案及独立引用列表；无命中时返回依据不足且不调用自由回答。
6. 四项权限分别限制读取、问答、管理和索引操作，管理员兼容现有全权限语义。
7. Qdrant、Embedding 或 Chat 服务不可达时返回可理解错误，其他模块不受影响。
8. 自动化测试覆盖路径安全、Markdown 解析、增量判断、删除同步、权限和问答引用；前端脚本语法检查通过。
