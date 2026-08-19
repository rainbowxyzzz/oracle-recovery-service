# 安装与启动排错

## 1. `未知的名称或服务` / `Could not find setuptools>=75.0`

**原因：** 服务器访问不了 PyPI（DNS 或无外网），不是代码错误。

### 办法 A：国内镜像（推荐）

```bash
cd /path/to/oracle-recovery-service
source venv/bin/activate   # 若还没有: python3.10 -m venv venv && source venv/bin/activate

export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
export PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

pip install -U pip setuptools wheel -i "$PIP_INDEX_URL" --trusted-host "$PIP_TRUSTED_HOST"
pip install -r requirements.txt -i "$PIP_INDEX_URL" --trusted-host "$PIP_TRUSTED_HOST"

export PYTHONPATH=$(pwd)/src
# 可选: pip install -e . --no-build-isolation -i "$PIP_INDEX_URL" --trusted-host "$PIP_TRUSTED_HOST"

./start-python.sh
```

或直接：

```bash
./install-python.sh
```

（脚本已默认走清华镜像。）

### 办法 B：修 DNS

```bash
# 临时
echo "nameserver 223.5.5.5" | sudo tee /etc/resolv.conf
echo "nameserver 114.114.114.114" | sudo tee -a /etc/resolv.conf

ping -c2 pypi.tuna.tsinghua.edu.cn
```

### 办法 C：完全离线

在有网络的机器上：

```bash
pip download -r requirements.txt -d ./wheels -i https://pypi.tuna.tsinghua.edu.cn/simple
```

把 `wheels/` 目录拷到服务器后：

```bash
pip install --no-index --find-links=./wheels -r requirements.txt
export PYTHONPATH=/path/to/oracle-recovery-service/src
```

---

## 2. 使用 Conda `(base)` 环境

建议为本项目单独建 venv，避免和 Conda 冲突：

```bash
conda deactivate
python3.10 -m venv venv
source venv/bin/activate
./install-python.sh
```

---

## 3. MySQL / Redis 连接失败

```bash
curl http://127.0.0.1:8000/api/v1/health
# 看 mysql / redis 字段

# 检查 .env
grep MYSQL_ .env
grep REDIS_ .env
```

---

## 4. 启动后无响应

```bash
tail -f logs/api.log logs/worker.log
ss -lntp | grep 8000
```
---

## Oracle SQL*Plus 报 `SP2-0667` / `SP2-0750`

现象：

```text
Error 6 initializing SQL*Plus
SP2-0667: Message file sp1<lang>.msb not found
SP2-0750: You may need to set ORACLE_HOME to your Oracle software directory
```

原因：

- 容器里能找到 `sqlplus`，但 `ORACLE_HOME` 未设置或指向了错误目录。
- `sqlplus/mesg` 目录不可见，SQL*Plus 找不到消息文件。
- 手工进入容器执行 `export ORACLE_HOME=...` 只对当前交互 shell 生效，自动导入任务下一次 `docker exec` 不会继承该设置。

处理：

```bash
cd /path/to/oracle-recovery-service
./scripts/ensure-oracle-home-env.sh oracle21c
```

如需写入当前 Oracle 容器的 `/etc/profile.d`，使后续登录 shell 自动带上环境变量：

```bash
./scripts/ensure-oracle-home-env.sh --write-profile oracle21c
```

打包要求：

- 部署包必须包含 `scripts/ensure-oracle-home-env.sh`。
- `oracle_dmp_auto_import.py` 的 `docker_exec()` 必须保留自动探测 Oracle Home 的前缀逻辑。
- 导入前置检查若出现该错误，应优先检查容器内实际 Oracle Home，而不是误判为 DMP 文件或 PDB 本身异常。
