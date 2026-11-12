#!/usr/bin/env python3
"""Configure an existing Docker Run deployment; Python 3.7+, standard library only."""
import argparse
import copy
import datetime
import getpass
import http.client
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

URL_KEYS = {"REDIS_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"}


def docker(*args, data=None, allow_error=False):
    result = subprocess.run(["docker", *args], input=data, capture_output=True, check=False)
    if result.returncode and not allow_error:
        # Docker error text can contain environment values or connection URLs.
        raise RuntimeError("Docker operation failed: " + args[0])
    return result.stdout + (result.stderr if allow_error else b"")


def inspect(name):
    return json.loads(docker("inspect", name))[0]


def env_map(lines):
    return dict(line.split("=", 1) for line in lines if "=" in line)


def read_env(text):
    values = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)=(.*)", line)
        if not match:
            raise ValueError("Unsupported .env syntax; use one KEY=value per line")
        parts = shlex.split(match[2], comments=True)
        if len(parts) > 1:
            raise ValueError("Unquoted whitespace in .env")
        values[match[1]] = parts[0] if parts else ""
    return values


def change_url(url, password):
    parsed = urlsplit(url)
    if parsed.scheme != "redis" or not parsed.hostname or parsed.username not in (None, ""):
        raise ValueError("Only password-authenticated redis:// URLs are supported")
    authority = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit(parsed._replace(netloc=":" + quote(password, safe="") + "@" + authority))


def update_env(text, values):
    # These lines are read both by Docker --env-file and by POSIX shell.
    if any(not re.fullmatch(r"[A-Za-z0-9_:/@%.,?=+\[\]-]+", value) for value in values.values()):
        raise ValueError("URL contains shell-sensitive characters; simplify its query before updating")
    remaining = dict(values)
    lines = []
    for line in text.splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)=", line)
        key = match[1] if match else None
        if key in values:
            if key in remaining:
                lines.append(key + "=" + remaining.pop(key))
        else:
            lines.append(line)
    lines.extend(key + "=" + value for key, value in remaining.items())
    return "\n".join(lines) + "\n"


def write_private(path, data):
    path.write_text(data, encoding="utf-8")
    path.chmod(0o600)


class Engine(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=120)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


def create(name, body):
    conn = Engine()
    try:
        conn.request("POST", "/v1.27/containers/create?name=" + quote(name),
                     json.dumps(body), {"Content-Type": "application/json"})
        response = conn.getresponse()
        payload = response.read()
        if response.status != 201:
            raise RuntimeError("Docker create failed for " + name)
        return json.loads(payload)["Id"]
    finally:
        conn.close()


def recreate_body(item):
    body = copy.deepcopy(item["Config"])
    body["Image"] = item["Image"]
    body["HostConfig"] = copy.deepcopy(item["HostConfig"])
    # Anonymous volumes must be rebound explicitly, or Docker creates empty ones.
    binds = body["HostConfig"].setdefault("Binds", None) or []
    destinations = {entry.split(":")[1] for entry in binds}
    for mount in item["Mounts"]:
        if mount["Destination"] not in destinations:
            source = mount.get("Name") or mount["Source"]
            binds.append(source + ":" + mount["Destination"] + (":rw" if mount["RW"] else ":ro"))
    body["HostConfig"]["Binds"] = binds
    endpoints = {}
    for network, endpoint in item["NetworkSettings"]["Networks"].items():
        aliases = [a for a in (endpoint.get("Aliases") or [])
                   if a not in (item["Id"], item["Id"][:12])]
        endpoints[network] = {"Aliases": aliases}
        if endpoint.get("IPAMConfig"):
            endpoints[network]["IPAMConfig"] = endpoint["IPAMConfig"]
    body["NetworkingConfig"] = {"EndpointsConfig": endpoints}
    return body


def redis_cli(name, password, *args):
    # Feed credentials on stdin; do not expose them in host command arguments.
    command = 'IFS= read -r REDISCLI_AUTH; export REDISCLI_AUTH; exec redis-cli --raw "$@"'
    return docker("exec", "-i", name, "sh", "-c", command, "redis-auth", *args,
                  data=(password + "\n").encode(), allow_error=True).decode().strip()


def wait_redis(name, password):
    for _ in range(30):
        try:
            if redis_cli(name, password, "PING") == "PONG":
                return
        except RuntimeError:
            pass
        time.sleep(1)
    raise RuntimeError("Redis authentication/readiness check failed")


def check_client(item, idle=False):
    name = item["Name"][1:]
    print("Checking " + name, flush=True)
    worker = name.startswith("oracle-recovery-worker")
    code = """
import json, socket, sys
from recovery_service.settings import get_settings
import redis
s = get_settings()
for url in (s.celery_broker_url, s.celery_result_backend):
    redis.from_url(url, socket_connect_timeout=5, socket_timeout=5).ping()
if sys.argv[1] == 'worker':
    from recovery_service.workers.celery_app import celery_app
    node = 'celery@' + socket.gethostname()
    inspector = celery_app.control.inspect(destination=[node], timeout=5)
    replies = inspector.ping()
    assert replies and replies.get(node, {}).get('ok') == 'pong', 'Worker not responding'
    if sys.argv[2] == 'idle':
        for method in ('active', 'reserved', 'scheduled'):
            replies = getattr(inspector, method)()
            assert replies is not None and node in replies, 'Worker status unknown'
            assert not replies[node], 'Worker has unfinished tasks'
else:
    import urllib.request
    with urllib.request.urlopen('http://127.0.0.1:%s/api/v1/health' % s.api_port, timeout=5) as response:
        assert response.status == 200, 'API not ready'
print('REDIS_AUTH_CHECK_OK')
"""
    output = docker("exec", name, "python", "-c", code,
                    "worker" if worker else "api", "idle" if idle else "ready")
    if not output.rstrip().endswith(b"REDIS_AUTH_CHECK_OK"):
        raise RuntimeError("Client check failed: " + name)


def check_idle(clients):
    for item in clients:
        if item["State"]["Running"] and item["Name"].startswith("/oracle-recovery-worker"):
            try:
                check_client(item, idle=True)
            except RuntimeError:
                raise RuntimeError("Worker busy or unreachable; retry when idle: " + item["Name"][1:]) from None


def stop_client(name):
    if not inspect(name)["State"]["Running"]:
        return
    print("Stopping " + name + " (graceful shutdown)", flush=True)
    docker("kill", "--signal=TERM", name)
    # Celery warm shutdown waits for any task acquired after the idle check.
    count = 0
    while inspect(name)["State"]["Running"]:
        time.sleep(1)
        count += 1
        if count % 30 == 0:
            print("Waiting for " + name + " to finish shutdown...", flush=True)


def stop_clients(clients):
    api = [i for i in clients if i["Name"] == "/oracle-recovery-api"]
    for item in api:
        stop_client(item["Name"][1:])
    check_idle(clients)
    for item in clients:
        if item not in api:
            stop_client(item["Name"][1:])


def wait_client(item):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if not inspect(item["Name"][1:])["State"]["Running"]:
            raise RuntimeError("Container exited during startup: " + item["Name"][1:])
        try:
            check_client(item)
            return
        except RuntimeError:
            time.sleep(2)
    raise RuntimeError("Client did not become ready: " + item["Name"][1:])


def rollback(folder):
    state = json.loads((folder / "state.json").read_text())
    if all(inspect(e["id"])["Name"] == "/" + e["name"] for e in state["containers"]):
        # No replacement happened: a busy-worker recheck must not prevent API recovery.
        write_private(Path(state["env_file"]), (folder / "env.before").read_text())
        for entry in state["containers"]:
            if entry["running"] and not inspect(entry["id"])["State"]["Running"]:
                docker("start", entry["name"])
        print("原容器及配置已恢复。")
        return
    names = docker("ps", "-a", "--format", "{{.Names}}").decode().splitlines()
    current_clients = [inspect(entry["name"]) for entry in state["containers"]
                       if entry["name"] != "oracle-recovery-redis" and entry["name"] in names]
    check_idle(current_clients)
    stop_clients(current_clients)
    for entry in reversed(state["containers"]):
        old = inspect(entry["id"])
        if old["Name"] == "/" + entry["name"]:
            continue
        names = docker("ps", "-a", "--format", "{{.Names}}").decode().splitlines()
        if entry["name"] in names:
            current = inspect(entry["name"])
            if (current["Config"].get("Labels") or {}).get("oracle-recovery.redis-auth") != str(folder):
                raise RuntimeError("Replacement identity changed; refusing rollback")
            if current["State"]["Running"]:
                docker("stop", "-t", "60", entry["name"])
            docker("rm", entry["name"])
        docker("rename", entry["id"], entry["name"])
    write_private(Path(state["env_file"]), (folder / "env.before").read_text())
    for entry in state["containers"]:
        if entry["running"]:
            docker("start", entry["name"])
    print("已回滚：旧容器、配置和原运行状态已恢复。")


def configure(env_file):
    text = env_file.read_text(encoding="utf-8-sig")
    values = read_env(text)
    name = values.get("REDIS_CONTAINER_NAME") or "oracle-recovery-redis"
    if name != "oracle-recovery-redis" or values.get("REDIS_HOST", "redis") not in ("redis", name):
        raise ValueError("Tool supports the project's local oracle-recovery-redis only")
    all_items = json.loads(docker("inspect", *docker("ps", "-aq").decode().split()))
    redis = next(item for item in all_items if item["Name"] == "/" + name)
    clients = [item for item in all_items if "-auth-backup-" not in item["Name"] and (
        item["Name"] == "/oracle-recovery-api" or item["Name"] == "/oracle-recovery-worker"
        or item["Name"].startswith("/oracle-recovery-worker-"))]
    if not clients:
        raise ValueError("Project API/Worker containers not found")
    if any(i["State"]["Running"] for i in clients) and not redis["State"]["Running"]:
        raise ValueError("Clients are running but Redis is stopped; restore Redis first")
    check_idle(clients)
    if redis["Config"]["Cmd"] not in (
        ["redis-server"], ["redis-server", "/usr/local/etc/redis/project-auth.conf"]
    ):
        raise ValueError("Custom Redis command detected; manual configuration required")
    for item in [redis, *clients]:
        if item["HostConfig"].get("AutoRemove") or item["HostConfig"].get("Links"):
            raise ValueError("AutoRemove/legacy Links containers are unsupported")
    data_mount = next((m for m in redis["Mounts"] if m["Destination"] == "/data"), None)
    if not data_mount:
        raise ValueError("Redis must have a persistent /data mount")
    for other in all_items:
        own_backup = other["Name"].startswith("/" + name + "-auth-backup-") and not other["State"]["Running"]
        if other["Id"] != redis["Id"] and not own_backup and any(m["Source"] == data_mount["Source"] for m in other["Mounts"]):
            raise ValueError("Redis data is shared with another container; aborting")
    password = getpass.getpass("请输入 Redis 新密码（至少20位，字母/数字/_-）：")
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", password):
        raise ValueError("密码至少20位，只能使用字母、数字、下划线或减号")
    if password != getpass.getpass("请再次输入密码："):
        raise ValueError("两次密码不一致")
    changes = {"REDIS_PASSWORD": password}
    hosts = {"redis", name} | {e["IPAddress"] for e in redis["NetworkSettings"]["Networks"].values()
                              if e.get("IPAddress")}
    for env in [values, *(env_map(i["Config"].get("Env") or []) for i in clients)]:
        if env.get("REDIS_HOST", "redis") not in hosts:
            raise ValueError("Client Redis host differs from the local Redis container")
        for key in URL_KEYS:
            if env.get(key) and urlsplit(env[key]).hostname not in hosts:
                raise ValueError("Explicit Redis URL targets another server")
    for key in URL_KEYS:
        if values.get(key):
            changes[key] = change_url(values[key], password)
    updated_text = update_env(text, changes)
    bodies = []
    for item in clients:
        body = recreate_body(item)
        env = env_map(body["Env"])
        env["REDIS_PASSWORD"] = password
        for key in URL_KEYS:
            url = env.get(key) or values.get(key)
            env[key] = change_url(url, password) if url else ""
        body["Env"] = [key + "=" + value for key, value in env.items()]
        bodies.append((item, body))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S-UTC")
    folder = env_file.parent / ("redis-auth-backup-" + stamp)
    folder.mkdir(mode=0o700)
    write_private(folder / "env.before", text)
    write_private(folder / "inspect.json", json.dumps([redis, *clients], indent=2))
    state = {"env_file": str(env_file), "containers": [
        {"id": item["Id"], "name": item["Name"][1:], "running": item["State"]["Running"]}
        for item in [redis, *clients]]}
    write_private(folder / "state.json", json.dumps(state))
    print("Backup: " + str(folder), flush=True)
    redis_started = False
    replaced = []
    try:
        print("自动停止服务并更新 Redis 密码…", flush=True)
        stop_clients(clients)
        if not redis["State"]["Running"]:
            docker("start", name)
            redis_started = True
        old_password = env_map(redis["Config"].get("Env") or []).get("REDIS_PASSWORD", values.get("REDIS_PASSWORD", ""))
        wait_redis(name, old_password)
        if redis_cli(name, old_password, "SAVE") != "OK":
            raise RuntimeError("Redis SAVE failed")
        docker("cp", name + ":/data/.", str(folder / "redis-data"))
        docker("stop", "-t", "60", name)
        # Preserve existing Redis server options before appending authentication.
        conf = ""
        if redis["Config"]["Cmd"] == ["redis-server", "/usr/local/etc/redis/project-auth.conf"]:
            mount = next(m for m in redis["Mounts"] if m["Destination"] == "/usr/local/etc/redis/project-auth.conf")
            conf = Path(mount["Source"]).read_text()
        conf = re.sub(r"(?m)^requirepass\s+.*$", "", conf)
        conf += "\ndir /data\nrequirepass " + password + "\n"
        config_file = folder / "redis.conf"
        write_private(config_file, conf)
        # Official Redis entrypoint drops to its image's redis user.
        config_file.chmod(0o644)
        body = recreate_body(redis)
        body["Cmd"] = ["redis-server", "/usr/local/etc/redis/project-auth.conf"]
        body["Env"] = [e for e in body.get("Env", []) if not e.startswith("REDIS_PASSWORD=")]
        body["Env"].append("REDIS_PASSWORD=" + password)
        binds = body["HostConfig"]["Binds"]
        body["HostConfig"]["Binds"] = [b for b in binds if b.split(":")[1] != "/usr/local/etc/redis/project-auth.conf"]
        body["HostConfig"]["Binds"].append(str(config_file) + ":/usr/local/etc/redis/project-auth.conf:ro")
        bodies.insert(0, (redis, body))
        for item, body in bodies:
            container_name = item["Name"][1:]
            if item is not redis:
                body["Image"] = docker("commit", item["Id"]).decode().strip()
            body["Labels"] = dict(body.get("Labels") or {})
            body["Labels"]["oracle-recovery.redis-auth"] = str(folder)
            docker("rename", item["Id"], container_name + "-auth-backup-" + folder.name.split("backup-")[1])
            create(container_name, body)
            replaced.append(container_name)
        write_private(env_file, updated_text)
        docker("start", name)
        wait_redis(name, password)
        if "NOAUTH" not in redis_cli(name, "", "PING"):
            raise RuntimeError("Unauthenticated access is still allowed")
        if redis_cli(name, password + "_wrong", "PING") == "PONG":
            raise RuntimeError("Wrong password was accepted")
        docker("restart", "-t", "60", name)
        wait_redis(name, password)
        for item in clients:
            if item["State"]["Running"]:
                docker("start", item["Name"][1:])
                wait_client(item)
        if not redis["State"]["Running"]:
            docker("stop", "-t", "60", name)
        print("配置完成：Redis 认证已验证，原来运行的服务已自动恢复。")
        print("原来停止的服务保持停止。备份目录：" + str(folder))
    except BaseException:
        for container_name in replaced:
            if container_name != name:
                stop_client(container_name)
        if name in replaced:
            docker("stop", "-t", "60", name)
        rollback(folder)
        if redis_started and inspect(name)["State"]["Running"]:
            docker("stop", "-t", "60", name)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.rollback:
            rollback(args.rollback.resolve())
        else:
            configure(args.env_file.resolve(strict=True))
    except (OSError, ValueError, RuntimeError, StopIteration, KeyboardInterrupt) as exc:
        print("FAILED: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
