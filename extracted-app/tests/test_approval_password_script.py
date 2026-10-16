import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from recovery_service.common.security import encrypt_secret

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/find-approval-doris-password.sh"
BASH = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")


@pytest.fixture
def script():
    source = SCRIPT.read_text(encoding="utf-8").split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    module = {"__name__": "password_script_test"}
    exec(compile(source, str(SCRIPT), "exec"), module)  # noqa: S102 - trusted local script under test
    return module


@pytest.mark.parametrize("password", ["a' $`\\中文\n\t", " ordinary "])
def test_recover_encrypted_and_legacy_plain(script, password):
    assert script["recover_password"](encrypt_secret(password, "key"), "key") == password
    assert script["recover_password"](password, "") == password


@pytest.mark.parametrize("key", ["wrong", ""])
def test_bad_key_never_returns_cipher(script, key):
    cipher = encrypt_secret("fixture-secret", "right")
    with pytest.raises(ValueError):
        script["recover_password"](cipher, key)


def test_empty_or_uncertain_plaintext_fails(script):
    for stored, key in [("", ""), ("old-plain", "new-key")]:
        with pytest.raises(ValueError):
            script["recover_password"](stored, key)


def test_parameterized_exact_successful_username_lookup(script):
    session = Mock()
    username = "张三_1234_1016' OR 1=1 --"
    session.execute.return_value.mappings.return_value.all.return_value = []
    assert script["lookup"](session, username) == []
    statement, params = session.execute.call_args.args
    sql = str(statement)
    assert params == {"username": username}
    assert username not in sql
    assert "CAST(:username AS BINARY)" in sql
    assert "l.step_key = 'internal_grant' AND l.status = 'success'" in sql
    assert "$.username" in sql and "JOIN approval_authorization_configs" in sql


def test_no_match_does_not_expose_other_configs(script, capsys):
    assert script["display"]([], "absent", "") == 3
    assert "未找到" in capsys.readouterr().err


def test_multiple_configs_and_failed_decryption(script, capsys):
    password = "test'\\\n中文"
    rows = [{"id": "one", "name": "任务一", "doris_connection_id": "conn1", "updated_at": "now",
             "default_doris_password_enc": encrypt_secret(password, "key")},
            {"id": "two", "name": "任务二", "doris_connection_id": "conn2", "updated_at": "old",
             "default_doris_password_enc": encrypt_secret("never-output", "wrong")}]
    assert script["display"](rows, "user", "key") == 4
    output = capsys.readouterr().out
    assert "匹配多个" in output and "未验证" in output
    assert json.dumps(password, ensure_ascii=False) in output
    assert "conn1" in output and "conn2" in output
    assert "never-output" not in output and "gAAAA" not in output


def test_script_format():
    raw = SCRIPT.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r\n" not in raw


@pytest.mark.skipif(not BASH, reason="Bash required")
def test_shell_arguments_and_exit_code(tmp_path):
    mock = tmp_path / "docker"
    mock.write_text('#!/bin/sh\n[ "$1" = exec ] && [ "$2" = -i ] && [ "$3" = custom-api ] && '
                    '[ "$4" = python ] && [ "$5" = - ] && [ "$6" = \'张三 $x ; test\' ] || exit 97\n'
                    'cat >/dev/null\nexit 4\n', encoding="utf-8", newline="\n")
    mock.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"])
    result = subprocess.run([BASH, str(SCRIPT), "张三 $x ; test", "custom-api"], env=env, capture_output=True, check=False)
    assert result.returncode == 4, result.stderr
    result = subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, check=False)
    assert result.returncode == 2
