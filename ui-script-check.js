
    const $ = (id) => document.getElementById(id);
    const API_KEY_STORAGE_KEY = "oracleRecoveryService.apiKey";
    let currentTasks = [];
    let currentDetailId = null;
    let detailRefreshTimer = null;
    let cleanupDefaultsCache = null;
    let cleanupCurrentPlan = null;

    function headers() {
      const h = { "Content-Type": "application/json" };
      const key = getApiKey();
      if (key) h["X-API-Key"] = key;
      return h;
    }

    function getApiKey() {
      return $("apiKey").value.trim() || localStorage.getItem(API_KEY_STORAGE_KEY) || "";
    }

    function initApiKey() {
      const saved = localStorage.getItem(API_KEY_STORAGE_KEY) || "";
      if (saved) $("apiKey").value = saved;
      $("apiKey").addEventListener("input", () => {
        const key = $("apiKey").value.trim();
        if (key) localStorage.setItem(API_KEY_STORAGE_KEY, key);
        else localStorage.removeItem(API_KEY_STORAGE_KEY);
      });
      $("clearApiKeyBtn").addEventListener("click", () => {
        $("apiKey").value = "";
        localStorage.removeItem(API_KEY_STORAGE_KEY);
        setMessage("API Key 已清除。", "ok");
      });
    }

    async function api(path, options = {}) {
      const res = await fetch(path, { ...options, headers: { ...headers(), ...(options.headers || {}) } });
      const text = await res.text();
      const data = text ? JSON.parse(text) : null;
      if (!res.ok) {
        const detail = data && (data.detail || data.message) ? data.detail || data.message : text;
        throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      }
      return data;
    }

    function setMessage(text, type = "") {
      $("formMessage").textContent = text;
      $("formMessage").className = `message ${type}`;
    }

    async function loadDefaults() {
      const data = await api("/api/v1/setup/embedded-oracle-defaults");
      $("oracleHost").value = data.oracle_docker_host || "";
      $("oraclePort").value = data.oracle_docker_ssh_port || "";
      $("oracleUser").value = data.oracle_docker_ssh_user || "";
      $("defaultsBox").innerHTML = Object.entries(data)
        .map(([k, v]) => `<strong>${escapeHtml(k)}</strong><span>${escapeHtml(v)}</span>`)
        .join("");
      setMessage("Oracle 默认配置已加载。", "ok");
    }

    async function submitTask() {
      const body = {
        source: {
          host: $("sourceHost").value.trim(),
          port: Number($("sourcePort").value || 22),
          user: $("sourceUser").value.trim(),
          password: $("sourcePassword").value,
          directory: $("sourceDirectory").value.trim()
        },
        volume_group_index: Number($("volumeGroupIndex").value || 0),
        auto_confirm: true
      };
      const oracleHost = $("oracleHost").value.trim();
      const oraclePort = $("oraclePort").value;
      const oracleUser = $("oracleUser").value.trim();
      const oraclePassword = $("oraclePassword").value;
      const generatedPassword = $("generatedPassword").value;
      const isSqlServer = $("engineType").value === "sqlserver";
      const isMySql = $("engineType").value === "mysql";
      if (isMySql) {
        if (oracleHost) body.mysql_host = oracleHost;
        if (oraclePort) body.mysql_port = Number(oraclePort);
        if (oracleUser) body.mysql_user = oracleUser;
        if (oraclePassword) body.mysql_password = oraclePassword;
        if (generatedPassword) body.root_password = generatedPassword;
        if ($("mysqlTargetDatabase").value.trim()) body.target_database = $("mysqlTargetDatabase").value.trim();
        body.drop_existing = $("mysqlDropExisting").checked;
      } else if (isSqlServer) {
        if (oracleHost) body.sqlserver_host = oracleHost;
        if (oraclePort) body.sqlserver_port = Number(oraclePort);
        if (oracleUser) body.sqlserver_user = oracleUser;
        if (oraclePassword) body.sqlserver_password = oraclePassword;
        if (generatedPassword) body.sa_password = generatedPassword;
      } else {
        if (oracleHost) body.oracle_host = oracleHost;
        if (oraclePort) body.oracle_port = Number(oraclePort);
        if (oracleUser) body.oracle_user = oracleUser;
        if (oraclePassword) body.oracle_password = oraclePassword;
        if (generatedPassword) body.generated_user_password = generatedPassword;
      }

      const endpoint = isMySql
        ? "/api/v1/tasks/embedded-mysql"
        : (isSqlServer ? "/api/v1/tasks/embedded-sqlserver" : "/api/v1/tasks/embedded-oracle");
      const created = await api(endpoint, {
        method: "POST",
        body: JSON.stringify(body)
      });
      setMessage(`任务已提交：${created.id}`, "ok");
      await refreshTasks();
      await showDetail(created.id);
    }

    async function refreshTasks() {
      currentTasks = await api("/api/v1/tasks?limit=200");
      renderTasks();
    }

    function renderTasks() {
      const filter = $("stateFilter").value;
      const rows = $("taskRows");
      const tasks = currentTasks.filter((task) => !filter || task.state === filter);
      if (!tasks.length) {
        rows.innerHTML = '<tr><td colspan="5" class="muted">暂无数据</td></tr>';
        return;
      }
      rows.innerHTML = tasks.map((task) => {
        const state = task.state || "";
        const schema = task.target_schema || "-";
        const tool = task.import_tool || "-";
        const canCancel = state === "created";
        return `<tr>
          <td><button data-detail="${task.id}">${shortId(task.id)}</button><br><span class="muted">${escapeHtml(task.target_connection || "")}</span></td>
          <td><span class="state ${escapeHtml(state)}">${escapeHtml(state)}</span></td>
          <td>${escapeHtml(task.remote_directory || "")}<br><span class="muted">schema: ${escapeHtml(schema)} / tool: ${escapeHtml(tool)}</span></td>
          <td>${formatTime(task.created_at)}<br><span class="muted">${formatTime(task.finished_at)}</span></td>
          <td>
            <button data-detail="${task.id}">详情</button>
            ${canCancel ? `<button data-cancel="${task.id}">取消</button>` : ""}
          </td>
        </tr>`;
      }).join("");
    }

    async function showDetail(id) {
      currentDetailId = id;
      const detail = await api(`/api/v1/tasks/${id}/detail`);
      $("detailPanel").hidden = false;
      renderSummary(detail.task);
      renderEvents(detail.events || []);
      scheduleDetailRefresh(detail.task);
    }

    function scheduleDetailRefresh(task) {
      if (detailRefreshTimer) {
        clearInterval(detailRefreshTimer);
        detailRefreshTimer = null;
      }
      if (!task || ["succeeded", "succeeded_with_warnings", "failed", "cancelled"].includes(task.state)) return;
      detailRefreshTimer = setInterval(async () => {
        if (!currentDetailId) return;
        try {
          const detail = await api(`/api/v1/tasks/${currentDetailId}/detail`);
          renderSummary(detail.task);
          renderEvents(detail.events || []);
          if (["succeeded", "succeeded_with_warnings", "failed", "cancelled"].includes(detail.task.state)) {
            clearInterval(detailRefreshTimer);
            detailRefreshTimer = null;
            await refreshTasks();
          }
        } catch (e) {
          setMessage(e.message, "error");
        }
      }, 2000);
    }

    function renderSummary(task) {
      const meta = task.metadata_snapshot || {};
      const entries = {
        "任务 ID": task.id,
        "状态": task.state,
        "源服务器": task.remote_host || "",
        "源目录": task.remote_directory || "",
        "目标连接": task.target_connection || "",
        "目标 schema": task.target_schema || meta.username || "",
        "源 schema": (meta.source_schemas || []).join(", "),
        "REMAP_SCHEMA": (meta.remap_schemas || []).join(", "),
        "识别导出模式": meta.detected_export_mode || "",
        "识别源表空间": (meta.detected_source_tablespaces || []).join(", "),
        "DMP决策置信度": meta.dump_decision_confidence === undefined ? "" : String(meta.dump_decision_confidence),
        "导入工具": task.import_tool || meta.import_tool || "",
        "数据库类型": meta.engine || "",
        "Oracle DIRECTORY": meta.oracle_directory || "",
        "DIRECTORY 路径": meta.oracle_directory_path || "",
        "表空间": meta.tablespace || "",
        "REMAP_TABLESPACE": meta.remap_tablespace || "",
        "MySQL 导入方式": meta.mysql_method || "",
        "MySQL 删除同名库": meta.drop_existing === undefined ? "" : String(meta.drop_existing),
        "警告成功": meta.warning_only === undefined ? "" : String(meta.warning_only),
        "警告错误数": Array.isArray(meta.warning_errors) ? String(meta.warning_errors.length) : "",
        "致命错误数": Array.isArray(meta.fatal_errors) ? String(meta.fatal_errors.length) : "",
        "未知错误数": Array.isArray(meta.unknown_errors) ? String(meta.unknown_errors.length) : "",
        "校验表数量": meta.validation_report ? String(meta.validation_report.table_count || 0) : "",
        "校验INVALID数量": meta.validation_report && Array.isArray(meta.validation_report.invalid_objects) ? String(meta.validation_report.invalid_objects.length) : "",
        "Datafile": meta.datafile || "",
        "导入日志文件": meta.import_logfile || "",
        "创建时间": formatTime(task.created_at),
        "完成时间": formatTime(task.finished_at),
        "错误摘要": task.error_message || ""
      };
      $("summaryBox").innerHTML = Object.entries(entries)
        .map(([k, v]) => `<strong>${escapeHtml(k)}</strong><span>${escapeHtml(v || "-")}</span>`)
        .join("");
    }

    function renderEvents(events) {
      const box = $("eventsBox");
      if (!events.length) {
        box.innerHTML = '<div class="muted">暂无执行日志。</div>';
        return;
      }
      box.innerHTML = events.map((event) => {
        const payload = event.payload && Object.keys(event.payload).length
          ? `<pre>${escapeHtml(JSON.stringify(event.payload, null, 2))}</pre>`
          : "";
        const stdout = event.stdout ? `<h3>stdout / 本次导入日志</h3><pre>${escapeHtml(event.stdout)}</pre>` : "";
        const stderr = event.stderr ? `<h3>stderr</h3><pre>${escapeHtml(event.stderr)}</pre>` : "";
        return `<div class="event">
          <div class="event-head">
            <div><strong>${escapeHtml(event.title)}</strong><br><span class="muted">${escapeHtml(event.event_type)} / ${formatTime(event.created_at)}</span></div>
            <span class="state ${escapeHtml(event.status)}">${escapeHtml(event.status)}</span>
          </div>
          <div class="event-body">
            ${event.message ? `<div>${escapeHtml(event.message)}</div>` : ""}
            ${payload}
            ${stdout}
            ${stderr}
          </div>
        </div>`;
      }).join("");
    }

    async function cancelTask(id) {
      await api(`/api/v1/tasks/${id}/cancel`, { method: "POST" });
      setMessage(`已取消排队任务：${id}`, "ok");
      await refreshTasks();
      await showDetail(id);
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }

    function shortId(id) {
      return String(id || "").slice(0, 8);
    }

    function formatTime(value) {
      if (!value) return "-";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleString();
    }

    function updateEngineOptions() {
      const engine = $("engineType").value;
      $("mysqlOptions").hidden = engine !== "mysql";
      $("loadDefaultsBtn").disabled = engine === "mysql";
      $("generatedPassword").placeholder = engine === "mysql"
        ? "留空则使用 MYSQL_RESTORE_ROOT_PASSWORD"
        : (engine === "sqlserver" ? "留空则使用 SQLSERVER_SA_PASSWORD" : "留空则使用 ORACLE_PWD");
      $("sourceDirectory").placeholder = engine === "mysql"
        ? "/data/mysql-backup"
        : "/sda/数据收集/2026";
    }

    function cleanupSetMessage(text, type = "") {
      $("cleanupMessage").textContent = text;
      $("cleanupMessage").className = `message ${type}`;
    }

    function cleanupConnectionBody() {
      const engine = $("cleanupEngine").value;
      const body = {
        engine,
        host: $("cleanupHost").value.trim(),
        port: $("cleanupPort").value ? Number($("cleanupPort").value) : null,
        username: $("cleanupUser").value.trim(),
        password: $("cleanupPassword").value || "",
      };
      const service = $("cleanupService").value.trim();
      if (engine === "oracle") body.service_name = service;
      else body.database = service;
      if (engine === "sqlserver") {
        body.ssh_host = $("cleanupSshHost").value.trim() || null;
        body.ssh_port = $("cleanupSshPort").value ? Number($("cleanupSshPort").value) : 22;
        body.ssh_user = $("cleanupSshUser").value.trim() || null;
        body.ssh_password = $("cleanupSshPassword").value || null;
        body.container_name = $("cleanupContainer").value.trim() || null;
      }
      return body;
    }

    function cleanupTargetBody() {
      return {
        connection: cleanupConnectionBody(),
        target_name: $("cleanupTarget").value.trim(),
        drop_storage: $("cleanupDropStorage").checked,
        cleanup_files: $("cleanupFiles").checked,
      };
    }

    async function loadCleanupDefaults() {
      cleanupDefaultsCache = await api("/api/v1/database-cleanup/defaults");
      applyCleanupDefaults();
      cleanupSetMessage("Defaults loaded. Empty passwords use server .env defaults.", "ok");
    }

    function applyCleanupDefaults() {
      if (!cleanupDefaultsCache) return;
      const engine = $("cleanupEngine").value;
      const data = cleanupDefaultsCache[engine] || {};
      $("cleanupHost").value = data.host || "";
      $("cleanupPort").value = data.port || "";
      $("cleanupService").value = data.service_name || data.database || "";
      $("cleanupUser").value = data.username || "";
      $("cleanupSshHost").value = data.ssh_host || "";
      $("cleanupSshPort").value = data.ssh_port || "";
      $("cleanupSshUser").value = data.ssh_user || "";
      $("cleanupContainer").value = data.container_name || "";
      updateCleanupEngineOptions();
    }

    function updateCleanupEngineOptions() {
      const engine = $("cleanupEngine").value;
      $("cleanupSqlServerDockerBox").hidden = engine !== "sqlserver";
      $("cleanupDropStorage").disabled = engine !== "oracle";
      $("cleanupFiles").disabled = engine === "oracle";
      if (engine !== "oracle") $("cleanupDropStorage").checked = false;
      if (engine === "oracle") $("cleanupFiles").checked = false;
      $("cleanupService").placeholder = engine === "oracle"
        ? "Example: ORCLPDB1"
        : "Optional; browse all databases";
    }

    async function cleanupTestConnection() {
      const data = await api("/api/v1/database-cleanup/test", {
        method: "POST",
        body: JSON.stringify({ connection: cleanupConnectionBody() })
      });
      cleanupSetMessage(data.message, data.ok ? "ok" : "error");
    }

    async function cleanupLoadCatalog() {
      const data = await api("/api/v1/database-cleanup/catalog", {
        method: "POST",
        body: JSON.stringify({ connection: cleanupConnectionBody() })
      });
      renderCleanupCatalog(data);
      cleanupSetMessage("Catalog loaded.", "ok");
    }

    function renderCleanupCatalog(data) {
      const targets = data.targets || [];
      const objects = data.objects || [];
      $("cleanupCatalogSummary").textContent = `${targets.length} targets / ${objects.length} object summaries`;
      if (!targets.length) {
        $("cleanupTargets").innerHTML = '<div class="muted" style="padding: 10px">No targets found.</div>';
        return;
      }
      const protectedSet = new Set((data.protected_targets || []).map((v) => String(v).toLowerCase()));
      $("cleanupTargets").innerHTML = targets.map((target) => {
        const childCount = objects
          .filter((obj) => obj.parent === target.name)
          .reduce((sum, obj) => sum + Number((obj.details && obj.details.count) || 1), 0);
        const isProtected = protectedSet.has(String(target.name).toLowerCase()) || String(target.type).includes("system");
        return `<button class="cleanup-item" type="button" data-cleanup-target="${escapeHtml(target.name)}">
          <strong>${escapeHtml(target.name)}</strong>
          <span class="muted"> ${escapeHtml(target.type)}${isProtected ? " / protected" : ""}</span>
          <br><span class="muted">${childCount} objects</span>
        </button>`;
      }).join("");
    }

    async function cleanupBuildPlan() {
      const body = cleanupTargetBody();
      if (!body.target_name) {
        cleanupSetMessage("Select or type a drop target first.", "error");
        return;
      }
      cleanupCurrentPlan = await api("/api/v1/database-cleanup/plan", {
        method: "POST",
        body: JSON.stringify(body)
      });
      renderCleanupPlan(cleanupCurrentPlan);
      cleanupSetMessage(
        cleanupCurrentPlan.can_execute ? "Plan built. Review it carefully before execution." : "This target is protected and cannot be dropped.",
        cleanupCurrentPlan.can_execute ? "ok" : "error"
      );
    }

    function renderCleanupPlan(plan) {
      $("cleanupPlanPanel").hidden = false;
      const summary = {
        "Engine": plan.engine,
        "Target": plan.target_name,
        "Protected": plan.protected ? "yes" : "no",
        "Executable": plan.can_execute ? "yes" : "no",
        "Confirmation": plan.confirmation,
        "Warnings": (plan.warnings || []).join("; ") || "-"
      };
      $("cleanupPlanSummary").innerHTML = Object.entries(summary)
        .map(([k, v]) => `<strong>${escapeHtml(k)}</strong><span>${escapeHtml(v)}</span>`)
        .join("");
      $("cleanupPlanSteps").innerHTML = (plan.steps || []).map((step, index) => {
        const notes = (step.notes || []).map((note) => `<div class="muted">${escapeHtml(note)}</div>`).join("");
        return `<div class="step-card ${escapeHtml(step.danger || "")}">
          <strong>${index + 1}. ${escapeHtml(step.layer)} / ${escapeHtml(step.action)}</strong>
          <div>Target: ${escapeHtml(step.target)}</div>
          ${step.sql ? `<pre>${escapeHtml(step.sql)}</pre>` : ""}
          ${notes}
        </div>`;
      }).join("");
      $("cleanupConfirm").value = "";
      $("cleanupResult").textContent = "";
      $("cleanupExecuteBtn").disabled = !plan.can_execute;
    }

    async function cleanupExecutePlan() {
      const body = cleanupTargetBody();
      body.confirmation = $("cleanupConfirm").value.trim();
      const result = await api("/api/v1/database-cleanup/execute", {
        method: "POST",
        body: JSON.stringify(body)
      });
      $("cleanupResult").textContent = result.state === "success"
        ? "Cleanup completed and verified."
        : `Result: ${result.state}${result.error ? " / " + result.error : ""}`;
      $("cleanupResult").className = `message ${result.state === "success" ? "ok" : "error"}`;
      cleanupCurrentPlan = result.plan;
      renderCleanupPlan(result.plan);
      await cleanupLoadCatalog().catch(() => {});
    }

    $("engineType").addEventListener("change", updateEngineOptions);
    $("submitBtn").addEventListener("click", async () => {
      $("submitBtn").disabled = true;
      try { await submitTask(); } catch (e) { setMessage(e.message, "error"); }
      finally { $("submitBtn").disabled = false; }
    });
    $("loadDefaultsBtn").addEventListener("click", async () => {
      try { await loadDefaults(); } catch (e) { setMessage(e.message, "error"); }
    });
    $("refreshBtn").addEventListener("click", async () => {
      try { await refreshTasks(); } catch (e) { setMessage(e.message, "error"); }
    });
    $("stateFilter").addEventListener("change", renderTasks);
    $("taskRows").addEventListener("click", async (event) => {
      const detail = event.target.getAttribute("data-detail");
      const cancel = event.target.getAttribute("data-cancel");
      try {
        if (detail) await showDetail(detail);
        if (cancel) await cancelTask(cancel);
      } catch (e) {
        setMessage(e.message, "error");
      }
    });
    $("cleanupEngine").addEventListener("change", () => {
      updateCleanupEngineOptions();
      applyCleanupDefaults();
    });
    $("cleanupDefaultsBtn").addEventListener("click", async () => {
      try { await loadCleanupDefaults(); } catch (e) { cleanupSetMessage(e.message, "error"); }
    });
    $("cleanupTestBtn").addEventListener("click", async () => {
      try { await cleanupTestConnection(); } catch (e) { cleanupSetMessage(e.message, "error"); }
    });
    $("cleanupCatalogBtn").addEventListener("click", async () => {
      try { await cleanupLoadCatalog(); } catch (e) { cleanupSetMessage(e.message, "error"); }
    });
    $("cleanupPlanBtn").addEventListener("click", async () => {
      try { await cleanupBuildPlan(); } catch (e) { cleanupSetMessage(e.message, "error"); }
    });
    $("cleanupExecuteBtn").addEventListener("click", async () => {
      try { await cleanupExecutePlan(); } catch (e) { $("cleanupResult").textContent = e.message; $("cleanupResult").className = "message error"; }
    });
    $("cleanupTargets").addEventListener("click", (event) => {
      const button = event.target.closest("[data-cleanup-target]");
      if (!button) return;
      $("cleanupTargets").querySelectorAll(".cleanup-item").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      $("cleanupTarget").value = button.getAttribute("data-cleanup-target") || "";
    });

    initApiKey();
    updateEngineOptions();
    updateCleanupEngineOptions();
    refreshTasks().catch(() => {});
  