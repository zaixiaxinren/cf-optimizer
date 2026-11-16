#!/bin/sh
# 容器入口：启动 Web 控制台 + crond
# 定时规则以网页上保存的为准（/data/web-config.json），网页没存过才用环境变量 CRON 兜底。
# crond 外面套一个 while 看守：网页上改了定时，server.py 会把 crond 杀掉，
# 这里立刻重新拉起并读到新的 crontab（busybox crond 的热重载不可靠）。
set -u

RUN_ON_START="${RUN_ON_START:-0}"
WEB_PORT="${WEB_PORT:-8090}"

mkdir -p /data /out /var/log /var/run /etc/crontabs

# 首次启动时如果没 ip.txt，放一份空模板，避免用户找不到位置
if [ ! -f /data/ip.txt ]; then
  cat > /data/ip.txt <<'EOF'
# 每行一条候选：IP 或 CIDR 网段，支持 # 注释
# 例：
# 104.16.0.0/24
# 172.64.0.0/24
# 162.159.132.53
EOF
  echo "[init] 已生成模板 /data/ip.txt，请填入你的候选 IP"
fi

echo "[init] 时区 TZ=${TZ:-未设置}  当前时间 $(date '+%F %T')"

# ---------- 1. 决定用哪个 cron 规则 ----------
# 优先级：网页设置 > 环境变量 CRON > 默认每 2 小时
CRON_EXPR="${CRON:-0 */2 * * *}"
CRON_ENABLED=1
if [ -f /data/web-config.json ] && command -v python3 >/dev/null 2>&1; then
  eval "$(python3 - <<'PY'
import json

try:
    cfg = json.load(open("/data/web-config.json", encoding="utf-8"))
    if not isinstance(cfg, dict):
        cfg = {}
except Exception:
    cfg = {}

print("CRON_ENABLED=%d" % (1 if cfg.get("cron_enabled", True) else 0))
expr = (cfg.get("cron_expr") or "").strip()
if expr:
    # 只留 cron 会用到的那几个字符，避免把引号之类的带进 shell
    safe = "".join(ch for ch in expr if ch in "0123456789*/,- ")
    if safe.strip():
        print("CRON_EXPR='%s'" % safe.strip())
PY
)"
fi

if [ "$CRON_ENABLED" = "1" ] && [ -n "$CRON_EXPR" ]; then
  echo "$CRON_EXPR /run.sh >> /proc/1/fd/1 2>&1" > /etc/crontabs/root
  echo "[init] 定时规则: $CRON_EXPR"
else
  echo "# 定时任务已在网页上关闭" > /etc/crontabs/root
  echo "[init] 定时任务: 已关闭（手动测速不受影响）"
fi
chmod 600 /etc/crontabs/root

# ---------- 2. Web 控制台 ----------
if command -v python3 >/dev/null 2>&1; then
  python3 /server.py &
  echo "[init] Web 控制台已启动，端口 ${WEB_PORT}"
else
  echo "[init] 警告：镜像里没有 python3，Web 控制台不可用（cron 模式仍正常）"
fi

if [ "$RUN_ON_START" = "1" ]; then
  echo "[init] 启动即跑一轮"
  /run.sh || echo "[init] 首轮未产出结果，等待定时任务"
fi

# ---------- 3. crond（带看守，便于网页改完即时生效）----------
start_crond() {
  crond -f -l 8 &
  CROND_PID=$!
  echo "$CROND_PID" > /var/run/crond.pid
  echo "[init] crond 已启动 (pid $CROND_PID)"
}

start_crond
trap '[ -n "${CROND_PID:-}" ] && kill -TERM "$CROND_PID" 2>/dev/null; exit 0' TERM INT

while :; do
  wait "$CROND_PID" 2>/dev/null
  # 走到这里说明 crond 退出了：通常是网页上改了定时，也可能是异常退出。
  # 等一下再拉起，避免出问题时疯狂重启。
  sleep 1
  start_crond
done
