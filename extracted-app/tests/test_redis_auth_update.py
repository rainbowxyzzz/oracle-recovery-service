import copy
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy/configure-redis-auth.py"
spec = importlib.util.spec_from_file_location("redis_auth_update", SCRIPT)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)
PASSWORD = "Redis_Test_Password_2026"


def item(name, running=False):
    return {
        "Id": name + "-id", "Name": "/" + name, "Image": "sha256:original",
        "State": {"Running": running},
        "Config": {"Cmd": ["redis-server"], "Env": ["KEEP=value"], "Labels": {}},
        "HostConfig": {"Binds": None, "RestartPolicy": {"Name": "unless-stopped"}},
        "Mounts": [{"Name": "existing-data", "Source": "/vol/existing-data",
                    "Destination": "/data", "RW": True}],
        "NetworkSettings": {"Networks": {"project-net": {"Aliases": [name, name + "-id"]}}},
    }


def test_env_update_preserves_unrelated_values_and_url_targets():
    text = "# keep\nMYSQL_PASSWORD='old secret'\nREDIS_PASSWORD=\nREDIS_PASSWORD=duplicate\n"
    result = tool.update_env(text, {"REDIS_PASSWORD": PASSWORD})
    assert result.startswith("# keep\nMYSQL_PASSWORD='old secret'\n")
    assert result.count("REDIS_PASSWORD=") == 1
    assert tool.read_env(result)["REDIS_PASSWORD"] == PASSWORD
    assert tool.change_url("redis://:old@redis:6380/7?socket_timeout=5", PASSWORD) == (
        "redis://:" + PASSWORD + "@redis:6380/7?socket_timeout=5")
    for invalid in ["http://redis/0", "redis://username:password@redis/0"]:
        with pytest.raises(ValueError):
            tool.change_url(invalid, PASSWORD)


def test_recreate_preserves_anonymous_volume_network_and_compatibility():
    original = item("redis")
    original["HostConfig"]["SecurityOpt"] = ["seccomp=unconfined"]
    original["HostConfig"]["PidsLimit"] = -1
    body = tool.recreate_body(original)
    assert body["HostConfig"]["Binds"] == ["existing-data:/data:rw"]
    assert body["HostConfig"]["SecurityOpt"] == ["seccomp=unconfined"]
    assert body["HostConfig"]["PidsLimit"] == -1
    assert body["NetworkingConfig"]["EndpointsConfig"] == {"project-net": {"Aliases": ["redis"]}}
    assert original["HostConfig"]["Binds"] is None


class FakeDocker:
    def __init__(self, redis_running=False, fail_create=False):
        self.items = [item("oracle-recovery-redis", redis_running), item("oracle-recovery-api")]
        self.items[1]["Mounts"] = []
        self.items[1]["Config"]["Env"] += ["CELERY_BROKER_URL=redis://redis:6379/0"]
        self.fail_create = fail_create
        self.calls = []

    def inspect(self, name):
        return next(x for x in self.items if x["Id"] == name or x["Name"] == "/" + name)

    def run(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == "ps":
            field = "Name" if "--format" in args else "Id"
            return "\n".join(x[field].lstrip("/") for x in self.items).encode()
        if args[0] == "inspect":
            return json.dumps(self.items).encode()
        if args[0] in ("start", "restart", "stop"):
            self.inspect(args[-1])["State"]["Running"] = args[0] != "stop"
        elif args[0] == "kill":
            self.inspect(args[-1])["State"]["Running"] = False
        elif args[0] == "rename":
            self.inspect(args[1])["Name"] = "/" + args[2]
        elif args[0] == "rm":
            self.items.remove(self.inspect(args[1]))
        elif args[0] == "commit":
            return b"sha256:hotfix-snapshot"
        return b""

    def create(self, name, body):
        if self.fail_create and name == "oracle-recovery-api":
            raise RuntimeError("injected create failure")
        new = item(name)
        new["Id"] = name + "-new-id"
        new["Config"] = copy.deepcopy(body)
        new["Image"] = body["Image"]
        self.items.append(new)
        return new["Id"]


@pytest.mark.parametrize("redis_running", [False, True])
@pytest.mark.parametrize("fail_create", [False, True])
def test_configuration_and_partial_failure_rollback(tmp_path, redis_running, fail_create):
    path = tmp_path / ".env"
    old = "REDIS_HOST=redis\nMYSQL_PASSWORD=unchanged\n"
    path.write_text(old)
    fake = FakeDocker(redis_running, fail_create)
    def cli(name, password, *args):
        if args == ("SAVE",):
            return "OK"
        if not password:
            return "NOAUTH Authentication required"
        return "WRONGPASS" if password.endswith("_wrong") else "PONG"
    with patch.object(tool, "docker", fake.run), patch.object(tool, "inspect", fake.inspect), \
         patch.object(tool, "create", fake.create), patch.object(tool, "wait_redis"), \
         patch.object(tool, "redis_cli", cli), patch.object(tool.getpass, "getpass", return_value=PASSWORD):
        if fail_create:
            with pytest.raises(RuntimeError, match="injected"):
                tool.configure(path)
            assert path.read_text() == old
            assert fake.inspect("oracle-recovery-api")["Id"] == "oracle-recovery-api-id"
        else:
            tool.configure(path)
            assert tool.read_env(path.read_text())["REDIS_PASSWORD"] == PASSWORD
            api = fake.inspect("oracle-recovery-api")
            assert api["Image"] == "sha256:hotfix-snapshot"
            assert "CELERY_BROKER_URL=redis://:" + PASSWORD + "@redis:6379/0" in api["Config"]["Env"]
            assert not api["State"]["Running"]
            assert any(call[0] == "restart" for call in fake.calls)
        assert fake.inspect("oracle-recovery-redis")["State"]["Running"] == redis_running


def test_busy_workers_abort_before_password_or_mutation(tmp_path):
    path = tmp_path / ".env"
    path.write_text("REDIS_HOST=redis\n")
    fake = FakeDocker(redis_running=True)
    worker = item("oracle-recovery-worker-sm4", True)
    worker["Mounts"] = []
    fake.items.append(worker)
    with patch.object(tool, "docker", fake.run), patch.object(tool.getpass, "getpass") as prompt, \
         patch.object(tool, "check_client", side_effect=RuntimeError("busy")):
        with pytest.raises(RuntimeError, match="Worker busy"):
            tool.configure(path)
        prompt.assert_not_called()
    assert not list(tmp_path.glob("redis-auth-backup-*"))
    assert not any(call[0] in {"kill", "stop", "rename"} for call in fake.calls)


def test_external_url_aborts_before_creating_backups(tmp_path):
    path = tmp_path / ".env"
    path.write_text("REDIS_HOST=redis\nCELERY_BROKER_URL=redis://another-server/0\n")
    fake = FakeDocker()
    with patch.object(tool, "docker", fake.run), patch.object(tool.getpass, "getpass", return_value=PASSWORD), \
         pytest.raises(ValueError, match="another server"):
        tool.configure(path)
    assert not list(tmp_path.glob("redis-auth-backup-*"))


def test_auth_backup_clients_are_not_recreated_again(tmp_path):
    path = tmp_path / ".env"
    path.write_text("REDIS_HOST=redis\n")
    fake = FakeDocker()
    backup = item("oracle-recovery-worker-sm4-auth-backup-previous")
    backup["Mounts"] = []
    fake.items.append(backup)
    def cli(name, password, *args):
        if args == ("SAVE",):
            return "OK"
        return "NOAUTH" if not password else "PONG" if password == PASSWORD else "WRONGPASS"
    with patch.object(tool, "docker", fake.run), patch.object(tool, "inspect", fake.inspect), \
         patch.object(tool, "create", fake.create), patch.object(tool, "wait_redis"), \
         patch.object(tool, "redis_cli", cli), patch.object(tool.getpass, "getpass", return_value=PASSWORD):
        tool.configure(path)
    assert not any(call[0] == "commit" and call[1] == backup["Id"] for call in fake.calls)


def test_script_is_linux_text_and_python37_syntax():
    import ast
    raw = SCRIPT.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    ast.parse(raw.decode(), feature_version=(3, 7))


@pytest.mark.parametrize("fail_create", [False, True])
def test_one_command_stops_and_restores_running_services(tmp_path, fail_create):
    path = tmp_path / ".env"
    path.write_text("REDIS_HOST=redis\n")
    fake = FakeDocker(redis_running=True, fail_create=fail_create)
    fake.items[1]["State"]["Running"] = True
    worker = item("oracle-recovery-worker-sm4", True)
    worker["Mounts"] = []
    fake.items.append(worker)
    idle_worker = item("oracle-recovery-worker-sql", False)
    idle_worker["Mounts"] = []
    fake.items.append(idle_worker)
    def cli(name, password, *args):
        if args == ("SAVE",):
            return "OK"
        return "NOAUTH" if not password else "PONG" if password == PASSWORD else "WRONGPASS"
    with patch.object(tool, "docker", fake.run), patch.object(tool, "inspect", fake.inspect), \
         patch.object(tool, "create", fake.create), patch.object(tool, "wait_redis"), \
         patch.object(tool, "check_client"), patch.object(tool, "wait_client") as ready, \
         patch.object(tool, "redis_cli", cli), patch.object(tool.getpass, "getpass", return_value=PASSWORD):
        if fail_create:
            with pytest.raises(RuntimeError, match="injected"):
                tool.configure(path)
        else:
            tool.configure(path)
            assert ready.call_count == 2
    for name in ["oracle-recovery-api", "oracle-recovery-worker-sm4", "oracle-recovery-redis"]:
        assert fake.inspect(name)["State"]["Running"]
    assert not fake.inspect("oracle-recovery-worker-sql")["State"]["Running"]
    signals = [call for call in fake.calls if call[0] == "kill"]
    assert signals[0][-1] == "oracle-recovery-api"
    assert signals[1][-1] == "oracle-recovery-worker-sm4"
    assert not any(call[0] == "rm" and "-v" in call for call in fake.calls)


def test_task_appearing_after_api_stop_restores_api(tmp_path):
    path = tmp_path / ".env"
    path.write_text("REDIS_HOST=redis\n")
    fake = FakeDocker(redis_running=True)
    fake.items[1]["State"]["Running"] = True
    worker = item("oracle-recovery-worker-sm4", True)
    worker["Mounts"] = []
    fake.items.append(worker)
    with patch.object(tool, "docker", fake.run), patch.object(tool, "inspect", fake.inspect), \
         patch.object(tool, "check_client", side_effect=[None, RuntimeError("task acquired")]), \
         patch.object(tool.getpass, "getpass", return_value=PASSWORD), \
         pytest.raises(RuntimeError, match="Worker busy"):
        tool.configure(path)
    assert fake.inspect("oracle-recovery-api")["State"]["Running"]
    assert fake.inspect("oracle-recovery-worker-sm4")["State"]["Running"]
    assert not any(call[0] in {"rename", "commit", "cp"} for call in fake.calls)
    assert path.read_text() == "REDIS_HOST=redis\n"
