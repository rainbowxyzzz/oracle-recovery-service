"""分步连通性检测：SSH → Docker → DMP 路径 → 目标库。"""

from dataclasses import dataclass, field

from recovery_service.core.docker_oracle import RemoteDockerOracle
from recovery_service.core.domain import RemoteHost, TargetDatabase
from recovery_service.engine.discovery.remote_scanner import RemoteScanner
from recovery_service.infrastructure.docker.remote_executor import RemoteDockerImpdpExecutor
from recovery_service.infrastructure.oracle.connectivity import check_connection
from recovery_service.settings import get_settings
from recovery_service.infrastructure.ssh.command_runner import run_ssh_command


@dataclass
class StepResult:
    step: str
    ok: bool
    message: str
    detail: dict = field(default_factory=dict)


def _ssh_host(body: dict) -> RemoteHost:
    return RemoteHost(
        host=body["ssh_host"],
        port=body.get("ssh_port", 22),
        username=body["ssh_user"],
        password=body.get("ssh_password", ""),
        private_key_path=body.get("ssh_private_key_path"),
    )


def validate_mysql(body: dict) -> StepResult:
    import pymysql

    s = get_settings()
    try:
        conn = pymysql.connect(
            host=s.mysql_host,
            port=s.mysql_port,
            user=s.mysql_user,
            password=s.mysql_password,
            database=s.mysql_database,
            charset=s.mysql_charset,
            connect_timeout=10,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.close()
        return StepResult(
            step="mysql",
            ok=True,
            message=f"MySQL 连接成功 ({s.mysql_host}:{s.mysql_port}/{s.mysql_database})",
        )
    except Exception as e:
        return StepResult(step="mysql", ok=False, message=str(e))


def validate_redis(body: dict) -> StepResult:
    import redis

    try:
        s = get_settings()
        r = redis.from_url(s.celery_broker_url)
        r.ping()
        return StepResult(step="redis", ok=True, message=f"Redis 连接成功 ({s.redis_host}:{s.redis_port})")
    except Exception as e:
        return StepResult(step="redis", ok=False, message=str(e))


def validate_ssh(body: dict) -> StepResult:
    host = _ssh_host(body)
    path = body.get("dmp_host_path", body.get("remote_directory", "/"))
    cmd = f"ls -la {path} 2>&1 | head -50"
    try:
        r = run_ssh_command(host, cmd, timeout=60)
        ok = r.returncode == 0
        return StepResult(
            step="ssh",
            ok=ok,
            message="SSH 连接成功" if ok else "SSH 或目录访问失败",
            detail={"stdout": r.stdout, "stderr": r.stderr, "returncode": r.returncode},
        )
    except Exception as e:
        return StepResult(step="ssh", ok=False, message=str(e))


def validate_dmp_files(body: dict) -> StepResult:
    host = _ssh_host(body)
    path = body.get("dmp_host_path") or body.get("remote_directory")
    if not path:
        return StepResult(step="dmp_files", ok=False, message="缺少 dmp_host_path")
    try:
        groups = RemoteScanner().scan(host, path)
        return StepResult(
            step="dmp_files",
            ok=len(groups) > 0,
            message=f"发现 {len(groups)} 个 DMP 卷组",
            detail={
                "groups": [
                    {
                        "group_id": g.group_id,
                        "dumps": [d.filename for d in g.dump_files],
                        "logs": [d.filename for d in g.log_files],
                    }
                    for g in groups
                ]
            },
        )
    except Exception as e:
        return StepResult(step="dmp_files", ok=False, message=str(e))


def validate_docker(body: dict) -> StepResult:
    host = _ssh_host(body)
    try:
        docker = _docker_from_body(body)
        if not docker:
            return StepResult(step="docker", ok=False, message="缺少 docker_container 等配置")
        ex = RemoteDockerImpdpExecutor(host, docker)
        r = ex.check_container()
        ok = "ORACLE_DOCKER_OK" in r.stdout and r.returncode == 0
        return StepResult(
            step="docker",
            ok=ok,
            message="Docker 容器可执行命令" if ok else "docker exec 失败",
            detail={"stdout": r.stdout, "stderr": r.stderr},
        )
    except Exception as e:
        return StepResult(step="docker", ok=False, message=str(e))


def validate_container_path(body: dict) -> StepResult:
    host = _ssh_host(body)
    try:
        docker = _docker_from_body(body)
        if not docker:
            return StepResult(step="container_path", ok=False, message="缺少 Docker 配置")
        ex = RemoteDockerImpdpExecutor(host, docker)
        r = ex.list_container_dmp_dir()
        has_dmp = ".dmp" in (r.stdout or "").lower()
        return StepResult(
            step="container_path",
            ok=r.returncode == 0 and has_dmp,
            message="容器内目录可读且含 dmp" if has_dmp else "容器内目录无 dmp 或不可访问",
            detail={"stdout": r.stdout, "stderr": r.stderr},
        )
    except Exception as e:
        return StepResult(step="container_path", ok=False, message=str(e))


def validate_impdp(body: dict) -> StepResult:
    host = _ssh_host(body)
    try:
        docker = _docker_from_body(body)
        if not docker:
            return StepResult(step="impdp", ok=False, message="缺少 Docker 配置")
        ex = RemoteDockerImpdpExecutor(host, docker)
        r = ex.impdp_help()
        ok = r.returncode == 0 or "Import" in (r.stdout + r.stderr)
        return StepResult(
            step="impdp",
            ok=ok,
            message="容器内 impdp 可用" if ok else "impdp 不可用",
            detail={"stdout": (r.stdout or "")[:2000], "stderr": (r.stderr or "")[:2000]},
        )
    except Exception as e:
        return StepResult(step="impdp", ok=False, message=str(e))


def validate_target_db(body: dict) -> StepResult:
    target = TargetDatabase(
        connection_string=body["target_connection"],
        admin_user=body["target_admin_user"],
        admin_password=body.get("target_admin_password", ""),
    )
    try:
        ok = check_connection(target)
        return StepResult(
            step="target_db",
            ok=ok,
            message="目标库连接成功（从恢复服务所在网络）" if ok else "目标库连接失败",
        )
    except Exception as e:
        return StepResult(step="target_db", ok=False, message=str(e))


def validate_all(body: dict) -> list[StepResult]:
    steps = [
        validate_mysql,
        validate_redis,
        validate_ssh,
        validate_dmp_files,
        validate_docker,
        validate_container_path,
        validate_impdp,
    ]
    if body.get("target_connection"):
        steps.append(validate_target_db)

    results: list[StepResult] = []
    for fn in steps:
        r = fn(body)
        results.append(r)
        if not r.ok and body.get("stop_on_first_error", True):
            break
    return results


def _docker_from_body(body: dict) -> RemoteDockerOracle | None:
    return RemoteDockerOracle.from_options(
        {"execution": body.get("execution", body)},
        body.get("dmp_host_path") or body.get("remote_directory") or "/",
    )
