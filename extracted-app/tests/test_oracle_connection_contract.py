import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from recovery_service.core.domain import RemoteHost, TargetDatabase
from recovery_service.orchestrator.oracle_auto_import_runner import OracleAutoImportRunner
from recovery_service.tools import oracle_dmp_auto_import as tool


@pytest.mark.parametrize("dsn", [
    "23.47.200.105:1523/ORCLPDBUTF8",
    "oracle-recovery-oracle21c-ee:1521/ORCLPDB1",
    "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=db)(PORT=1523))(CONNECT_DATA=(SERVICE_NAME=UTF8)))",
])
def test_preflight_preserves_complete_connection(dsn):
    target = TargetDatabase(dsn, "SYSTEM", "test-secret")
    result = SimpleNamespace(returncode=0, stdout="ORCLPDBUTF8:READ WRITE", stderr="")
    with patch("recovery_service.orchestrator.oracle_auto_import_runner.run_ssh_command", return_value=result) as run:
        _, details = OracleAutoImportRunner()._check_pdb(RemoteHost("ssh-host"), "utf-container", target, None)
    command = run.call_args.args[1]
    assert dsn in command
    assert "sqlplus -L -S" in command
    assert details["connection"] == dsn


@pytest.mark.parametrize("output", ["CDB$ROOT:READ WRITE", "ORCLPDBUTF8:READ ONLY", "ORA-12154: unresolved"])
def test_preflight_rejects_invalid_database_context(output):
    with patch("recovery_service.orchestrator.oracle_auto_import_runner.run_ssh_command",
               return_value=SimpleNamespace(returncode=0, stdout=output, stderr="")), pytest.raises(RuntimeError):
        OracleAutoImportRunner()._check_pdb(RemoteHost("ssh-host"), "utf-container",
                                          TargetDatabase("db:1523/UTF8", "SYSTEM", "test-secret"), None)


def test_resolver_priority_and_driver_use():
    from recovery_service.api.schemas.database_cleanup import CleanupConnection
    from recovery_service.common.oracle_connection import oracle_connection_dsn
    from recovery_service.services.database_cleanup import _oracle_conn

    conn = CleanupConnection(engine="oracle", host="db", port=1523, username="SYSTEM",
                             password="test-secret", service_name="SERVICE", database="LEGACY")
    assert oracle_connection_dsn(conn) == "db:1523/SERVICE"
    conn.service_name = None
    assert oracle_connection_dsn(conn) == "db:1523/LEGACY"
    conn.dsn = "//custom:1529/ACTUAL"
    assert oracle_connection_dsn(conn) == conn.dsn
    with patch("recovery_service.services.database_cleanup.oracledb.connect") as connect:
        _oracle_conn(conn)
    assert connect.call_args.kwargs["dsn"] == conn.dsn


def test_tool_sqlplus_fast_failure_and_password_mask(tmp_path):
    logger = tool.RunLogger(tmp_path, "SYSTEM", "test@secret")
    with patch.object(tool, "docker_exec", return_value=SimpleNamespace(returncode=0, stdout="OK")) as execute:
        tool.sqlplus("utf", "SYSTEM", "test@secret", "@db:1523/UTF8", "select 1 from dual;", logger)
    command = execute.call_args.args[1]
    assert "sqlplus -L -s" in command
    assert 'SYSTEM/"test@secret"@db:1523/UTF8' in command
    assert "test@secret" not in logger.mask(command)


def test_explicit_connect_error_is_not_ignored():
    args = SimpleNamespace(container="utf", username="SYSTEM", password="test-secret", connect="@db:1523/UTF8")
    with patch.object(tool, "sql_first_value", side_effect=RuntimeError("ORA-12154")), pytest.raises(RuntimeError, match="ORA-12154"):
        tool.resolve_pdb_connect(args, MagicMock())


@pytest.mark.asyncio
async def test_task_creation_freezes_selected_dsn():
    from recovery_service.api.schemas.database_cleanup import CleanupConnection
    from recovery_service.api.v1 import tasks

    conn = CleanupConnection(engine="oracle", host="unused", username="SYSTEM", password="secret",
                             dsn="//actual:1523/UTF8", ssh_host="ssh", ssh_user="root",
                             ssh_password="ssh-secret", container_name="utf")
    body = tasks.EmbeddedOracleTaskCreateRequest(import_source_mode="direct",
                                                direct_dmp_host_path="/data/dmp", manual_dumpfile="test.dmp")
    with patch.object(tasks, "_target_profile", new=AsyncMock(return_value=SimpleNamespace(id="profile"))), \
         patch.object(tasks, "profile_to_cleanup_connection", return_value=conn), \
         patch.object(tasks, "TaskRepository") as repo, \
         patch.object(tasks, "_enqueue_recovery_task"), \
         patch.object(tasks, "_task_response", side_effect=lambda task, **kw: task):
        repo.return_value.create = AsyncMock(side_effect=lambda task: task)
        task = await tasks.create_embedded_oracle_task(body, db=MagicMock())
    assert task.target_connection == conn.dsn
    assert task.options["professional_flow"]["target"]["connection"] == conn.dsn
    assert task.options["professional_flow"]["oracle_docker"]["container"] == "utf"


def test_runner_passes_full_connection_to_import_tool():
    runner = OracleAutoImportRunner()
    target = TargetDatabase("//db:1523/UTF8", "SYSTEM", "secret")
    with patch.object(runner, "preflight", return_value=[]), \
         patch.object(runner, "_install_script"), patch.object(runner, "_dump_args", return_value=[]), \
         patch.object(runner, "_remote_python", return_value="python3"), \
         patch.object(runner, "_verify_remote_script"), \
         patch.object(runner, "_read_json", return_value={"status": "dry-run"}), \
         patch.object(runner, "_read_text", return_value=""), \
         patch.object(runner, "_read_json_lines", return_value=[]), \
         patch.object(runner, "_list_log_artifacts", return_value=[]), \
         patch("recovery_service.orchestrator.oracle_auto_import_runner.run_ssh_command_stream",
               return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as run:
        result = runner.run(task_id="test", oracle_host=RemoteHost("ssh"), group=MagicMock(),
                            dmp_host_path="/dmp", dmp_container_path="/dmp", tablespace_container_path="/tables",
                            container="utf", target=target, target_user_password="secret",
                            oracle_home_in_container=None, oracle_directory=None, execute=False)
    assert result.success
    assert shlex.quote("@" + target.connection_string) in run.call_args.args[1]
    assert "--connect" in run.call_args.args[1]
    assert "--pdb" not in run.call_args.args[1]


@pytest.mark.parametrize("failed_stage", [None, "database", "server", "container", "pdb"])
def test_connection_test_reports_each_stage(failed_stage):
    from recovery_service.api.schemas.database_cleanup import CleanupConnection
    from recovery_service.services import database_cleanup as service

    conn = CleanupConnection(engine="oracle", host="db", username="SYSTEM", password="secret",
                             service_name="UTF8", ssh_host="ssh", ssh_user="root",
                             ssh_password="ssh-secret", container_name="utf")
    good = SimpleNamespace(returncode=0, stdout="true healthy /utf", stderr="")
    bad = SimpleNamespace(returncode=1, stdout="", stderr="failed: secret")
    with patch.object(service, "_oracle_conn") as database, \
         patch.object(service, "run_ssh_command", side_effect=[bad if failed_stage == "server" else good,
                                                              bad if failed_stage == "container" else good]), \
         patch.object(service.OracleAutoImportRunner, "_check_pdb") as pdb:
        database.return_value.__enter__.return_value.cursor.return_value.fetchone.return_value = ["Oracle 21c"]
        if failed_stage == "database":
            database.side_effect = RuntimeError("failed: secret")
        pdb.return_value = ("UTF8:READ WRITE", {"pdb": "UTF8", "open_mode": "READ WRITE"})
        if failed_stage == "pdb":
            pdb.side_effect = RuntimeError("ORA-12154: secret")
        result = service._test_oracle(conn)
    assert result.ok == (failed_stage is None)
    assert "secret" not in result.message
    stages = ["database", "server", "container", "pdb"]
    for index, stage in enumerate(stages):
        detail = result.details[stage]
        if failed_stage and index > stages.index(failed_stage):
            assert detail["skipped"]
        else:
            assert detail["ok"] == (stage != failed_stage)
    if failed_stage in (None, "pdb"):
        assert pdb.call_args.args[2].connection_string == "db:1521/UTF8"


def test_database_only_test_does_not_claim_restore_ready():
    from recovery_service.api.schemas.database_cleanup import CleanupConnection
    from recovery_service.services import database_cleanup as service

    conn = CleanupConnection(engine="oracle", host="db", username="SYSTEM", password="secret", service_name="UTF8")
    with patch.object(service, "_oracle_conn"), patch.object(service, "_test_oracle_server_access") as check:
        result = service._test_oracle(conn)
    assert result.ok
    assert result.details["server"]["skipped"]
    check.assert_not_called()


@pytest.mark.parametrize("password", ["te'st@secret", "bad\"password", "test-secret"])
def test_logger_masks_shell_quoted_password_without_parsing_login(tmp_path, password):
    logger = tool.RunLogger(tmp_path, "SYSTEM", password)
    command = shlex.quote(f'SYSTEM/"{password}"@db:1523/UTF8')
    assert password not in logger.mask(password)
    assert password.replace("'", "'\"'\"'") not in logger.mask(command)


@pytest.mark.asyncio
@pytest.mark.parametrize("routing_enabled", [True, False])
async def test_stop_selects_routed_connection_only_when_routing_enabled(routing_enabled):
    from recovery_service.api.v1 import tasks

    original = "//selected:1523/CUSTOM"
    routed = "utf:1521/UTF8"
    task = SimpleNamespace(id="test", state="importing", oracle_run_dir="/runs/test", oracle_job_name="ORS_TEST",
                           oracle_run_id="test", oracle_container="utf", target_admin_user="SYSTEM",
                           target_connection=original, stop_requested_at=None,
                           options={"embedded_oracle": True, "professional_flow": {
                               "target": {"connection": original}, "oracle_docker": {"host": "ssh"},
                               "character_set_routing": {"enabled": routing_enabled,
                                                         "utf8": {"container": "utf", "connection": routed}}}})
    with patch.object(tasks, "TaskRepository") as repo, \
         patch.object(tasks, "decrypt_secret", return_value="secret"), \
         patch.object(tasks, "record_task_event"), \
         patch.object(tasks, "record_audit", new=AsyncMock()), \
         patch.object(tasks, "_task_response", return_value={}), \
         patch.object(tasks.OracleAutoImportRunner, "stop", return_value={}) as stop:
        repo.return_value.get = AsyncMock(return_value=task)
        await tasks.stop_oracle_import_task("test", tasks.TaskStopRequest(reason="test"),
                                            request=MagicMock(), db=AsyncMock(), actor=MagicMock())
    assert stop.call_args.kwargs["connection"] == (routed if routing_enabled else original)
