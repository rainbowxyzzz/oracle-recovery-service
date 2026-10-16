#!/bin/sh
# Read-only: uses the running application's metadata DB and credential key.
set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ] || [ -z "$1" ]; then
    printf '%s\n' '用法: sh find-approval-doris-password.sh "Doris用户名" [API容器名]' >&2
    exit 2
fi

container=${2:-oracle-recovery-api}
docker exec -i "$container" python - "$1" <<'PY'
import json
import sys

from sqlalchemy import text

from recovery_service.common.security import decrypt_secret
from recovery_service.db.session import get_sync_session_factory
from recovery_service.settings import get_settings


LOOKUP_SQL = """
SELECT DISTINCT c.id, c.name, c.doris_connection_id, c.updated_at,
       c.default_doris_password_enc
FROM approval_authorization_step_logs AS l
JOIN approval_authorization_configs AS c ON c.id = l.config_id
WHERE l.step_key = 'internal_grant' AND l.status = 'success'
  AND CAST(JSON_UNQUOTE(JSON_EXTRACT(l.extracted_data, '$.username')) AS BINARY)
      = CAST(:username AS BINARY)
ORDER BY c.id
"""


def recover_password(stored, key):
    if not stored:
        raise ValueError("该任务当前未保存默认密码。")
    password = decrypt_secret(stored, key)
    # The application helper returns ciphertext unchanged on InvalidToken.
    if stored.startswith("gAAAA") and password == stored:
        raise ValueError("无法解密：需要保存时的 CREDENTIAL_ENCRYPTION_KEY，未将密文当作密码输出。")
    if key and password == stored:
        raise ValueError("无法确认解密结果：请核对原凭据密钥或历史明文存储状态。")
    return password


def lookup(session, username):
    return session.execute(text(LOOKUP_SQL), {"username": username}).mappings().all()


def display(rows, username, key):
    if not rows:
        print("未找到该用户名的成功审批授权记录及现存配置；日志/配置可能已清理，或用户并非由该模块创建。", file=sys.stderr)
        return 3
    print("查询用户：", json.dumps(username, ensure_ascii=False))
    print("以下为关联任务当前保存的候选密码，未验证 Doris 登录。默认密码已修改、用户原先存在或外部改密时可能不适用。")
    if len(rows) > 1:
        print("匹配多个任务/连接，请按配置和 Doris 连接 ID 区分，不能认定为同一个账号。")
    failed = False
    for row in rows:
        print("任务名称：", json.dumps(row["name"], ensure_ascii=False))
        print("配置 ID：", row["id"])
        print("Doris 连接 ID：", row["doris_connection_id"])
        print("配置更新时间：", row["updated_at"])
        try:
            password = recover_password(row["default_doris_password_enc"], key)
        except ValueError as exc:
            print("读取失败：", str(exc))
            failed = True
        else:
            print("当前配置候选密码（JSON 字符串）：", json.dumps(password, ensure_ascii=False))
        print("-" * 50)
    print('密码外层双引号不属于密码；特殊字符按 JSON 转义显示。脚本未修改任何账号或配置。')
    return 4 if failed else 0


def main():
    try:
        settings = get_settings()
        with get_sync_session_factory()() as session:
            rows = lookup(session, sys.argv[1])
        return display(rows, sys.argv[1], settings.credential_encryption_key)
    except Exception as exc:
        # Do not echo driver exceptions: they may contain connection credentials.
        print("查询失败（%s）。请确认 API 容器的系统库连接和表结构正常；未修改数据。" % type(exc).__name__, file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
PY
