from __future__ import annotations

import re
import json
import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx
import oracledb
import pymysql
from pymysql.cursors import SSDictCursor
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from recovery_service.common.security import decrypt_secret, encrypt_secret
from recovery_service.common.time import app_now
from recovery_service.core.models.task import (
    ApiOrchestrationConnector,
    ApiOrchestrationNodeRun,
    ApiOrchestrationRun,
    ApiOrchestrationSqlApi,
    ApiOrchestrationWorkflow,
    DatabaseConnectionProfile,
)
from recovery_service.db.session import get_sync_session_factory
from recovery_service.settings import get_settings

_READ_SQL = re.compile(r"^\s*(select|with)\b", re.I | re.S)
_WRITE_SQL = re.compile(r"\b(insert|update|delete|merge|replace|drop|alter|create|truncate|grant|revoke|call)\b", re.I)
_READ_SIDE_EFFECT_SQL = re.compile(r"\binto\s+(outfile|dumpfile)\b|\bfor\s+update\b", re.I)
_PATH_TOKEN = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")
_MAPPING_CONTEXT_ROOTS = {"input", "nodes", "variables", "state", "system"}


def connector_dict(item: ApiOrchestrationConnector) -> dict[str, Any]:
    return {"id": str(item.id), "name": item.name, "method": item.method, "url": item.url,
            "headers": item.headers or {}, "query": item.query or {}, "body_template": item.body_template,
            "auth_type": item.auth_type, "auth_name": item.auth_name, "has_auth_secret": bool(item.auth_secret_enc),
            "success_statuses": item.success_statuses or [], "success_path": item.success_path,
            "success_value": item.success_value, "timeout_seconds": item.timeout_seconds, "enabled": item.enabled,
            "created_at": item.created_at, "updated_at": item.updated_at}


def connector_snapshot(item: ApiOrchestrationConnector) -> dict[str, Any]:
    return {
        "id": str(item.id), "method": item.method, "url": item.url,
        "headers": item.headers or {}, "query": item.query or {}, "body_template": item.body_template,
        "auth_type": item.auth_type, "auth_name": item.auth_name, "auth_secret_enc": item.auth_secret_enc,
        "success_statuses": item.success_statuses or [], "success_path": item.success_path,
        "success_value": item.success_value, "timeout_seconds": item.timeout_seconds, "enabled": True,
    }


def sql_api_dict(item: ApiOrchestrationSqlApi) -> dict[str, Any]:
    return {"id": str(item.id), "name": item.name, "slug": item.slug, "connection_id": str(item.connection_id),
            "database": item.database, "sql_text": item.sql_text, "input_schema": item.input_schema or {},
            "mode": item.mode, "max_rows": item.max_rows, "timeout_seconds": item.timeout_seconds,
            "enabled": item.enabled, "created_at": item.created_at, "updated_at": item.updated_at}


def sql_api_snapshot(item: ApiOrchestrationSqlApi) -> dict[str, Any]:
    return {
        "id": str(item.id), "connection_id": str(item.connection_id), "database": item.database,
        "sql_text": item.sql_text, "input_schema": item.input_schema or {}, "mode": item.mode,
        "max_rows": item.max_rows, "timeout_seconds": item.timeout_seconds, "enabled": True,
    }


def workflow_dict(item: ApiOrchestrationWorkflow) -> dict[str, Any]:
    return {"id": str(item.id), "name": item.name, "description": item.description, "nodes": item.nodes or [],
            "edges": item.edges or [], "status": item.status, "revision": item.revision,
            "created_at": item.created_at, "updated_at": item.updated_at}


def run_dict(session: Session, item: ApiOrchestrationRun, detail: bool = False) -> dict[str, Any]:
    result = {"id": str(item.id), "workflow_id": str(item.workflow_id), "workflow_name": item.workflow_name,
              "workflow_revision": item.workflow_revision, "input_data": _summarize(run_input_data(item)),
              "state": item.state, "message": item.message, "created_at": item.created_at,
              "started_at": item.started_at, "finished_at": item.finished_at}
    if detail:
        rows = session.scalars(select(ApiOrchestrationNodeRun).where(ApiOrchestrationNodeRun.run_id == item.id).order_by(ApiOrchestrationNodeRun.id)).all()
        result["context_data"] = _summarize(_load_context(item.context_data or {}))
        result["node_runs"] = [{"id": row.id, "node_key": row.node_key, "node_name": row.node_name,
          "node_type": row.node_type, "state": row.state, "attempt": row.attempt,
          "request_summary": row.request_summary, "response_summary": row.response_summary,
          "output_data": row.output_data, "error_message": row.error_message,
          "duration_ms": row.duration_ms, "started_at": row.started_at, "finished_at": row.finished_at} for row in rows]
    return result


def save_connector(session: Session, data: dict[str, Any], username: str | None, item_id: uuid.UUID | None = None):
    item = session.get(ApiOrchestrationConnector, item_id) if item_id else ApiOrchestrationConnector()
    if item is None: raise ValueError("连接器不存在。")
    secret = data.pop("auth_secret", None)
    previous_auth_type = item.auth_type if item_id else "none"
    for key, value in data.items(): setattr(item, key, value)
    if item.auth_type in {"none", "dynamic_bearer"}:
        item.auth_secret_enc = ""
    elif secret is not None:
        item.auth_secret_enc = encrypt_secret(secret, get_settings().credential_encryption_key)
    elif previous_auth_type != item.auth_type or not item.auth_secret_enc:
        raise ValueError("启用认证或切换认证方式时必须填写密码 / Token。")
    if not item_id: item.created_by_username = username; session.add(item)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ValueError("连接器保存失败，请检查名称或配置是否重复。") from exc
    session.refresh(item); return item


def save_sql_api(session: Session, data: dict[str, Any], username: str | None, item_id: uuid.UUID | None = None):
    item = session.get(ApiOrchestrationSqlApi, item_id) if item_id else ApiOrchestrationSqlApi()
    if item is None: raise ValueError("SQL API 不存在。")
    profile = session.get(DatabaseConnectionProfile, data["connection_id"])
    if not profile or profile.engine not in {"doris", "mysql", "oracle"}: raise ValueError("请选择 Doris、MySQL 或 Oracle 数据连接。")
    sql = _single_sql(data["sql_text"])
    if data.get("mode", "read") == "read": _ensure_read_only_sql(sql)
    for key, value in data.items(): setattr(item, key, value)
    item.sql_text = sql
    if not item_id: item.created_by_username = username; session.add(item)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ValueError("SQL API 标识 slug 已存在。") from exc
    session.refresh(item); return item


def save_workflow(session: Session, data: dict[str, Any], username: str | None, item_id: uuid.UUID | None = None):
    _validate_graph(data["nodes"], data["edges"])
    item = session.get(ApiOrchestrationWorkflow, item_id) if item_id else ApiOrchestrationWorkflow()
    if item is None: raise ValueError("流程不存在。")
    for key, value in data.items(): setattr(item, key, value)
    if item_id: item.revision += 1; item.status = "draft"
    else: item.created_by_username = username; session.add(item)
    session.commit(); session.refresh(item); return item


def invoke_connector(item: ApiOrchestrationConnector, input_data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(item, dict):
        item = SimpleNamespace(**item)
    if item is None:
        raise ValueError("连接器不存在。")
    if not item.enabled: raise ValueError("连接器已停用。")
    context = {"input": input_data}
    headers = _render(item.headers or {}, context); query = _render(item.query or {}, context)
    body = _render(item.body_template, context)
    url = _render_connector_url(item.url, context)
    secret = decrypt_secret(item.auth_secret_enc, get_settings().credential_encryption_key) if item.auth_secret_enc else ""
    if item.auth_type == "bearer": headers["Authorization"] = f"Bearer {secret}"
    elif item.auth_type == "dynamic_bearer":
        token_path = str(item.auth_name or "token").strip()
        found, token = _lookup_present(input_data, token_path)
        if not found or token is None or token == "":
            raise ValueError(f"动态 Bearer 缺少流程输入：{token_path}")
        headers["Authorization"] = f"Bearer {token}"
    elif item.auth_type == "raw": headers["Authorization"] = secret
    elif item.auth_type == "api_key": headers[item.auth_name or "X-API-Key"] = secret
    auth = (item.auth_name or "", secret) if item.auth_type == "basic" else None
    started = time.monotonic()
    with httpx.Client(timeout=item.timeout_seconds, follow_redirects=False) as client:
        response = client.request(item.method, url, headers=headers, params=query, json=body if body is not None else None, auth=auth)
    duration = int((time.monotonic() - started) * 1000)
    try: payload = response.json()
    except Exception: payload = response.text
    ok = response.status_code in set(item.success_statuses or [200])
    if ok and item.success_path: ok = _lookup(payload, item.success_path) == item.success_value
    result = {"transport": {"status_code": response.status_code, "duration_ms": duration, "attempt": 1},
              "headers": dict(response.headers), "body": payload, "success": ok}
    if not ok: raise RuntimeError(f"接口调用失败：HTTP {response.status_code}，响应={str(payload)[:500]}")
    return result


def invoke_sql_api(session: Session, item: ApiOrchestrationSqlApi, params: dict[str, Any], allow_write: bool = False) -> dict[str, Any]:
    if isinstance(item, dict):
        item = SimpleNamespace(**item)
    if item is None:
        raise ValueError("SQL API 不存在。")
    if not item.enabled: raise ValueError("SQL API 已停用。")
    if item.mode == "write" and not allow_write: raise PermissionError("缺少 SQL API 写入权限。")
    _validate_params(item.input_schema or {}, params)
    connection_id = item.connection_id if isinstance(item.connection_id, uuid.UUID) else uuid.UUID(str(item.connection_id))
    profile = session.get(DatabaseConnectionProfile, connection_id)
    if not profile: raise ValueError("SQL API 数据连接不存在。")
    started = time.monotonic(); rows=[]; columns=[]; affected=None; truncated=False
    password = decrypt_secret(profile.password_enc, get_settings().credential_encryption_key)
    if profile.engine in {"doris", "mysql"}:
        sql = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", r"%(\1)s", item.sql_text)
        with pymysql.connect(host=profile.host, port=profile.port or (9030 if profile.engine == "doris" else 3306), user=profile.username,
            password=password, database=item.database or profile.database or None, charset="utf8mb4", autocommit=True,
            cursorclass=SSDictCursor, connect_timeout=min(item.timeout_seconds, 30), read_timeout=item.timeout_seconds) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params); affected=cur.rowcount
                if cur.description:
                    columns=[str(x[0]) for x in cur.description]; fetched=cur.fetchmany(item.max_rows+1); truncated=len(fetched)>item.max_rows
                    rows=[{k:_jsonable(v) for k,v in row.items()} for row in fetched[:item.max_rows]]; affected=None
    else:
        dsn = profile.dsn or oracledb.makedsn(profile.host, profile.port or 1521, service_name=profile.service_name or profile.database)
        with oracledb.connect(user=profile.username, password=password, dsn=dsn) as conn:
            conn.call_timeout=item.timeout_seconds*1000
            with conn.cursor() as cur:
                cur.execute(item.sql_text, params); affected=cur.rowcount
                if cur.description:
                    columns=[str(x[0]) for x in cur.description]
                    fetched=cur.fetchmany(item.max_rows+1); truncated=len(fetched)>item.max_rows
                    rows=[{columns[i]:_jsonable(v) for i,v in enumerate(row)} for row in fetched[:item.max_rows]]; affected=None
                elif item.mode == "write": conn.commit()
    return {"success": True, "columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated, "affected_rows": affected,
            "duration_ms": int((time.monotonic()-started)*1000)}


def submit_run(session: Session, workflow: ApiOrchestrationWorkflow, input_data: dict[str, Any], username: str | None,
               snapshot: dict[str, Any] | None = None, revision: int | None = None):
    uses_frozen_snapshot = snapshot is not None
    snapshot = snapshot or workflow.published_snapshot or {"nodes": workflow.nodes or [], "edges": workflow.edges or []}
    if workflow.status != "published" and not uses_frozen_snapshot: raise ValueError("流程尚未发布。")
    run = ApiOrchestrationRun(workflow_id=workflow.id, workflow_name=workflow.name, workflow_revision=revision or workflow.revision,
        workflow_snapshot=snapshot, input_data=_store_context({"input": input_data}), context_data=_store_context({"input": input_data, "nodes": {}}), created_by_username=username)
    session.add(run); session.commit(); session.refresh(run)
    from recovery_service.workers.celery_app import celery_app
    try:
        result=celery_app.send_task("api_orchestration.run", args=[str(run.id)], queue=get_settings().celery_api_orchestration_queue)
    except Exception as exc:
        run.state="failed"; run.message=f"任务投递失败：{exc}"; run.finished_at=app_now(); session.commit()
        raise
    run.celery_task_id=result.id; session.commit(); return run


def execute_run(run_id: str) -> None:
    factory=get_sync_session_factory()
    with factory() as session:
        run=session.get(ApiOrchestrationRun, uuid.UUID(run_id));
        if not run: return
        run.state="running"; run.started_at=app_now(); session.commit()
        try:
            nodes={str(n["id"]):n for n in run.workflow_snapshot.get("nodes",[])}; edges=run.workflow_snapshot.get("edges",[])
            incoming={key:[] for key in nodes}; outgoing={key:[] for key in nodes}
            for index, edge in enumerate(edges):
                incoming[str(edge["target"])].append(index); outgoing[str(edge["source"])].append(index)
            edge_state={}; pending=set(nodes); context=_load_context(run.context_data or {})
            queue, skipped=_resolve_ready_nodes(nodes,incoming,outgoing,edge_state,pending)
            _record_skipped_nodes(session,run,skipped)
            visited=set()
            while queue:
                key=queue.pop(0)
                if key in visited: continue
                node=nodes[key]; started=time.monotonic(); nr=ApiOrchestrationNodeRun(run_id=run.id,node_key=key,node_name=node.get("name") or key,node_type=node["type"],state="running")
                session.add(nr); session.commit()
                try:
                    nr.request_summary=_node_request_summary(node,context)
                    output=_execute_node(session,node,context); nr.output_data=_summarize(output); nr.response_summary=_summarize(output); nr.state="succeeded"
                    context.setdefault("nodes",{})[key]=output; run.context_data=_store_context(context)
                except Exception as exc:
                    nr.state="failed"; nr.error_message=str(exc); run.state="failed"; run.message=f"节点 {nr.node_name} 失败：{exc}"; raise
                finally: nr.duration_ms=int((time.monotonic()-started)*1000); nr.finished_at=app_now(); session.commit()
                visited.add(key)
                selected_edges=_selected_outgoing_edges(node,edges,outgoing[key],output)
                for edge_index in outgoing[key]: edge_state[edge_index]=edge_index in selected_edges
                ready, newly_skipped=_resolve_ready_nodes(nodes,incoming,outgoing,edge_state,pending)
                queue.extend(ready); skipped.extend(newly_skipped)
                _record_skipped_nodes(session,run,newly_skipped)
            if pending:
                raise RuntimeError(f"流程存在未决节点：{', '.join(sorted(pending))}")
            run.state="succeeded"; run.message=f"流程执行成功，共完成 {len(visited)} 个节点，跳过 {len(skipped)} 个节点。"; run.context_data=_store_context(context)
        except Exception:
            if run.state!="failed": run.state="failed"
        finally: run.finished_at=app_now(); session.commit()


def _execute_node(session,node,context):
    kind=node["type"]; config=node.get("config") or {}
    if kind=="start": return context.get("input",{})
    if kind=="end": return {"success":True}
    if kind=="http":
        item=session.get(ApiOrchestrationConnector,uuid.UUID(str(config["connector_id"])))
        if not item or not item.enabled: raise ValueError("连接器不存在或已停用。")
        return invoke_connector(config.get("connector_snapshot") or item,_render(config.get("input",{}),context))
    if kind=="sql_api":
        item=session.get(ApiOrchestrationSqlApi,uuid.UUID(str(config["sql_api_id"])))
        if not item or not item.enabled: raise ValueError("SQL API 不存在或已停用。")
        return invoke_sql_api(session,config.get("sql_api_snapshot") or item,_render(config.get("input",{}),context),allow_write=bool(config.get("write_authorized")))
    if kind=="mapping":
        return _render_mapping(config.get("template"), context, config.get("missing_policy", "error"))
    if kind=="condition": return {"matched":_compare(_lookup(context,str(config.get("path") or "")),config.get("operator","equals"),config.get("value"))}
    raise ValueError(f"不支持的节点类型：{kind}")


def _selected_outgoing_edges(node, edges, outgoing_indexes, output):
    if node.get("type") != "condition":
        return set(outgoing_indexes)
    branch = "true" if output.get("matched") else "false"
    matched = {index for index in outgoing_indexes if str(edges[index].get("branch", "default")) == branch}
    if matched:
        return matched
    return {index for index in outgoing_indexes if str(edges[index].get("branch", "default")) == "default"}


def _resolve_ready_nodes(nodes, incoming, outgoing, edge_state, pending):
    ready=[]; skipped=[]; changed=True
    while changed:
        changed=False
        for key in nodes:
            if key not in pending:
                continue
            incoming_edges=incoming[key]
            if incoming_edges and not all(index in edge_state for index in incoming_edges):
                continue
            pending.remove(key); changed=True
            if not incoming_edges or any(edge_state[index] for index in incoming_edges):
                ready.append(key)
            else:
                skipped.append(key)
                for index in outgoing[key]:
                    edge_state[index]=False
    return ready, skipped


def _record_skipped_nodes(session,run,node_keys):
    now=app_now()
    for key in node_keys:
        node=next((item for item in run.workflow_snapshot.get("nodes",[]) if str(item.get("id"))==key),{})
        session.add(ApiOrchestrationNodeRun(run_id=run.id,node_key=key,node_name=node.get("name") or key,
            node_type=node.get("type") or "unknown",state="skipped",request_summary={"reason":"条件分支未命中"},
            response_summary={},output_data={},duration_ms=0,started_at=now,finished_at=now))
    if node_keys: session.commit()


def _validate_graph(nodes,edges):
    ids=[str(n.get("id")) for n in nodes]
    if not nodes or len(ids)!=len(set(ids)) or any(not x or x=="None" for x in ids): raise ValueError("流程节点 ID 无效或重复。")
    starts=[n for n in nodes if n.get("type")=="start"]
    if len(starts)!=1 or not any(n.get("type")=="end" for n in nodes): raise ValueError("流程必须包含且只能包含一个开始节点，并至少包含一个结束节点。")
    graph={x:[] for x in ids}; indegree={x:0 for x in ids}
    for e in edges:
        s,t=str(e.get("source")),str(e.get("target"))
        if s not in graph or t not in graph or s==t or t in graph[s]: raise ValueError("流程连线无效、自环或重复。")
        graph[s].append(t); indegree[t]+=1
    original_indegree=indegree.copy()
    q=[k for k,v in indegree.items() if v==0]; count=0
    while q:
        x=q.pop(0); count+=1
        for y in graph[x]: indegree[y]-=1; q.append(y) if indegree[y]==0 else None
    if count!=len(ids): raise ValueError("流程不允许环路。")
    reachable=set(); q=[str(starts[0]["id"])]
    while q:
        current=q.pop(0)
        if current in reachable: continue
        reachable.add(current); q.extend(graph[current])
    if reachable!=set(ids): raise ValueError("所有节点都必须从开始节点可达。")
    for node in nodes:
        key=str(node["id"])
        if node.get("type")=="start" and original_indegree.get(key,0): raise ValueError("开始节点不能有入线。")
        if node.get("type")=="end" and graph[key]: raise ValueError("结束节点不能有出线。")
        if node.get("type")=="mapping": _validate_mapping_config(node)


def _render(value,context):
    if isinstance(value,dict): return {k:_render(v,context) for k,v in value.items()}
    if isinstance(value,list): return [_render(v,context) for v in value]
    if not isinstance(value,str): return value
    match=_PATH_TOKEN.fullmatch(value)
    if match: return _lookup(context,match.group(1))
    return _PATH_TOKEN.sub(lambda m:str(_lookup(context,m.group(1)) or ""),value)


def _render_mapping(value: Any, context: dict[str, Any], missing_policy: str = "error") -> Any:
    if missing_policy not in {"error", "null"}:
        raise ValueError("数据映射节点的缺失字段策略必须是 error 或 null。")
    if isinstance(value, dict):
        return {key: _render_mapping(item, context, missing_policy) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_mapping(item, context, missing_policy) for item in value]
    if not isinstance(value, str):
        return value
    match = _PATH_TOKEN.fullmatch(value)
    if match:
        found, result = _lookup_present(context, match.group(1))
        if found:
            return result
        if missing_policy == "null":
            return None
        raise ValueError(f"数据映射路径不存在：{match.group(1)}")

    def replace(match: re.Match[str]) -> str:
        found, result = _lookup_present(context, match.group(1))
        if not found:
            if missing_policy == "null":
                return ""
            raise ValueError(f"数据映射路径不存在：{match.group(1)}")
        if result is None:
            return ""
        if isinstance(result, (dict, list, bool, int, float)):
            return json.dumps(result, ensure_ascii=False, default=_jsonable)
        return str(result)

    return _PATH_TOKEN.sub(replace, value)


def _lookup(value,path):
    found, current = _lookup_present(value, path)
    return current if found else None


def _lookup_present(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    for part in [item for item in str(path).split(".") if item]:
        if isinstance(current, dict):
            if part not in current:
                return False, None
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, current


def _validate_mapping_config(node: dict[str, Any]) -> None:
    config = node.get("config") or {}
    template = config.get("template")
    label = node.get("name") or node.get("id") or "mapping"
    if not isinstance(template, (dict, list)):
        raise ValueError(f"数据映射节点 {label} 的输出模板必须是 JSON 对象或数组。")
    missing_policy = config.get("missing_policy", "error")
    if missing_policy not in {"error", "null"}:
        raise ValueError(f"数据映射节点 {label} 的缺失字段策略必须是 error 或 null。")
    for expression in _mapping_references(template, validate=True):
        if expression.split(".", 1)[0] not in _MAPPING_CONTEXT_ROOTS:
            raise ValueError(f"数据映射节点 {label} 使用了不支持的上下文路径：{expression}")


def _mapping_references(value: Any, validate: bool = False) -> list[str]:
    references: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            references.extend(_mapping_references(item, validate=validate))
    elif isinstance(value, list):
        for item in value:
            references.extend(_mapping_references(item, validate=validate))
    elif isinstance(value, str):
        references.extend(_PATH_TOKEN.findall(value))
        if validate:
            remainder = _PATH_TOKEN.sub("", value)
            if "{{" in remainder or "}}" in remainder:
                raise ValueError(f"数据映射模板包含不受支持的表达式：{value}")
    return references


def _compare(actual,op,expected):
    if op=="exists": return actual is not None
    if op=="not_equals": return actual!=expected
    if op=="contains": return expected in actual if actual is not None else False
    return actual==expected


def _validate_params(schema,params):
    for key in schema.get("required",[]):
        if key not in params: raise ValueError(f"缺少必填参数：{key}")
    props=schema.get("properties",{})
    for key,value in params.items():
        if key not in props: raise ValueError(f"未声明的参数：{key}")
        expected=props[key].get("type")
        if expected=="integer" and (not isinstance(value,int) or isinstance(value,bool)): raise ValueError(f"参数 {key} 必须为整数。")
        if expected=="number" and not isinstance(value,(int,float)): raise ValueError(f"参数 {key} 必须为数字。")
        if expected=="boolean" and not isinstance(value,bool): raise ValueError(f"参数 {key} 必须为布尔值。")
        if expected=="string" and not isinstance(value,str): raise ValueError(f"参数 {key} 必须为字符串。")


def _single_sql(sql):
    text=str(sql or "").strip().rstrip(";").strip()
    if not text or ";" in text: raise ValueError("SQL API 仅允许单条 SQL。")
    return text


def _ensure_read_only_sql(sql: str) -> None:
    if not _READ_SQL.match(sql) or _WRITE_SQL.search(sql) or _READ_SIDE_EFFECT_SQL.search(sql):
        raise ValueError("只读 SQL API 仅允许不含写入或 DDL 关键字的 SELECT/WITH。")


def _render_connector_url(template: str, context: dict[str, Any]) -> str:
    rendered = str(_render(template, context) or "").strip()
    configured_url = urlsplit(template)
    rendered_url = urlsplit(rendered)
    if configured_url.scheme not in {"http", "https"} or not configured_url.netloc:
        raise ValueError("连接器 URL 必须是完整的 HTTP/HTTPS 地址。")
    if rendered_url.scheme != configured_url.scheme or rendered_url.netloc != configured_url.netloc:
        raise ValueError("动态 URL 只能替换路径、查询参数，不能改变协议或目标主机。")
    if rendered_url.username or rendered_url.password:
        raise ValueError("连接器 URL 不允许携带用户名或密码。")
    return rendered


def _jsonable(value):
    if isinstance(value,(datetime,date)): return value.isoformat()
    if isinstance(value,Decimal): return float(value)
    if isinstance(value,(bytes,bytearray)): return value.hex()
    return value


def _summarize(value):
    if isinstance(value,dict): return {k:("******" if any(x in k.lower() for x in ("token","password","secret","cookie","authorization")) else _redact(v)) for k,v in value.items() if k!="headers"}
    if isinstance(value,list): return [_redact(v) for v in value[:100]]
    return {"value":value}


def _node_request_summary(node: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    config = node.get("config") or {}
    if node.get("type") == "mapping":
        referenced_values = {}
        for path in sorted(set(_mapping_references(config.get("template")))):
            found, value = _lookup_present(context, path)
            referenced_values[path] = value if found else "<missing>"
        return _summarize({
            "node_type": "mapping",
            "missing_policy": config.get("missing_policy", "error"),
            "referenced_values": referenced_values,
        })
    summary = {
        "node_type": node.get("type"),
        "resource_id": config.get("connector_id") or config.get("sql_api_id"),
        "input": _render(config.get("input", {}), context),
    }
    return _summarize(summary)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("******" if any(token in key.lower() for token in ("token", "password", "secret", "cookie", "authorization")) else _redact(item))
            for key, item in value.items()
            if key != "headers"
        }
    if isinstance(value, list):
        return [_redact(item) for item in value[:100]]
    return value


def _store_context(context: dict[str, Any]) -> dict[str, Any]:
    plain = json.dumps(context, ensure_ascii=False, default=_jsonable)
    return {"_encrypted": encrypt_secret(plain, get_settings().credential_encryption_key)}


def _load_context(stored: dict[str, Any]) -> dict[str, Any]:
    cipher = stored.get("_encrypted") if isinstance(stored, dict) else None
    if not cipher:
        return stored if isinstance(stored, dict) else {}
    plain = decrypt_secret(str(cipher), get_settings().credential_encryption_key)
    try:
        value = json.loads(plain)
    except (TypeError, ValueError):
        return {"input": {}, "nodes": {}}
    return value if isinstance(value, dict) else {"input": {}, "nodes": {}}


def run_input_data(run: ApiOrchestrationRun) -> dict[str, Any]:
    stored = _load_context(run.input_data or {})
    value = stored.get("input", {})
    return value if isinstance(value, dict) else {}
