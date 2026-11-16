#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cf-optimizer Web 控制台
只用 Python3 标准库，无第三方依赖。

功能：
  - 填一个或多个 ip.txt 网络地址，或直接粘贴 IP 列表（会合并去重）
  - 调 cfst 测速，实时回传日志 + 进度条（还剩多少个没测）
  - 从 cfst 的 CSV 里取「地区码」列，生成 IP:端口#后缀 形式
  - 一键把结果写进 CFNext 面板（面板地址/密码在网页上填，首次使用会引导）
  - 配置持久化到 /data/web-config.json
"""
import datetime
import json
import os
import re
import selectors
import signal
import subprocess
import threading
import time
import urllib.request

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import resolve  # 同目录下的候选列表解析器（域名/IP:端口#备注 -> 纯 IP）

DATA_DIR = os.environ.get("DATA_DIR", "/data")
OUT_DIR = os.environ.get("OUT_DIR", "/out")
WEB_PORT = int(os.environ.get("WEB_PORT", "8090"))
HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
CONFIG_FILE = os.path.join(DATA_DIR, "web-config.json")
WORK_DIR = "/tmp/cfwork"

DEFAULTS = {
    "source_urls": [],        # 多个候选列表地址，合并去重
    "source_text": "",
    "port": 443,
    "latency": 350,
    "speed": 0,
    "topn": 10,
    "threads": 100,
    "ping_times": 4,
    "disable_dl": False,
    "dn": 10,          # 下载测速数量（从延迟最低的里面取几个测下载）
    "dt": 10,          # 单个 IP 下载测速时长上限（秒）
    "dl_parallel": 4,  # 并行下载测速数量：同时测几个 IP（cfst 自带是串行的）
    "speed_url": "https://speed.cloudflare.com/__down?bytes=50000000",
    "suffix_mode": "source",  # source | colo | fixed | none | custom
    "suffix_custom": "{colo}",
    "tag": "auto",
    "panel_url": "",
    "panel_password": "",
    "setup_done": False,
    # ---- 定时任务（网页上可改，改完即时生效，不用重启容器）----
    "cron_enabled": True,
    "cron_expr": "0 */2 * * *",   # 标准五段式：分 时 日 月 周
    "cron_push": True,            # 定时跑完是否自动推送到面板
}

# crond 的 spool 文件；crond 由 entrypoint 里的 shell 看守，杀掉会被立刻拉起
CRON_FILE = "/etc/crontabs/root"
CRON_PID_FILE = "/var/run/crond.pid"
CRON_RUN_CMD = "/run.sh >> /proc/1/fd/1 2>&1"

# 不回传给浏览器的字段（只回「设过没」这类派生信息）
SECRET_FIELDS = ("panel_password",)

lock = threading.Lock()
state = {
    "status": "idle",        # idle | running | done | error
    "log": "",
    "result": [],
    "error": "",
    "started": 0.0,
    "finished": 0.0,
    "params": {},
    "progress": {"phase": "", "done": 0, "total": 0, "alive": 0, "percent": 0.0},
    # 延迟测速通过的 IP 清单，网页上按「等测 / 测速中 / 已测完」实时更新
    "dl_rows": [],
}

LOG_MAX = 20000

# 进度条片段长这样（写在 stderr、片段之间用空格拼、一个换行都没有）：
#   87 / 142 [----↘_____] 可用: 87
# 末尾必须跟着 "["，这样被打断的半截数字会被丢掉，不会让进度条乱跳。
PROG_PAIR = re.compile(r"(\d+)\s*/\s*(\d+)\s*\[")
PROG_ALIVE = re.compile(r"可用:\s*(\d+)")
# dltest.py 的进度行：[dl] 进度 3/10  1.2.3.4  5.20 MB/s LAX
DL_PROG = re.compile(r"\[dl\] 进度 (\d+)/(\d+)")
# dltest.py 测速过程中的实时上报：[dl] now 1.2.3.4 5.20 18.40
DL_NOW = re.compile(r"\[dl\] now (\S+) ([\d.]+) ([\d.]+)")


def load_config():
    cfg = dict(DEFAULTS)
    cfg["source_urls"] = []
    saved = {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            saved = {}
    except Exception:
        saved = {}

    for k in DEFAULTS:
        if k in saved:
            cfg[k] = saved[k]

    # 兼容旧版：只存了一个 source_url
    if not cfg.get("source_urls") and saved.get("source_url"):
        cfg["source_urls"] = [saved["source_url"]]
    if not isinstance(cfg.get("source_urls"), list):
        cfg["source_urls"] = []

    # compose 里的环境变量仍作兜底（网页上填过就以网页为准）
    if not (cfg.get("panel_url") or "").strip():
        cfg["panel_url"] = os.environ.get("PANEL_URL", "").strip()
    if not cfg.get("panel_password"):
        cfg["panel_password"] = os.environ.get("PANEL_PASSWORD", "")

    # 面板信息齐了就算配置完成，老用户不用再走一遍引导
    if (cfg.get("panel_url") or "").strip() and cfg.get("panel_password"):
        cfg["setup_done"] = True

    # 定时任务：网页上还没存过时，用 compose 里的 CRON 兜底（老用户升级后不丢原来的设置）
    if "cron_expr" not in saved:
        cfg["cron_expr"] = (os.environ.get("CRON") or "").strip() or DEFAULTS["cron_expr"]
    if "cron_enabled" not in saved:
        cfg["cron_enabled"] = os.environ.get("CRON_ENABLED", "1") not in ("0", "false", "False", "")
    if "cron_push" not in saved:
        cfg["cron_push"] = (os.environ.get("PUSH_MODE", "panel") or "").strip() == "panel"
    return cfg


def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    clean = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(CONFIG_FILE, 0o600)   # 里面有面板密码，只给自己读
    except OSError:
        pass


def _cron_field(spec, lo, hi):
    """解析单个 cron 字段：* / 5 / 1-5 / */2 / 1,3,5 / 1-20/3"""
    vals = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            raise ValueError("有空的字段")
        step = 1
        if "/" in part:
            part, sv = part.split("/", 1)
            try:
                step = int(sv)
            except ValueError:
                raise ValueError("步长 %s 不是数字" % sv)
            if step <= 0:
                raise ValueError("步长必须大于 0")
        if part in ("*", "?"):
            a, b = lo, hi
        elif "-" in part.lstrip("-"):
            x, y = part.split("-", 1)
            try:
                a, b = int(x), int(y)
            except ValueError:
                raise ValueError("范围 %s 不是数字" % part)
        else:
            try:
                a = int(part)
            except ValueError:
                raise ValueError("%s 不是数字" % part)
            b = hi if step > 1 else a
        if a < lo or b > hi or a > b:
            raise ValueError("%s 超出 %d-%d" % (part, lo, hi))
        for v in range(a, b + 1, step):
            vals.add(v)
    if not vals:
        raise ValueError("字段解析为空")
    return vals


def parse_cron(expr):
    """返回 (分, 时, 日, 月, 周, 日是否受限, 周是否受限)"""
    parts = (expr or "").split()
    if len(parts) != 5:
        raise ValueError("要 5 段：分 时 日 月 周，例如 0 */2 * * *")
    mins = _cron_field(parts[0], 0, 59)
    hours = _cron_field(parts[1], 0, 23)
    doms = _cron_field(parts[2], 1, 31)
    mons = _cron_field(parts[3], 1, 12)
    dows = _cron_field(parts[4], 0, 7)
    if 7 in dows:            # 7 和 0 都是周日
        dows.discard(7)
        dows.add(0)
    dom_res = parts[2].strip() not in ("*", "?")
    dow_res = parts[4].strip() not in ("*", "?")
    return mins, hours, doms, mons, dows, dom_res, dow_res


def cron_ok(expr):
    try:
        parse_cron(expr)
        return True
    except Exception:  # noqa: BLE001
        return False


def cron_next(expr, base=None):
    """算出下一次触发时间；最多往后找一年，找不到就返回 None"""
    try:
        mins, hours, doms, mons, dows, dom_res, dow_res = parse_cron(expr)
    except Exception:  # noqa: BLE001
        return None
    t = (base or datetime.datetime.now()).replace(second=0, microsecond=0)
    t += datetime.timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        if t.minute in mins and t.hour in hours and t.month in mons:
            dow = t.isoweekday() % 7        # 周一=1 ... 周日=0
            # 标准 cron 语义：日和周同时受限时是「或」，只有其中一个受限才按「与」
            if dom_res and dow_res:
                day_ok = (t.day in doms) or (dow in dows)
            elif dom_res:
                day_ok = t.day in doms
            elif dow_res:
                day_ok = dow in dows
            else:
                day_ok = True
            if day_ok:
                return t
        t += datetime.timedelta(minutes=1)
    return None


def cron_human(dt):
    if not dt:
        return ""
    days = (dt.date() - datetime.date.today()).days
    hm = dt.strftime("%H:%M")
    if days == 0:
        return "今天 " + hm
    if days == 1:
        return "明天 " + hm
    if days == 2:
        return "后天 " + hm
    return dt.strftime("%m月%d日 ") + hm


def reload_crond():
    """让 crond 重新读 crontab。

    crond 由 entrypoint 里的 shell 看守（while 循环 + wait），
    所以只要把它杀掉，看守会在一秒内拉起新的进程并读到新规则。
    """
    try:
        with open(CRON_PID_FILE, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def apply_crontab(cfg):
    """把定时设置写进 crontab 并立刻生效。返回 (ok, msg)"""
    enabled = bool(cfg.get("cron_enabled", True))
    expr = (cfg.get("cron_expr") or "").strip()

    if enabled:
        if not cron_ok(expr):
            try:
                parse_cron(expr)
            except ValueError as e:
                return False, "cron 表达式有误：%s" % e
            return False, "cron 表达式有误"
        body = "%s %s\n" % (expr, CRON_RUN_CMD)
    else:
        body = "# 定时任务已在网页上关闭，手动测速不受影响\n"

    try:
        os.makedirs(os.path.dirname(CRON_FILE), exist_ok=True)
        with open(CRON_FILE, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(CRON_FILE, 0o600)
    except OSError as e:
        return False, "写 crontab 失败：%s" % e

    reload_crond()
    if enabled:
        nxt = cron_next(expr)
        return True, "定时已生效：%s，下次运行 %s" % (expr, cron_human(nxt) or "（算不出来）")
    return True, "定时任务已关闭"


def public_config(cfg):
    """给浏览器的配置：密码不下发，只告诉前端设过没有"""
    pub = {k: v for k, v in cfg.items() if k not in SECRET_FIELDS}
    pub["panel_password_set"] = bool(cfg.get("panel_password"))
    # 定时任务的派生信息：下次什么时候跑、表达式对不对
    expr = (cfg.get("cron_expr") or "").strip()
    pub["cron_valid"] = cron_ok(expr)
    if cfg.get("cron_enabled") and pub["cron_valid"]:
        nxt = cron_next(expr)
        pub["cron_next"] = nxt.strftime("%Y-%m-%d %H:%M") if nxt else ""
        pub["cron_next_human"] = cron_human(nxt)
    else:
        pub["cron_next"] = ""
        pub["cron_next_human"] = ""
    try:
        with open(CRON_FILE, "r", encoding="utf-8", errors="replace") as f:
            pub["cron_active"] = f.read().strip()
    except OSError:
        pub["cron_active"] = ""
    return pub


def apply_patch(cfg, body):
    """把前端传来的字段合并进配置；密码留空表示不修改"""
    for k, v in body.items():
        if k not in DEFAULTS:
            continue
        if k == "panel_password":
            if isinstance(v, str) and v.strip():
                cfg[k] = v.strip()
            continue
        if k == "source_urls":
            if isinstance(v, str):
                v = [v]
            if isinstance(v, list):
                cfg[k] = [str(x).strip() for x in v if str(x).strip()]
            continue
        cfg[k] = v
    return cfg


def append_log(text):
    with lock:
        state["log"] = (state["log"] + text + "\n")[-LOG_MAX:]


def set_progress(phase, done, total, alive, **extra):
    pct = round(done * 100.0 / total, 1) if total else 0.0
    with lock:
        p = {
            "phase": phase, "done": done, "total": total,
            "alive": alive, "percent": pct,
        }
        p.update(extra)
        state["progress"] = p


def dl_set_rows(rows):
    """重置「下载测速清单」——延迟测速跑完后把通过的 IP 全列出来"""
    with lock:
        state["dl_rows"] = rows


def dl_mark(ip, status=None, **fields):
    """更新清单里某个 IP 的状态；status: wait / running / done"""
    with lock:
        for r in state["dl_rows"]:
            if r["ip"] == ip:
                if status:
                    r["status"] = status
                r.update(fields)
                return


def clean_ip_lines(raw):
    """去掉注释行和空行（只做粗清洗，真正解析交给 resolve）"""
    out = []
    for ln in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return "\n".join(out) + ("\n" if out else "")


def fetch_sources(params):
    """拉取所有候选列表（可多个）并合并；一个失败不影响其它"""
    urls = params.get("source_urls") or []
    if isinstance(urls, str):
        urls = [urls]
    urls = [str(u).strip() for u in urls if str(u).strip()]

    chunks = []
    ok = 0
    for u in urls:
        append_log("[web] 拉取候选列表: %s" % u)
        try:
            req = urllib.request.Request(u, headers={
                "User-Agent": "Mozilla/5.0 (cf-optimizer)",
                "Accept": "text/plain,*/*",
            })
            with urllib.request.urlopen(req, timeout=40) as resp:
                raw = resp.read().decode("utf-8", "replace")
            n = len([x for x in raw.split("\n") if x.strip()])
            chunks.append(raw)
            ok += 1
            append_log("[web]   ✓ %d 行" % n)
        except Exception as e:  # noqa: BLE001
            append_log("[web]   ✗ 失败：%s" % e)

    if urls and ok == 0:
        raise RuntimeError("填的 %d 个候选地址全部拉取失败，检查一下网络和地址" % len(urls))

    pasted = params.get("source_text") or ""
    if pasted.strip():
        chunks.append(pasted)
        append_log("[web] 加上手动粘贴的 %d 行"
                   % len([x for x in pasted.split("\n") if x.strip()]))

    if not chunks:
        raise RuntimeError("候选列表为空：请填写候选列表的网络地址，或粘贴 IP 列表")
    return clean_ip_lines("\n".join(chunks))


def build_suffix(params, colo, latency, speed, remark=""):
    mode = params.get("suffix_mode") or "source"
    tag = params.get("tag") or "auto"
    if mode == "none":
        return ""
    if mode == "fixed":
        return tag
    if mode == "source" and remark:
        return remark
    if mode == "custom":
        tpl = params.get("suffix_custom") or "{colo}"
        c = colo if colo and colo != "N/A" else tag
        return (tpl.replace("{colo}", c)
                   .replace("{latency}", str(latency))
                   .replace("{speed}", str(speed)))
    return colo if colo and colo != "N/A" else tag


def read_latency_rows(path, default_port=443, meta=None):
    """延迟测速的 CSV -> 网页上「待测下载」的清单。

    这里不做速度过滤、也不按 topn 截断：用户要看到所有通过延迟测速的 IP。
    """
    rows = []
    meta = meta or {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return rows

    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < 7:
            continue
        ip = parts[0].strip()
        if not ip:
            continue
        src_port, _remark = meta.get(ip, (default_port, ""))
        colo = parts[6].strip()
        rows.append({
            "ip": ip,
            "port": src_port or default_port,
            "loss": parts[3].strip(),
            "latency": parts[4].strip(),
            "speed": "",
            "colo": "" if colo.upper() == "N/A" else colo,
            "status": "wait",
        })
    return rows


def parse_csv(path, params, meta=None):
    """cfst CSV: IP地址,已发送,已接收,丢包率,平均延迟,下载速度(MB/s),地区码"""
    rows = []
    meta = meta or {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return rows

    default_port = int(params.get("port") or 443)
    try:
        topn = max(1, int(params.get("topn") or 10))
    except (TypeError, ValueError):
        topn = 10

    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < 7:
            continue
        ip = parts[0].strip()
        if not ip:
            continue
        loss = parts[3].strip()
        latency = parts[4].strip()
        speed = parts[5].strip()
        colo = parts[6].strip()
        # 端口和备注优先取源列表里那一行自带的（如 :8443#EDT导航）
        src_port, remark = meta.get(ip, (default_port, ""))
        port = src_port or default_port
        suffix = build_suffix(params, colo, latency, speed, remark)
        host = "[%s]" % ip if ":" in ip else ip
        line = "%s:%d" % (host, port) + (("#" + suffix) if suffix else "")
        rows.append({
            "ip": ip,
            "port": port,
            "loss": loss,
            "latency": latency,
            "speed": speed,
            "colo": colo,
            "suffix": suffix,
            "line": line,
        })

    # 最终结果统一按「下载速度降序」排（dltest 是多线程并行完成，CSV 里的顺序
    # 就是完成顺序，不排的话最快的 IP 可能排在最后）。
    def _spd(r):
        try:
            return float(r["speed"] or 0)
        except (TypeError, ValueError):
            return 0.0

    rows.sort(key=_spd, reverse=True)

    # 测出 0 速度的 IP 不能用，直接不展示。只有整批都没测速（跳过下载测速 /
    # 只探数据中心）时才保留，否则会把结果全清空。
    if any(_spd(r) > 0 for r in rows):
        rows = [r for r in rows if _spd(r) > 0]

    # 注意 cfst 的 -p 只管控制台显示，CSV 里是全量结果，必须在这里截断。
    return rows[:topn]


def run_cfst(cmd):
    """跑 cfst 并实时解析进度。

    坑：进度条不是按行输出的，而是写在 stderr、片段之间用空格拼接
    （一个 \\r / \\n 都没有）。所以必须分开读两个管道，从 stderr 里用正则捞
    `N / M [`。只读 stdout 的话永远拿不到任何进度。
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0)
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, "out")
    sel.register(proc.stderr, selectors.EVENT_READ, "err")

    out_buf = ""
    err_tail = ""
    lat_total = 0
    seen = {"done": 0, "total": 0, "alive": 0, "phase": "latency"}
    deadline = None

    def on_progress(txt):
        nonlocal err_tail, lat_total
        err_tail = (err_tail + txt)[-800:]
        pairs = PROG_PAIR.findall(err_tail)
        if not pairs:
            return
        n, m = int(pairs[-1][0]), int(pairs[-1][1])
        if m <= 0:
            return
        # 第一次见到的总数就是本轮 IP 总数；之后总数变小说明进了下载测速阶段
        if lat_total == 0 or m >= lat_total:
            lat_total = m if m > lat_total else lat_total
            phase = "latency"
        else:
            phase = "download"
        alive = seen["alive"]
        am = PROG_ALIVE.findall(err_tail)
        if am:
            try:
                alive = int(am[-1])
            except ValueError:
                pass
        seen.update({"done": n, "total": m, "alive": alive, "phase": phase})
        set_progress(phase, n, m, alive)

    while True:
        if deadline is None and proc.poll() is not None:
            deadline = time.time() + 3      # 进程结束，给管道 3 秒把残留读干净
        if deadline is not None:
            left = deadline - time.time()
            if left <= 0:
                break
            timeout = min(0.2, left)
        else:
            timeout = 0.2

        events = sel.select(timeout)
        if not events:
            continue
        for key, _ in events:
            try:
                raw = os.read(key.fd, 8192)
            except OSError:
                raw = b""
            if not raw:
                try:
                    sel.unregister(key.fileobj)
                except (KeyError, ValueError):
                    pass
                continue
            txt = raw.decode("utf-8", "replace")
            if key.data == "err":
                on_progress(txt)
            else:
                out_buf += txt
                while "\n" in out_buf:
                    ln, out_buf = out_buf.split("\n", 1)
                    s = ln.strip()
                    if not s or "___" in s or "----" in s:
                        continue
                    append_log(s)

    if out_buf.strip():
        append_log(out_buf.strip())
    sel.close()
    return proc.wait()


def run_dltest(in_csv, out_csv, params, mode="speed"):
    """并行下载测速（mode=speed）或只探测数据中心（mode=colo）。

    cfst 自带的下载测速是串行的（-dn 个 IP 一个接一个），dltest.py 用线程池同时测，
    并发数由 dl_parallel 控制。速度阈值也在这里过滤。
    返回 dltest.py 的退出码：0 有结果 / 3 全被过滤 / 其它是异常。
    """
    dn = max(1, int(params.get("dn") or 10))
    par = max(1, min(32, int(params.get("dl_parallel") or 4)))
    dt = max(1, int(params.get("dt") or 10))
    port = int(params.get("port") or 443)
    url = (params.get("speed_url") or "").strip()
    sl = float(params.get("speed") or 0)

    cmd = ["python3", os.path.join(HERE, "dltest.py"), in_csv, out_csv,
           str(dn), str(par), str(dt), str(port), url, str(sl)]
    phase = "download"
    if mode == "colo":
        cmd.append("colo")
        phase = "colo"
        append_log("[web] 数据中心探测：跳过下载测速，只读响应头补 cf-ray 机房码")
    else:
        append_log("[web] 并行下载测速：取前 %d 个 IP，并发 %d，单个上限 %ds，阈值 %.2f MB/s"
                   % (dn, par, dt, sl))
    set_progress(phase, 0, dn, 0)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    alive = 0
    for line in proc.stderr:
        line = line.strip()
        if not line:
            continue

        # 测速中途的实时速度：[dl] now <ip> <瞬时MB/s> <已下载MB>
        m = DL_NOW.search(line)
        if m:
            ip, mbps = m.group(1), m.group(2)
            try:
                if float(mbps) > 0:
                    pass
            except ValueError:
                pass
            dl_mark(ip, "running", speed=mbps)
            with lock:
                cur = dict(state["progress"])
            set_progress(phase, cur.get("done", 0), cur.get("total", dn), alive,
                         current_ip=ip, current_speed=mbps,
                         current_mb=m.group(3))
            continue

        # 单个 IP 测完：[dl] 进度 3/10  1.2.3.4  5.20 MB/s LAX
        m = DL_PROG.search(line)
        if m:
            parts = line.split()
            done, total = int(m.group(1)), int(m.group(2))
            ip = parts[3] if len(parts) > 3 else ""
            spd = parts[4] if len(parts) > 4 and parts[4] != "-" else ""
            colo = parts[-1] if len(parts) > 5 and parts[-1] != "-" else ""
            try:
                if spd and float(spd) > 0:
                    alive += 1
            except ValueError:
                pass
            if mode == "colo":
                dl_mark(ip, "done", colo=colo)
            else:
                dl_mark(ip, "done", speed=spd, colo=colo)
            set_progress(phase, done, total, alive,
                         current_ip="", current_speed="", current_mb="")
            continue

        append_log(line)
    rc = proc.wait()
    set_progress(phase, 1, 1, alive, current_ip="", current_speed="", current_mb="")
    return rc


def run_scan(params):
    """跑一轮测速。在独立线程里执行，结果写进全局 state。"""
    with lock:
        state["status"] = "running"
        state["log"] = ""
        state["result"] = []
        state["error"] = ""
        state["started"] = time.time()
        state["finished"] = 0.0
        state["params"] = params
        state["progress"] = {"phase": "resolve", "done": 0, "total": 0,
                             "alive": 0, "percent": 0.0}
        state["dl_rows"] = []

    try:
        os.makedirs(WORK_DIR, exist_ok=True)
        os.makedirs(OUT_DIR, exist_ok=True)

        ip_text = fetch_sources(params)
        if not ip_text.strip():
            raise RuntimeError("候选列表为空：请填写候选列表的网络地址，或粘贴 IP 列表")

        # cfst 只认纯 IP / CIDR，域名和 IP:端口#备注 都会让它直接报错退出，
        # 所以统一先解析成 IP 列表，端口/备注单独存一份映射。
        clean_text, meta_text, stats, warns = resolve.resolve_text(ip_text)
        if not clean_text.strip():
            raise RuntimeError(
                "候选列表里解析不出可测的 IP（输入 %d 行：跳过 %d 行、"
                "非 Cloudflare 剔除 %d 个）。检查一下地址格式或换一个列表。"
                % (stats["in"], stats["fail"], stats["noncf"]))

        ip_file = os.path.join(WORK_DIR, "ip.txt")
        with open(ip_file, "w", encoding="utf-8") as f:
            f.write(clean_text)
        meta_path = os.path.join(WORK_DIR, "meta.tsv")
        with open(meta_path, "w", encoding="utf-8") as f:
            f.write(meta_text)
        meta = resolve.load_meta(meta_path)

        append_log("[web] 解析候选：输入 %d 行 -> 可测 IP %d 个"
                   "（域名展开 %d 条、已去重、非 CF 剔除 %d 个、跳过 %d 行）"
                   % (stats["in"], stats["out"], stats["domain"],
                      stats["noncf"], stats["fail"]))
        for w in warns[:5]:
            append_log("[web] " + w)
        if len(warns) > 5:
            append_log("[web] ...另有 %d 条解析提示" % (len(warns) - 5))

        csv_path = os.path.join(WORK_DIR, "result.csv")
        if os.path.exists(csv_path):
            os.remove(csv_path)

        # cfst 只做延迟测速（-dd），下载速度交给 dltest.py 并行测：
        # cfst 自带的下载测速是串行的，dn 个 IP × dt 秒要串着等；dltest 能同时测多个。
        cmd = [
            "cfst", "-f", ip_file,
            "-tp", str(int(params.get("port") or 443)),
            "-t", str(int(params.get("ping_times") or 4)),
            "-n", str(int(params.get("threads") or 100)),
            "-tl", str(int(params.get("latency") or 350)),
            "-sl", "0",                      # 速度筛选由 dltest 负责
            "-p", str(int(params.get("topn") or 10)),
            "-o", csv_path,
            "-dd",
        ]
        if params.get("speed_url"):
            cmd += ["-url", params["speed_url"]]

        append_log("[web] 开始延迟测速：延迟<=%sms 端口%s"
                   % (params.get("latency"), params.get("port")))

        rc = run_cfst(cmd)

        # 延迟测速跑完，先把通过的 IP 全部列出来：网页上要能看到「有哪些 IP、延迟多少，
        # 然后逐个测下载」。这一步不做速度过滤，也不按 topn 截断。
        lat_rows = read_latency_rows(csv_path, int(params.get("port") or 443), meta)
        dl_set_rows([dict(r) for r in lat_rows])
        if lat_rows:
            append_log("[web] 延迟测速通过 %d 个 IP，接下来逐个测下载速度" % len(lat_rows))

        if not params.get("disable_dl"):
            dl_csv = os.path.join(WORK_DIR, "speed.csv")
            rc2 = run_dltest(csv_path, dl_csv, params)
            if rc2 == 3:
                raise RuntimeError(
                    "下载测速没有 IP 达到速度下限 %.2f MB/s，本轮不产出结果。"
                    "上面日志里有「最快只有多少 MB/s」，按提示调低阈值或把单次测速时长"
                    "加到 8~10 秒再试。" % float(params.get("speed") or 0))
            if rc2 != 0:
                raise RuntimeError("下载测速异常退出（退出码 %s），看上面日志" % rc2)
            if os.path.exists(dl_csv):
                csv_path = dl_csv
        else:
            # 跳过下载测速时，仍要补数据中心码，否则「数据中心」后缀会退化成固定文本 auto
            colo_csv = os.path.join(WORK_DIR, "colo.csv")
            try:
                run_dltest(csv_path, colo_csv, params, mode="colo")
                if os.path.exists(colo_csv) and os.path.getsize(colo_csv) > 0:
                    csv_path = colo_csv
            except Exception as e:  # noqa: BLE001
                append_log("[web] 数据中心探测失败（不影响结果）：%s" % e)
        set_progress("parse", 1, 1, 0)

        rows = parse_csv(csv_path, params, meta)
        if not rows:
            raise RuntimeError(
                "本轮没有满足条件的结果（cfst 退出码 %s）。常见原因：延迟阈值 %sms 太严、"
                "速度下限 %s MB/s 超过实际带宽、候选 IP 不可用、或出网受阻。"
                "可以勾上「跳过下载测速」再跑一次，用来判断卡在哪个环节。"
                % (rc, params.get("latency"), params.get("speed")))

        with lock:
            state["result"] = rows
            state["status"] = "done"
            state["finished"] = time.time()
            state["progress"] = {"phase": "done", "done": 1, "total": 1,
                                 "alive": 0, "percent": 100.0}
        append_log("[web] 完成，共 %d 条" % len(rows))

    except Exception as e:
        with lock:
            state["status"] = "error"
            state["error"] = str(e)
            state["finished"] = time.time()
            state["progress"] = {"phase": "error", "done": 0, "total": 0,
                                 "alive": 0, "percent": 0.0}
        append_log("[web] 失败: %s" % e)


def push_result():
    with lock:
        rows = list(state["result"])
    if not rows:
        return False, "还没有结果可推送"

    cfg = load_config()
    panel_url = (cfg.get("panel_url") or "").strip()
    panel_pwd = cfg.get("panel_password") or ""
    if not panel_url or not panel_pwd:
        return False, ("还没配置 CFNext 面板。请在左侧「CFNext 面板」里填入口地址"
                       "和管理密码，保存后再推送。")

    os.makedirs(OUT_DIR, exist_ok=True)
    result_file = os.path.join(OUT_DIR, "result.txt")
    with open(result_file, "w", encoding="utf-8") as f:
        f.write("\n".join(r["line"] for r in rows) + "\n")

    env = dict(os.environ)
    env["PANEL_URL"] = panel_url
    env["PANEL_PASSWORD"] = panel_pwd

    proc = subprocess.run(["sh", "/push_panel.sh", result_file],
                          capture_output=True, text=True, timeout=240, env=env)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out[-4000:]


def push_state():
    with lock:
        return {
            "status": state["status"],
            "log": state["log"],
            "result": state["result"],
            "error": state["error"],
            "started": state["started"],
            "finished": state["finished"],
            "progress": state["progress"],
            "dl_rows": state["dl_rows"],
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "cf-optimizer/1.1"

    def log_message(self, fmt, *args):
        pass  # 静音访问日志，避免刷屏

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            fp = os.path.join(STATIC_DIR, "index.html")
            try:
                with open(fp, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "index.html 缺失", "text/plain; charset=utf-8")
            return

        if path == "/api/state":
            self._json({"ok": True, "data": push_state()})
            return

        if path == "/api/config":
            self._json({"ok": True, "data": public_config(load_config())})
            return

        if path == "/api/health":
            self._json({"ok": True})
            return

        self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/config":
            body = self._read_json()
            cfg = apply_patch(load_config(), body)
            msg = ""
            # 动了定时相关字段就顺手重写 crontab，改完立即生效（不用重启容器）
            if "cron_expr" in body or "cron_enabled" in body:
                ok, msg = apply_crontab(cfg)
                if not ok:
                    self._json({"ok": False, "msg": msg,
                                "data": public_config(cfg)}, 400)
                    return
            save_config(cfg)
            self._json({"ok": True, "msg": msg, "data": public_config(cfg)})
            return

        if path == "/api/run":
            with lock:
                if state["status"] == "running":
                    self._json({"ok": False, "msg": "已有任务在跑，请等它结束"}, 409)
                    return
            body = self._read_json()
            cfg = apply_patch(load_config(), body)
            cfg["setup_done"] = True
            save_config(cfg)

            t = threading.Thread(target=run_scan, args=(cfg,), daemon=True)
            t.start()
            self._json({"ok": True, "msg": "已开始"})
            return

        if path == "/api/push":
            try:
                ok, out = push_result()
            except Exception as e:
                self._json({"ok": False, "msg": str(e)}, 500)
                return
            self._json({"ok": ok, "msg": out})
            return

        self._send(404, "not found", "text/plain; charset=utf-8")


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), Handler)
    print("[web] 监听 0.0.0.0:%d" % WEB_PORT, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
