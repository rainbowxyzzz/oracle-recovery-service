# 2026-08-21 独立数据血缘中心发布验证记录

## 1. 发布范围

- 发布模式：模式二，增量发布到 `192.168.150.128`。
- 新增左侧一级导航“数据血缘中心”。
- 新增只读血缘总览接口 `GET /api/v1/data-automation/lineage`，支持资产/字段关键字、层级、批次和展示上限参数。
- 既有资产追踪接口增加可选 `batch_id`，原有不传批次的调用语义保持不变。
- 自动化流水线资产表增加“查看血缘”跳转，不修改流水线保存、监听、扫描、继续和运行语义。

## 2. 页面能力

- 展示资产、血缘、字段级、待确认和 SM4 关系摘要。
- 支持资产/字段检索、业务层级和数据批次过滤。
- 支持向上/向下追踪以及 1 至 20 层深度。
- 按恢复层、原始层、标准层和安全层展示图谱。
- 二级区域展示根资产详情、字段预览和当前关系；三级窗口展示完整字段合同、转换表达式和证据 JSON。
- SM4 关系显著标识；页面不展示密钥种子或连接凭据。

## 3. 发布保护与兼容边界

- 发布前通过 Celery `active/reserved` 检查确认无在途任务。
- 系统库备份：`/opt/oracle-recovery/releases/20260821-data-lineage-center-r1-20260821-160310/oracle_recovery_before.sql`。
- 仅更新 API 容器中的 `static/ui.html`、`services/data_automation.py` 和 `api/v1/data_automation.py`；没有数据库迁移，没有重启 Worker。
- 旧流水线、批次、资产、血缘、离线流程、SM4 任务、密钥和调度均未修改。
- 回滚文件位于上述发布目录的 `backup` 子目录。

## 4. 验证结果

- 本地专项测试：`5 passed`。
- 本地完整测试：`232 passed, 1 skipped, 32 subtests passed`。
- 页面脚本语法、919 个静态 DOM ID、目标控件存在性及重复 ID 检查通过。
- 真实鉴权总览：4 个资产、20 条关系、18 条字段级关系、0 条待确认、3 条 SM4 相关关系。
- `PHONE` 字段搜索命中 4 个资产。
- 安全层资产按指定批次向上追踪：3 个相关资产、14 条关系，其中 2 条字段级表达式为 `SM4_ENCRYPT`。
- API 与 8 个业务 Worker 均为运行状态；API 近期日志未发现 `Traceback`、`SyntaxError`、`ImportError` 或 `ModuleNotFoundError`。
- 本地与 128 上三个发布文件 SHA256 完全一致。

## 5. 视觉验证说明

尝试按项目规则连接浏览器执行最大化、左右半屏和还原窗口验证时，本机 Browser 插件在页面打开前报 `Trusted RPC dependency must resolve within a configured trusted code path`。该问题发生在浏览器控制运行时初始化阶段，不是 128 页面或接口报错。为避免误报，本记录不把真实可见浏览器多窗口检查列为通过；响应式样式已覆盖 `1280px`、`900px` 和 `620px` 三档，但仍建议用户刷新页面后做一次现场目视确认。

## 6. 固化镜像

- `oracle-recovery-service-api:20260821-data-lineage-center-r1`
- 镜像 ID：`sha256:a3ed2a4a2436c8eb840332df6e7f27cc2b24b89f4441441c3fc956dd88d3d7d1`
