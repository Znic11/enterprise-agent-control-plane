以下是为你整理的 **EnterpriseOps-Gym 全流程全指令操作手册**，包含**全领域容器创建、各领域独立启动、状态检查、任务评测、全面清理与一键自动化切换脚本**。

---

# EnterpriseOps-Gym (udocker 全流程指令手册)

---

## 一、 一次性初始化准备

在项目根目录下执行以下指令，完成数据库解压、配置目录准备和 **7 个领域容器的批量创建**：

```bash
cd <PROJECT_ROOT>

# 1. 解压评测初始数据库
unzip -q gym_dbs.zip

# 2. 准备配置目录及日志目录
cp -r conf.example/ conf/
mkdir -p logs

# 3. 批量创建 7 个领域的 udocker 容器
MIRROR="docker.1panel.live"
DOMAINS=("teams" "csm" "email" "itsm" "calendar" "hr" "drive")

for domain in "${DOMAINS[@]}"; do
    echo "正在创建容器: gym_${domain} ..."
    udocker create --name="gym_${domain}" "${MIRROR}/shivakrishnareddyma225/enterpriseops-gym-mcp-${domain}:latest"
done

# 验证容器是否全部就绪
udocker ps
```

---

## 二、 各领域 MCP 服务的启动指令

> **提示**：除了 `calendar` 默认监听 **`8003`** 端口外，其余 6 个领域默认监听 **`8005`** 端口。每次评测不同领域前，请确保上一领域的后台进程已被终止。

### 1. Teams 领域
```bash
# 启动
nohup udocker run gym_teams > logs/teams.log 2>&1 &
# 检查
lsof -i :8005
```

### 2. CSM (客服系统) 领域
```bash
# 启动
nohup udocker run gym_csm > logs/csm.log 2>&1 &
# 检查
lsof -i :8005
```

### 3. Email (邮件系统) 领域
```bash
# 启动
nohup udocker run gym_email > logs/email.log 2>&1 &
# 检查
lsof -i :8005
```

### 4. ITSM (IT服务管理) 领域
```bash
# 启动
nohup udocker run gym_itsm > logs/itsm.log 2>&1 &
# 检查
lsof -i :8005
```

### 5. Calendar (日历系统) 领域（注意：端口为 8003）
```bash
# 启动
nohup udocker run gym_calendar > logs/calendar.log 2>&1 &
# 检查
lsof -i :8003
```

### 6. HR (人力资源系统) 领域
```bash
# 启动
nohup udocker run gym_hr > logs/hr.log 2>&1 &
# 检查
lsof -i :8005
```

### 7. Drive (云盘/文档系统) 领域
```bash
# 启动
nohup udocker run gym_drive > logs/drive.log 2>&1 &
# 检查
lsof -i :8005
```

---

## 三、 运行评测任务与监控

### 1. 快速更新配置文件
在运行某个领域评测前，需要同步修改配置文件：

- **修改 `conf/ray/domain_conf.json`**：将当前测试领域的端口设置为 `8005`（如果是 `calendar` 则设置为 `8003`）。
- **修改 `conf/ray/experiment.json`**：将 `"domains"` 字段设为当前领域，例如 `["teams"]`。

> 也可以使用 Python 单行指令快速修改（以 teams 为例）：
> ```bash
> python3 -c '
> import json
> # 改 domain_conf.json
> with open("conf/ray/domain_conf.json", "r+") as f:
>     d = json.load(f); d["teams"] = {"host": "localhost", "port": 8005}
>     f.seek(0); json.dump(d, f, indent=2); f.truncate()
> # 改 experiment.json
> with open("conf/ray/experiment.json", "r+") as f:
>     d = json.load(f); d["domains"] = ["teams"]
>     f.seek(0); json.dump(d, f, indent=2); f.truncate()
> '
> ```

### 2. 启动评测
```bash
uv run python ray_experiment_queue.py
```

---

## 四、 清理与关闭指令（必须执行）

每次跑完一个领域、准备切换领域，或者彻底结束任务时，执行对应的清理操作：

### 1. 释放 MCP 服务端口（核心）
```bash
# 释放 8005 端口（适用于 teams, csm, email, itsm, hr, drive）
fuser -k 8005/tcp || kill -9 $(lsof -t -i:8005) 2>/dev/null

# 释放 8003 端口（适用于 calendar）
fuser -k 8003/tcp || kill -9 $(lsof -t -i:8003) 2>/dev/null
```

### 2. 彻底清理残留后台子进程（可选）
如果端口被未知残留进程锁定，可以强行杀死属于当前用户的后台容器进程：
```bash
pkill -u $USER -f "proot"
pkill -u $USER -f "enterpriseops-gym-mcp"
```

### 3. 清理 Ray 缓存与守护进程
如果评测卡死或中断退出，建议重置 Ray 集群状态：
```bash
uv run ray stop --force 2>/dev/null || true
rm -rf /tmp/ray/*
```

---

## 五、 进阶：一键切换与运行脚本（省时利器）

为了避免每次手动输入指令和修改 json，可以在项目根目录下创建一个管理脚本 `manage.sh`：

```bash
cat << 'EOF' > manage.sh
#!/bin/bash
DOMAIN=$1
ACTION=$2

if [ -z "$DOMAIN" ]; then
    echo "用法: ./manage.sh <domain_name> [start|stop|test]"
    echo "可选 domain: teams | csm | email | itsm | calendar | hr | drive"
    exit 1
fi

PORT=8005
if [ "$DOMAIN" == "calendar" ]; then
    PORT=8003
fi

stop_service() {
    echo ">>> 正在停止端口 $PORT 上的服务..."
    fuser -k ${PORT}/tcp >/dev/null 2>&1 || kill -9 $(lsof -t -i:${PORT}) 2>/dev/null
    sleep 1
}

start_service() {
    stop_service
    echo ">>> 正在启动 gym_${DOMAIN} (端口: $PORT)..."
    nohup udocker run gym_${DOMAIN} > logs/${DOMAIN}.log 2>&1 &
    sleep 3
    if lsof -i :${PORT} >/dev/null 2>&1; then
        echo ">>> [成功] gym_${DOMAIN} 已在端口 $PORT 启动！"
    else
        echo ">>> [失败] 启动异常，请查看 logs/${DOMAIN}.log"
        exit 1
    fi
}

update_config() {
    echo ">>> 正在自动配置 conf/ray/... 适配 $DOMAIN (端口 $PORT)..."
    python3 -c "
import json
with open('conf/ray/domain_conf.json', 'r+') as f:
    d = json.load(f)
    d['$DOMAIN'] = {'host': 'localhost', 'port': $PORT}
    f.seek(0); json.dump(d, f, indent=2); f.truncate()
with open('conf/ray/experiment.json', 'r+') as f:
    d = json.load(f)
    d['domains'] = ['$DOMAIN']
    f.seek(0); json.dump(d, f, indent=2); f.truncate()
"
}

case "$ACTION" in
    start)
        start_service
        update_config
        ;;
    stop)
        stop_service
        ;;
    test)
        start_service
        update_config
        echo ">>> 正在启动评测..."
        uv run python ray_experiment_queue.py
        stop_service
        ;;
    *)
        start_service
        update_config
        ;;
esac
EOF

chmod +x manage.sh
```

### 脚本使用示例：
```bash
# 一键启动 teams 服务并自动同步配置文件
./manage.sh teams start

# 停止当前领域服务
./manage.sh teams stop

# 一键自动化跑完整流程（启动服务 -> 自动配置 -> 执行评测 -> 结束后自动清理）
./manage.sh teams test
./manage.sh csm test
./manage.sh calendar test
```


## 🚀 Running the Benchmark

### Option A — Ray *(recommended)*

Ray orchestrates parallel runs across models and domains.

**1. Create an experiment config** (`conf/ray/experiment.json`):

```json
{
    "llms": ["gpt-4.1-mini", "gemini_2p5"],
    "domains": ["teams", "csm", "email"],
    "modes": ["oracle", "plus_5_tools", "plus_10_tools", "plus_15_tools"],
    "orchestrator": "react",
    "num_runs": 1,
    "num_llm_instances": 1,
    "path_templates": {
        "log_dir": "logs/{orchestrator}/{llm}/{domain}/{mode}",
        "output_folder": "results/{orchestrator}/{llm}/{domain}/{mode}",
        "llm_config": "conf/llm/{llm}.json"
    }
}
```

Per-model task concurrency is set in `conf/ray/llm_concurrency.json` (defaults to 5):

```json
{ "gpt-4.1-mini": 4, "gemini_2p5": 4 }
```

**2. Run:**

```bash
python ray_experiment_queue.py --experiment_config conf/ray/experiment.json
```

---

### Option B — Direct

Run a single domain/mode without Ray. **Use this option for the `hybrid` domain.**

```bash
python evaluate.py \
    --hf_dataset ServiceNow-AI/EnterpriseOps-Gym \
    --domain teams --mode oracle \
    --llm_config conf/llm/deepseek-v4-flash.json \
    --output_folder results/react/deepseek-v4-flash/teams/oracle \
    --orchestrator react \
    --concurrency 4 --num_runs 1
```

For hybrid tasks:

```bash
python evaluate.py \
    --hf_dataset ServiceNow-AI/EnterpriseOps-Gym \
    --domain hybrid --mode oracle \
    --llm_config conf/llm/deepseek-v4-flash.json \
    --output_folder results/react/deepseek-v4-flash/hybrid/oracle \
    --orchestrator react \
    --concurrency 2 --num_runs 1
```

```bash
python evaluate.py \
    --hf_dataset ServiceNow-AI/EnterpriseOps-Gym \
    --domain csm --mode oracle \
    --llm_config conf/llm/deepseek-v4-flash.json \
    --output_folder results/react/deepseek-v4-flash/csm/oracle \
    --orchestrator react \
    --concurrency 4 --num_runs 1
```

## 计算得分
### 单个 mode
```bash
python compute_score.py --results_folder results/react/deepseek-v4-flash/teams/oracle
```

### 一次汇总多个 mode（目录按 mode 分文件夹）
```bash
python compute_score.py --results_folder results/react/deepseek-v4-flash/teams
```