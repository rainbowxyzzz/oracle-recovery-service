# 2026-09-07 SQL 集合宽工作台发布验证

## 范围与方式

- 模式：发布验证（模式二）。
- 变更：将 SQL 集合由窄弹窗调整为视口内宽工作台，增加集合搜索和“集合配置 / SQL 编排 / 运行记录”页签，底部固定原有操作。
- 兼容边界：不改变集合 API、字段、权限、成员顺序、开发/生产版本、运行、调度或归档语义。
- 发布方式：仅热更新 `oracle-recovery-api` 容器内 `/app/src/recovery_service/static/ui.html`；未重建镜像、未重启容器、未执行数据库迁移、未创建或运行业务 SQL。

## 本地验证

- `python -m pytest tests --ignore=tests/test_microservice_modes.py`：`326 passed, 1 skipped`。
- `tests/test_doris_sql_collections.py`：`4 passed`，其中覆盖工作台三页签和原有固定操作入口。
- `ui.html` 内联脚本的 Node `vm.Script` 语法检查通过。
- `git diff --check` 通过。

## 128 发布前保护

- API 健康检查返回 `status=ok`，MySQL 连接正常。
- `data_platform_workflow_runs` 中 `queued/running` 记录为 `0`。
- API 容器重启次数为 `0`，近 20 分钟无 `Traceback`、`CRITICAL` 或 `Internal Server Error`。
- 更新前备份：`/root/codex-release/20260907-sql-collection-workbench/backup/ui.html.before`。

## 发布结果

- 最终候选、API 容器文件和线上 `/ui?sqlCollectionWorkbench=20260907r3` 响应 SHA-256 均为：

  ```text
  18dad05dc0daf45f61678e054977f67e113da1348d64b44815a126131be688b2
  ```

- 初次页面热更新后发现内部页签 `<section>` 会继承全局卡片边框和内边距；已补充 SQL 集合局部覆盖并以 `ui.html.candidate-r2` 重新热更新。两版上线前文件均保留在同一发布目录的 `backup/` 下。
- 用户截图进一步暴露 `.modal` 通用规则覆盖了 SQL 集合的宽度和内边距；已将选择器提升为 `.modal.sql-collection-modal` 并以 `ui.html.candidate-r3` 重新热更新。该优先级可确保桌面端宽度为 `min(1360px, 100vw - 40px)`，不再回落到通用 `520px` 弹窗宽度。
- API 容器保持 `RestartCount=0`、`Running=true`；发布后健康检查继续返回 `status=ok`，MySQL 连接正常。
- 发布后 5 分钟 API 日志未发现 `Traceback`、`CRITICAL` 或 `Internal Server Error`。

## 可见页面限制

已打开 128 页面，但当前浏览器没有统一认证登录态，停留在登录页。为避免使用或提交未知账号，本次未执行页面登录、集合保存、发布、运行或归档操作；因此三页签切换、搜索和多尺寸可见验收未标记为通过。现有自动化、静态资源一致性和服务健康验证均已完成。
