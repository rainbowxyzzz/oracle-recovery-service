"use strict";
const $ = id => document.getElementById(id);
let currentPlan = null, loading = false;
const states = {draft:"待确认",confirmed:"已确认",assistant_waiting_file:"等待文件稳定",restore_queued:"还原排队",restoring:"正在还原",assistant_sync_ready:"待同步",sync_queued:"同步排队",syncing:"正在同步",assistant_standard_ready:"待更新 DWD",standardize_queued:"SQL 排队",standardizing:"更新 DWD",assistant_encryption_ready:"待全库加密",encrypting:"正在加密",completed:"全部完成",failed:"失败",blocked:"已阻断"};
async function api(path, body) {
  const token = localStorage.getItem("oracleRecoveryService.authToken");
  const response = await fetch("/api/v1/assistant" + path, {method:body === undefined ? "GET" : "POST", cache:"no-store", headers:{"Content-Type":"application/json",...(token ? {Authorization:"Bearer " + token} : {})}, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
  const result = await response.json();
  if (!response.ok) throw new Error(response.status === 401 ? "请先返回业务工作台登录。" : response.status === 403 ? "智能助手首版仅允许管理员操作。" : typeof result.detail === "string" ? result.detail : "请求失败，请核对填写内容。");
  return result;
}
async function action(fn) {
  if (loading) return;
  loading = true; $("chat").disabled = true; $("prepare").disabled = true;
  try {$("reply").classList.remove("error"); await fn();} catch(e) {$("reply").textContent=e.message; $("reply").classList.add("error");} finally {loading=false; $("chat").disabled=false; $("prepare").disabled=false;}
}
function option(select, value, text) {const o=document.createElement("option");o.value=value;o.textContent=text;select.append(o);}
async function refresh() {
  const [data, history] = await Promise.all([api("/catalog"), api("/plans")]);
  const savedPipeline=$("pipeline").value, savedSm4=$("sm4").value;
  $("pipeline").replaceChildren();option($("pipeline"),"","自动匹配");
  data.pipelines.forEach(p=>option($("pipeline"),p.id,p.name+(p.configured?"":"（绑定待补齐）")));
  $("sm4").replaceChildren();option($("sm4"),"","使用路径绑定 / 由助手推荐");
  data.sm4_tasks.forEach(t=>option($("sm4"),t.id,`${t.name} · ${t.database} · ${t.table_count}表 · 修订${t.revision}`));
  $("pipeline").value=savedPipeline;$("sm4").value=savedSm4;
  $("status").textContent=data.harness_configured?"Harness 桥接地址已配置；真实模型连通性将在规划时检查。":"Harness 尚未配置，可先用显式路径生成计划。配置模型后才能使用自然语言规划。";
  $("history").replaceChildren();
  history.forEach(p=>{const b=document.createElement("button");b.textContent=`${p.file_name}\n${p.pipeline_name} · ${states[p.state]||(p.state.startsWith("assistant_submitting_")?"提交待核对":p.state)}`;b.onclick=()=>action(()=>loadPlan(p.plan_id));$("history").append(b);});
}
async function loadPlan(id){currentPlan=await api("/plans/"+id);draw();}
function draw(){
  const p=currentPlan,b=p.batch||{},state=b.state||p.state;$("planCard").hidden=false;
  $("planState").textContent=states[state]||(state.startsWith("assistant_submitting_")?"提交中 / 结果待核对":state);
  $("summary").textContent=`${p.pipeline_name}｜${p.files.map(f=>f.relative_path).join("、")}\nOracle：${p.restore_target}\nDWD：${p.standard_target.map(t=>t.database+(t.table_name?"."+t.table_name:"")).join("、")}｜生产版本 ${p.workflow_version_no}\n全库加密任务：${p.sm4.name}（修订 ${p.sm4.revision}），${p.sm4.tables.length} 张表`;
  $("summary").style.whiteSpace="pre-wrap";$("warnings").textContent=p.warnings.join(" ");
  $("steps").replaceChildren();
  [["Oracle 还原","restore_task_id"],["同步到 ODS","sync_run_id"],["更新 DWD","standard_run_id"],["全库 SM4","encryption_batch_id"]].forEach(([title,key])=>{const el=document.createElement("div");el.className="step";const strong=document.createElement("strong");strong.textContent=title;const code=document.createElement("code");code.textContent=b[key]||"尚未创建运行";el.append(strong,code);$("steps").append(el);});
  $("events").replaceChildren();(p.events||[]).forEach(e=>{const el=document.createElement("p");el.textContent=`${e.created_at} · ${e.stage} · ${e.message}`;$("events").append(el);});
  if(b.error_message){const el=document.createElement("p");el.className="error";el.textContent=b.error_message;$("events").append(el);}
  $("review").textContent=p.batch_id?"查看完整范围":"查看完整范围 / 确认执行";$("resume").hidden=state!=="failed";
  localStorage.setItem("oracleRecoveryService.assistantPlan",p.plan_id);
}
function section(title,rows){const h=document.createElement("h3");h.textContent=title;const wrap=document.createElement("div");wrap.className="table-wrap";const table=document.createElement("table");rows.forEach(row=>{const tr=document.createElement("tr");row.forEach(v=>{const td=document.createElement("td");td.textContent=String(v??"");tr.append(td);});table.append(tr);});wrap.append(table);$("detail").append(h,wrap);}
$("review").onclick=()=>{
  const p=currentPlan;$("detail").replaceChildren();
  section("实际文件清单",p.files.map(f=>[f.relative_path,f.size_bytes+" 字节"]));
  section("ODS 同步范围与写入策略",p.sync_tables.map(t=>[t.source_table,t.target_database,t.target_table,t.write_mode||p.sync_write_mode||"以原任务配置为准"]));
  section("DWD 标准目标",p.standard_target.map(t=>[t.database,t.table_name,"生产版本 "+p.workflow_version_no]));
  (p.sql_steps||[]).forEach(s=>{const details=document.createElement("details"),title=document.createElement("summary"),sql=document.createElement("pre");title.textContent=`查看冻结 SQL：${s.name} · ${s.database||"连接默认库"} · ${s.connection_id||""}`;sql.textContent=s.sql||"";sql.style.whiteSpace="pre-wrap";sql.style.overflowWrap="anywhere";details.append(title,sql);$("detail").append(details);});
  section("全库 SM4："+p.sm4.name,p.sm4.tables.map(t=>[p.sm4.database,t.table_name,(t.columns||[]).join("、"),p.sm4.table_strategy,p.sm4.target_suffix||"默认加密后缀"]));
  $("ack").checked=false;$("confirm").disabled=true;$("confirm").hidden=!!p.batch_id;$("ack").parentElement.hidden=!!p.batch_id;$("reviewDialog").showModal();
};
$("ack").onchange=()=>{$("confirm").disabled=!$("ack").checked;};
$("confirm").onclick=()=>action(async()=>{const id=currentPlan.plan_id;$("confirm").disabled=true;try{await api(`/plans/${id}/confirm`,{plan_hash:currentPlan.plan_hash});$("reviewDialog").close();await loadPlan(id);await refresh();$("reply").textContent="已确认；后台将自动推进四个阶段。";}finally{$("confirm").disabled=!$("ack").checked;}});
$("closeReview").onclick=()=>$("reviewDialog").close();$("closeResume").onclick=()=>$("resumeDialog").close();
$("resume").onclick=()=>{$("retryAck").checked=false;$("retryConfirm").disabled=true;$("resumeDialog").showModal();};
$("retryAck").onchange=()=>{$("retryConfirm").disabled=!$("retryAck").checked;};
$("retryConfirm").onclick=()=>action(async()=>{await api(`/plans/${currentPlan.plan_id}/resume`,{acknowledge_partial_writes:true});$("resumeDialog").close();await loadPlan(currentPlan.plan_id);});
$("chat").onclick=()=>action(async()=>{$("reply").textContent="Harness 正在理解指令，仅规划、不执行…";const r=await api("/chat",{message:$("message").value,pipeline_id:$("pipeline").value||null,sm4_task_id:$("sm4").value||null});$("reply").textContent=r.reply;if(r.plan)await loadPlan(r.plan.plan_id);await refresh();});
$("prepare").onclick=()=>action(async()=>{if(!$("pipeline").value)throw new Error("请先选择处理路径。");const p=await api("/plans",{pipeline_id:$("pipeline").value,file_name:$("filename").value,sm4_task_id:$("sm4").value||null});await loadPlan(p.plan_id);await refresh();$("reply").textContent="已生成待确认计划，尚未执行。";});
$("poll").onclick=()=>action(()=>loadPlan(currentPlan.plan_id));$("refresh").onclick=()=>action(refresh);
setInterval(()=>{if(currentPlan?.batch_id&&!loading&&!document.hidden&&!$("reviewDialog").open&&!$("resumeDialog").open)action(()=>loadPlan(currentPlan.plan_id));},10000);
action(async()=>{await refresh();const id=localStorage.getItem("oracleRecoveryService.assistantPlan");if(id)await loadPlan(id);});
