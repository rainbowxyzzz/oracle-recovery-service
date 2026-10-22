# 20261009 A 服务器完整包

## 范围与边界

- 版本：`20261009-sm4-partial-encryption-r1`。
- 只生成 A 服务器 Docker 17 完整离线包；B 服务器 OpenMetadata 包没有变化，也未重新生成。
- 结构从最近确认的 `20260917-oracle-dual-charset-routing-r1` A 包派生。
- 本次只打包和执行本地、Linux 包级验证，不替换 128 容器、不执行迁移、不重启服务。

## 本版内容

- 保留 `CQ_SM4_ENCRYPT` / `CQ_SM4_DECRYPT` 历史整字段能力。
- 新增 `CQ_SM4_PARTIAL_ENCRYPT` / `CQ_SM4_PARTIAL_DECRYPT`。
- 支持证件号码、姓名、电话、地址四种固定局部加密规则。
- 字段规则保存在现有任务 JSON 的 `column_rules` 中，并随修订、调度、批次和离线生产快照冻结。
- 当前完整源码同时包含默认关闭的企业知识库 MVP；外部 Qdrant、Embedding 和回答模型不进入 A 包。

## 配置变化

- 原有 `.env.example` 字段顺序和默认值保持不变。
- 更新 API、Worker 和应用镜像归档版本。
- 在 OpenMetadata 配置之后追加 `KNOWLEDGE_*` 可选配置，默认 `KNOWLEDGE_ENABLED=false`。
- SM4 局部加密没有新增环境变量，也没有新增系统数据库表。

## 验证口径

- 主测试：`382 passed, 1 skipped, 43 subtests passed`。
- 局部加密专项：`31 passed, 8 subtests passed`。
- 前端内联脚本语法通过。
- 128 已验证任务新增、修改、回读、版本、调度快照及 CSV/JSON 文件解密；隔离数据已清理。
- 128 当前没有可用 Doris 测试连接，因此未完成真实 Doris UDF 创建与目标表写入闭环。
- 登录后浏览器页面闭环未完成，不据此声明完整生产验收通过。

## 部署注意

升级现有环境时保留原 `.env`，只合并本版镜像标签和需要启用的新增配置。部署前备份系统 MySQL、确认无在途任务，再执行 `sh ./start-service.sh`。需要使用局部加密的 Doris 数据库，应在升级后重新应用一次现有密钥的函数能力，以创建并验证局部函数。
