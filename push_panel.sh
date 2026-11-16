#!/bin/sh
# 把测速结果写进 CFNext 面板的「优选 IP」列表
#
# 适配 CFNext v3.0.0（实测，混淆版）。关键点：
#   1. /<入口> 只是登录入口；登录后内部 API 前缀是另一条 uuid 路径，
#      它写在控制台页面的 `var APIPATH = "..."` 里，必须动态提取。
#   2. 没有 /api/bootstrap（那是旧版），v3 用 GET/POST /api/config。
#   3. GET /api/config 返回 {"ok":true,"data":{...配置...}}，data 就是配置本体。
#   4. POST /api/config 是「增量合并」：只传 preferredIPs 不会动其他设置。
#   5. GET 请求若不带 Accept/JSON 相关头，容易被当成页面请求返回 HTML 兜底页。
#
# 用法: push_panel.sh <result.txt>
set -u

PANEL_URL="${PANEL_URL:?PANEL_URL 未设置}"
PANEL_PASSWORD="${PANEL_PASSWORD:?PANEL_PASSWORD 未设置}"
RESULT_FILE="${1:-/out/result.txt}"

PANEL_URL=$(printf '%s' "$PANEL_URL" | sed 's#/*$##')
ORIGIN=$(printf '%s' "$PANEL_URL" | sed -E 's#^(https?://[^/]+).*#\1#')

UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

[ -s "$RESULT_FILE" ] || { echo "[panel] 结果文件为空: $RESULT_FILE"; exit 1; }

COOKIE=$(mktemp) || exit 1
PAGE=$(mktemp); CFG=$(mktemp); BODY=$(mktemp); IPS=$(mktemp)
trap 'rm -f "$COOKIE" "$PAGE" "$CFG" "$BODY" "$IPS" "$IPS.a"' EXIT INT TERM

# ---------- 1. 登录 ----------
LOGIN=$(curl -sS -m 40 -A "$UA" -c "$COOKIE" -X POST "$PANEL_URL" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "password=$PANEL_PASSWORD" \
  --data-urlencode "next=$PANEL_URL" 2>&1)

if ! printf '%s' "$LOGIN" | jq -e '.ok == true' >/dev/null 2>&1; then
  echo "[panel] 登录失败，面板返回: $(printf '%s' "$LOGIN" | head -c 300)"
  exit 1
fi
CK=$(grep -o '[[:space:]]luma_auth[[:space:]].*' "$COOKIE" | awk '{print $2}')
if [ -z "$CK" ]; then
  echo "[panel] 登录成功但没拿到会话 cookie"
  exit 1
fi
echo "[panel] 登录成功"

# ---------- 2. 从控制台页提取真实 API 前缀 ----------
curl -sS -m 40 -A "$UA" -H "Cookie: luma_auth=$CK" "$PANEL_URL" -o "$PAGE" 2>/dev/null
APIPATH=$(grep -oE 'var APIPATH = "[^"]*"' "$PAGE" | head -1 | sed 's/.*"\(.*\)"/\1/')
if [ -z "$APIPATH" ]; then
  echo "[panel] 没能从控制台页提取 APIPATH（页面可能不是控制台）"
  exit 1
fi
API_BASE="$ORIGIN$APIPATH"
echo "[panel] API 前缀 $APIPATH"

# ---------- 3. 读当前配置 ----------
curl -sS -m 40 -A "$UA" -H "Cookie: luma_auth=$CK" "$API_BASE/api/config" -o "$CFG" 2>/dev/null
if ! jq -e '.ok == true and (.data != null)' "$CFG" >/dev/null 2>&1; then
  echo "[panel] 读取配置失败: $(head -c 200 "$CFG")"
  exit 1
fi
OLD_N=$(jq '.data.preferredIPs | if type=="array" then length else 0 end' "$CFG" 2>/dev/null || echo 0)
echo "[panel] 当前优选 IP 有 ${OLD_N} 条，本次将替换"

# ---------- 4. result.txt -> preferredIPs 数组 ----------
# 输入形如: 1.2.3.4:443#LAX   /   [2606:4700::1]:443#HKG
awk '
  function jesc(s){ gsub(/\\/,"\\\\",s); gsub(/"/,"\\\"",s); return s }
  {
    line=$0
    sub(/\r$/,"",line)
    if (line=="" || line ~ /^#/) next
    name=""
    p=index(line,"#")
    if (p>0) { name=substr(line,p+1); line=substr(line,1,p-1) }
    port=""
    if (match(line, /:[0-9]+$/)) {
      port=substr(line, RSTART+1, RLENGTH-1)
      line=substr(line, 1, RSTART-1)
    }
    host=line
    gsub(/^\[/,"",host); gsub(/\]$/,"",host)
    if (host=="" || host ~ /[^0-9a-fA-F.:]/) next
    if (port=="") port=443
    if (n++) printf ","
    printf "{\"ip\":\"%s\",\"port\":%d,\"name\":\"%s\"}", host, port, jesc(name)
  }
' "$RESULT_FILE" > "$IPS"

if [ ! -s "$IPS" ]; then
  echo "[panel] 无法从 $RESULT_FILE 解析出任何 IP，放弃推送"
  exit 1
fi

printf '[%s]' "$(cat "$IPS")" > "$IPS.a"
NEW_N=$(jq 'length' "$IPS.a" 2>/dev/null || echo "")
if [ -z "$NEW_N" ]; then
  echo "[panel] 生成的 JSON 不合法，放弃推送"
  exit 1
fi
echo "[panel] 本次解析出 ${NEW_N} 条"

# ---------- 5. 写回（只传 preferredIPs，增量合并） ----------
# PANEL_FORCE_CUSTOM=1 时顺带确保订阅模式是 custom（只用优选 IP）。
# optimizer 在后端是整体替换，所以要取原值再改，不能只传两个字段。
PANEL_FORCE_CUSTOM="${PANEL_FORCE_CUSTOM:-1}"
if [ "$PANEL_FORCE_CUSTOM" = "1" ]; then
  jq -n --slurpfile a "$IPS.a" --slurpfile b "$CFG" \
    '{preferredIPs: $a[0],
      optimizer: (($b[0].data.optimizer // {}) + {subMode:"custom", subIncludeDefault:false})}' \
    > "$BODY" 2>/dev/null || { echo "[panel] 组装请求体失败"; exit 1; }
  echo "[panel] 同时确保订阅模式为 custom（只用优选 IP）"
else
  jq -n --slurpfile a "$IPS.a" '{preferredIPs: $a[0]}' > "$BODY" 2>/dev/null \
    || { echo "[panel] 组装请求体失败"; exit 1; }
fi

RESP=$(curl -sS -m 60 -A "$UA" -H "Cookie: luma_auth=$CK" \
  -X POST "$API_BASE/api/config" \
  -H 'Content-Type: application/json' \
  --data-binary @"$BODY" 2>&1)

if ! printf '%s' "$RESP" | jq -e '.ok == true' >/dev/null 2>&1; then
  echo "[panel] 写入失败: $(printf '%s' "$RESP" | head -c 300)"
  exit 1
fi
echo "[panel] 写入成功，回读校验中…"

# ---------- 6. 回读 ----------
sleep 2
curl -sS -m 40 -A "$UA" -H "Cookie: luma_auth=$CK" "$API_BASE/api/config" -o "$CFG" 2>/dev/null
NOW_N=$(jq '.data.preferredIPs | if type=="array" then length else -1 end' "$CFG" 2>/dev/null || echo -1)

if [ "$NOW_N" = "$NEW_N" ]; then
  echo "[panel] 回读通过：面板现有 ${NOW_N} 条"
  jq -r '.data.preferredIPs[] | "  \(.ip):\(.port)#\(.name)"' "$CFG" 2>/dev/null | head -15
  echo "[panel] 订阅模式 $(jq -r '.data.optimizer.subMode // "(未设置)"' "$CFG")，含默认节点 $(jq -r 'if .data.optimizer.subIncludeDefault then "是" else "否" end' "$CFG")"
  exit 0
else
  echo "[panel] 回读数量不一致（提交 ${NEW_N}，实际 ${NOW_N}）"
  exit 1
fi
