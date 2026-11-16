#!/bin/sh
# 一轮完整任务：读 TXT -> CloudflareST 测速 -> 按阈值筛选 -> 写结果 -> 推送
set -u

IP_FILE="${IP_FILE:-/data/ip.txt}"
IP_MODE="${IP_MODE:-ipv4}"   # ipv4 / ipv6 / both（v6 需要容器有 IPv6 出口）
OUT_DIR="${OUT_DIR:-/out}"
LATENCY_MAX="${LATENCY_MAX:-200}"
SPEED_MIN="${SPEED_MIN:-5}"
TOP_N="${TOP_N:-10}"
PORT="${PORT:-443}"
TAG="${TAG:-auto}"
SUFFIX_MODE="${SUFFIX_MODE:-source}"   # source=用源列表@备注 / colo=数据中心码 / fixed=固定TAG / none=无后缀
DL_COUNT="${DL_COUNT:-10}"
DL_TIME="${DL_TIME:-10}"
DL_PARALLEL="${DL_PARALLEL:-4}"
THREADS="${THREADS:-100}"
PING_TIMES="${PING_TIMES:-4}"
KEEP_HISTORY="${KEEP_HISTORY:-20}"
SPEED_URL="${SPEED_URL:-}"
DISABLE_DL="${DISABLE_DL:-0}"
WEBHOOK_URL="${WEBHOOK_URL:-}"
WEBHOOK_TOKEN="${WEBHOOK_TOKEN:-}"

log() { echo "[$(date '+%F %T')] $*"; }

# ---------- 0. 跟随网页设置 ----------
# 网页上改过的参数（候选地址、面板地址/密码、阈值、后缀…）优先级最高，
# 免得「网页上改了、定时任务还用旧值」。容器环境变量只在网页没填时兜底。
CFG_FILE="${CFG_FILE:-/data/web-config.json}"
if [ -f "$CFG_FILE" ]; then
  # python3 把 json 转成一串 shell 赋值再 eval，避免手写 jq 转义出错
  eval "$(python3 - "$CFG_FILE" <<'PY'
import json, shlex, sys

try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
    if not isinstance(cfg, dict):
        cfg = {}
except Exception:
    cfg = {}

def s(v):
    if v is None or v is False:
        return ""
    if v is True:
        return "1"
    return str(v).strip()

pairs = [
    ("IP_MODE", s(cfg.get("ip_mode"))),
    ("PANEL_URL", s(cfg.get("panel_url"))),
    ("PANEL_PASSWORD", s(cfg.get("panel_password"))),
    ("SUFFIX_MODE", s(cfg.get("suffix_mode"))),
    ("TAG", s(cfg.get("tag"))),
    ("LATENCY_MAX", s(cfg.get("latency"))),
    ("SPEED_MIN", s(cfg.get("speed"))),
    ("TOP_N", s(cfg.get("topn"))),
    ("THREADS", s(cfg.get("threads"))),
    ("PING_TIMES", s(cfg.get("ping_times"))),
    ("PORT", s(cfg.get("port"))),
    ("SPEED_URL", s(cfg.get("speed_url"))),
    ("DL_COUNT", s(cfg.get("dn"))),
    ("DL_TIME", s(cfg.get("dt"))),
    ("DL_PARALLEL", s(cfg.get("dl_parallel"))),
]
for k, v in pairs:
    if v != "":
        print("%s=%s" % (k, shlex.quote(v)))

# 复选框：只有显式 true 才算开，否则按关处理（不能用空值，会退回环境变量）
print("DISABLE_DL=%s" % ("1" if cfg.get("disable_dl") is True else "0"))

# 定时跑完是否自动推送（网页上的开关）。没存过就沿用 compose 里的 PUSH_MODE
if "cron_push" in cfg:
    print("PUSH_MODE=%s" % ("panel" if cfg.get("cron_push") else "none"))

urls = cfg.get("source_urls") or []
if isinstance(urls, str):
    urls = [urls]
urls = [str(u).strip() for u in urls if str(u).strip()]
if urls:
    print("SOURCE_URLS=%s" % shlex.quote("\n".join(urls)))
PY
)"
fi

# ---------- 1. 候选来源 ----------
# 多个地址依次拉取、追加到一个文件里，重复 IP 后面由 resolve.py 去重
URLS="${SOURCE_URLS:-${SOURCE_URL:-}}"
if [ -n "$URLS" ]; then
  : > /tmp/ip.download
  GOT=0
  while IFS= read -r u; do
    [ -z "$u" ] && continue
    log "拉取候选列表 $u"
    if curl -fsSL -m 60 -A "cf-optimizer" "$u" >> /tmp/ip.download 2>/dev/null; then
      printf '\n' >> /tmp/ip.download
      GOT=$((GOT + 1))
    else
      log "  拉取失败，跳过这个地址"
    fi
  done <<EOF
$URLS
EOF
  if [ "$GOT" -gt 0 ] && [ -s /tmp/ip.download ]; then
    mv /tmp/ip.download "$IP_FILE"
    log "候选列表已更新：来自 ${GOT} 个地址，$(grep -cvE '^[[:space:]]*(#|$)' "$IP_FILE" 2>/dev/null || echo 0) 行有效条目"
  else
    rm -f /tmp/ip.download
    log "所有候选地址都拉取失败，继续用本地已有的 $IP_FILE"
  fi
fi

[ -n "${PANEL_URL:-}" ] && export PANEL_URL
[ -n "${PANEL_PASSWORD:-}" ] && export PANEL_PASSWORD

# ---------- 2. 输入检查 ----------
if [ ! -f "$IP_FILE" ]; then
  log "错误：找不到 $IP_FILE"
  log "请在 WebUI 里填候选列表地址，或把候选 IP / CIDR 每行一条放进 data/ip.txt"
  exit 1
fi

CNT=$(grep -vE '^[[:space:]]*(#|$)' "$IP_FILE" 2>/dev/null | wc -l | tr -d ' ')
if [ "$CNT" -eq 0 ]; then
  log "错误：$IP_FILE 里没有有效条目（空行和 # 注释不算）"
  exit 1
fi
log "输入 $CNT 条候选，文件 $IP_FILE"

# ---------- 2. 测速 ----------
mkdir -p "$OUT_DIR" /tmp/cfst
cd /tmp/cfst || exit 1
rm -f result.csv

# CloudflareST 只认 IP / CIDR：遇到 `域名`、`IP:端口`、`#备注` 中任何一样
# 都会 ParseCIDR err 直接退出（结果 0 条）。所以先用 resolve.py 统一清洗：
#   域名 -> DNS 解析成 IP；IP:端口#备注 -> 拆开（端口/备注存进 meta 供后面用）；
#   顺手剔掉不属于 Cloudflare 段的外站 IP。
CLEAN=/tmp/cfst/ip.clean
META=/tmp/cfst/meta.tsv
if ! python3 /resolve.py "$IP_FILE" "$CLEAN" "$META" "$IP_MODE"; then
  log "错误：候选列表解析后没有可测的 IP"
  exit 1
fi

# 用 set -- 组装参数，避免 URL 里的 ? & = 被 word splitting 拆坏
# cfst 只做延迟测速（-dd）：它自带的下载测速是串行的，速度交给 dltest.py 并行测
set -- -f "$CLEAN" -tl "$LATENCY_MAX" -sl 0 -p "$TOP_N" \
       -tp "$PORT" -t "$PING_TIMES" -n "$THREADS" -o /tmp/cfst/result.csv -dd

if [ -n "$SPEED_URL" ]; then
  set -- "$@" -url "$SPEED_URL"
fi

log "开始延迟测速：延迟<=${LATENCY_MAX}ms 端口${PORT} IP版本=${IP_MODE}"
cfst "$@" > /tmp/cfst/run.log 2>&1
CFST_RC=$?
tail -45 /tmp/cfst/run.log

if [ ! -s /tmp/cfst/result.csv ]; then
  log "本轮没有满足条件的结果（CloudflareST 退出码 $CFST_RC）"
  log "可能原因：阈值太严、候选 IP 不可用、或出网受阻"
  # 保留上一轮结果不动，避免把 CFNext 上的列表清空
  exit 2
fi

# ---------- 2.1 并行下载测速 ----------
if [ "$DISABLE_DL" = "1" ]; then
  # 跳过下载测速也必须有数据中心码，否则 colo 后缀会退化成固定文本（默认 auto）
  log "已跳过下载测速，只探测数据中心（读 cf-ray 响应头，不下载正文）"
  if python3 /dltest.py /tmp/cfst/result.csv /tmp/cfst/colo.csv \
       "$DL_COUNT" "$DL_PARALLEL" 6 "$PORT" "$SPEED_URL" 0 colo; then
    mv /tmp/cfst/colo.csv /tmp/cfst/result.csv
  else
    log "数据中心探测失败，按原样输出（后缀可能退化成固定文本）"
  fi
else
  log "并行下载测速：取前 ${DL_COUNT} 个 IP，并发 ${DL_PARALLEL}，单个上限 ${DL_TIME}s，阈值 ${SPEED_MIN} MB/s"
  if python3 /dltest.py /tmp/cfst/result.csv /tmp/cfst/speed.csv \
       "$DL_COUNT" "$DL_PARALLEL" "$DL_TIME" "$PORT" "$SPEED_URL" "$SPEED_MIN"; then
    mv /tmp/cfst/speed.csv /tmp/cfst/result.csv
  else
    log "下载测速没有可用结果（阈值过高或全部测速失败），放弃本轮"
    exit 2
  fi
fi

# ---------- 3. 解析与格式化 ----------
# cfst 的 CSV 列：IP地址,已发送,已接收,丢包率,平均延迟,下载速度(MB/s),地区码
# SUFFIX_MODE: source=用源列表里的 #备注（没有则回退地区码）/ colo=地区码 / fixed=TAG / none=无
# cfst 输出本身已按下载速度降序（未测速时按延迟升序），这里直接取前 N 条。
TMP="$OUT_DIR/.result.$$"
if ! python3 /format_result.py /tmp/cfst/result.csv "$META" "$TMP" \
       "$SUFFIX_MODE" "$TAG" "$PORT" "$TOP_N"; then
  log "解析后结果为空，放弃本轮"
  rm -f "$TMP"
  exit 3
fi

COUNT=$(wc -l < "$TMP" | tr -d ' ')
if [ "$COUNT" -eq 0 ]; then
  log "解析后结果为空，放弃本轮"
  rm -f "$TMP"
  exit 3
fi

mv "$TMP" "$OUT_DIR/result.txt"
cp "$OUT_DIR/result.txt" "$OUT_DIR/result-$(date '+%Y%m%d-%H%M').txt"
log "已写出 $COUNT 条到 $OUT_DIR/result.txt"

# 只留最近 N 份历史
ls -1t "$OUT_DIR"/result-*.txt 2>/dev/null | tail -n +"$((KEEP_HISTORY + 1))" | while IFS= read -r f; do
  rm -f "$f"
done

echo "---- 本轮结果 ----"
cat "$OUT_DIR/result.txt"
echo "------------------"

# ---------- 4. 推送 ----------
# PUSH_MODE: panel = 直写 CFNext 面板 / webhook = POST 到 Worker / none = 只留本地
PUSH_MODE="${PUSH_MODE:-none}"

case "$PUSH_MODE" in
  panel)
    if [ -z "${PANEL_URL:-}" ] || [ -z "${PANEL_PASSWORD:-}" ]; then
      log "PUSH_MODE=panel 但缺少 PANEL_URL 或 PANEL_PASSWORD，跳过推送"
    else
      log "推送 -> CFNext 面板 $PANEL_URL"
      if sh /push_panel.sh "$OUT_DIR/result.txt"; then
        log "面板写入成功"
      else
        log "面板写入失败（结果已留在本地，下轮重试）"
      fi
    fi
    ;;
  webhook)
    if [ -z "${WEBHOOK_URL:-}" ]; then
      log "PUSH_MODE=webhook 但 WEBHOOK_URL 为空，跳过推送"
    else
      log "推送 -> $WEBHOOK_URL"
      if curl -fsS -m 40 -X POST "$WEBHOOK_URL" \
           -H "Content-Type: text/plain; charset=utf-8" \
           -H "x-token: ${WEBHOOK_TOKEN:-}" \
           --data-binary @"$OUT_DIR/result.txt" -o /dev/null; then
        log "推送成功"
      else
        log "推送失败（结果已留在本地，下轮会重试）"
      fi
    fi
    ;;
  *)
    log "PUSH_MODE=$PUSH_MODE，只写本地文件"
    ;;
esac

exit 0
