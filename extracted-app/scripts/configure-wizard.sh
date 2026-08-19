#!/bin/bash
# 交互式收集 B 机(DMP+Docker) 与 C 机(目标库) 配置，并调用 API 分步检测
set -euo pipefail

API="${RECOVERY_API:-http://127.0.0.1:8000}"
API_KEY="${RECOVERY_API_KEY:-}"

echo "=== Oracle Recovery 配置向导 ==="
echo "恢复服务 API: $API"
if [ -z "$API_KEY" ]; then
  read -rsp "API Key (SECRET_KEY，回车跳过若开发模式): " API_KEY
  echo
fi

read -rp "DMP 服务器 IP (B机): " SSH_HOST
read -rp "SSH 端口 [22]: " SSH_PORT
SSH_PORT=${SSH_PORT:-22}
read -rp "SSH 用户名: " SSH_USER
read -rsp "SSH 密码: " SSH_PASS
echo
read -rp "宿主机 DMP 目录 (如 /data/dump): " DMP_HOST_PATH

read -rp "Oracle 容器名 (docker ps): " DOCKER_CONTAINER
read -rp "容器内 DMP 路径 (与 DIRECTORY 一致): " DMP_CONTAINER_PATH
read -rp "Oracle DIRECTORY 名 [DATA_PUMP_DIR]: " ORA_DIR
ORA_DIR=${ORA_DIR:-DATA_PUMP_DIR}
read -rp "容器内 ORACLE_HOME (可空): " ORA_HOME

read -rp "目标库连接串 (C机 host:1521/service): " T_CONN
read -rp "目标库用户: " T_USER
read -rsp "目标库密码: " T_PASS
echo

HDR=(-H "Content-Type: application/json")
[ -n "$API_KEY" ] && HDR+=(-H "X-API-Key: $API_KEY")

BODY=$(cat <<EOF
{
  "ssh_host": "$SSH_HOST",
  "ssh_port": $SSH_PORT,
  "ssh_user": "$SSH_USER",
  "ssh_password": "$SSH_PASS",
  "dmp_host_path": "$DMP_HOST_PATH",
  "target_connection": "$T_CONN",
  "target_admin_user": "$T_USER",
  "target_admin_password": "$T_PASS",
  "execution": {
    "mode": "remote_docker",
    "docker_container": "$DOCKER_CONTAINER",
    "dmp_host_path": "$DMP_HOST_PATH",
    "dmp_container_path": "$DMP_CONTAINER_PATH",
    "oracle_directory": "$ORA_DIR",
    "oracle_home_in_container": "$ORA_HOME"
  }
}
EOF
)

echo ""
echo ">>> 开始分步检测..."
curl -s "${HDR[@]}" -d "$BODY" "$API/api/v1/setup/check/all" | python3 -m json.tool 2>/dev/null || curl -s "${HDR[@]}" -d "$BODY" "$API/api/v1/setup/check/all"

echo ""
echo ">>> 任务提交 JSON 模板已保存到 task.submit.json"
echo "$BODY" > task.submit.json
echo "提交: curl -X POST $API/api/v1/tasks -H 'X-API-Key: ...' -d @task.submit.json"
