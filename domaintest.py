#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按「域名」做下载测速：候选直接填域名，结果也按域名出。

和老链路的分工：
    老链路 候选 -> resolve 展平成 IP -> cfst 测延迟筛一轮 -> 取前 N 个测下载 -> 输出 IP
    本脚本 候选 -> 每个域名各自解析出 IP -> 每个域名测若干个 IP -> 按域名聚合取最快

为什么要单独一条链路：
    老链路里每个域名解析出的 IP 都得先挤进「延迟排序前 N 名」才轮得到测速。
    bestcf 那种列表 78 个域名能解析出几百个 IP，绝大多数根本没被测到就被截断了，
    这时候比较「哪个域名快」是不准的。要按域名比，就得保证每个域名都被公平测一次。

用法：
  domaintest.py <src> <out_csv> <probe_ips> <parallel> <seconds> <port> <url> <speed_min> [ip_mode] [sni]

  probe_ips  每个域名最多测几个解析出来的 IP（默认 2）
  sni        ip     （默认）用 url 里的域名做 SNI，测的是「这个 IP 到 CF 的吞吐」
             domain 用候选域名本身做 SNI；url 里可以写 {domain} 占位，
                     例如 https://{domain}/speedtest.bin（域名下有大文件时才用这个）

输出 CSV：domain,ip,port,latency,speed,colo,remark
  一行一个域名，取该域名测得最快的那次；latency 是 TCP+TLS 握手耗时。

日志沿用 dltest.py 的 [dl] 前缀，Web 控制台不用为它单独写一套解析。
"""
import csv
import ipaddress
import os
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import resolve                      # noqa: E402  复用行解析 / 域名展开 / CF 段判断
from dltest import probe, norm_ip   # noqa: E402  复用 TLS+HTTP 的测速实现

CSV_HEADER = ["domain", "ip", "port", "latency", "speed", "colo", "remark"]


def build_line(row, suffix_mode, tag, default_port):
    """生成推送用的 `IP:端口#后缀` 一行

    为什么输出的是 IP 而不是域名：CFNext 面板的 ip 字段和后端订阅都要能吃下这一行，
    填域名有被拒的风险。域名放在 `#` 后面做名称，既不影响面板，又能一眼看出归属。
    想让面板里直接出现域名，把 SUFFIX_MODE 保持 source 即可（没有备注时就用域名当后缀）。
    """
    ip = row["ip"]
    try:
        port = int(row["port"] or default_port)
    except (TypeError, ValueError):
        port = default_port
    host = "[%s]" % ip if ":" in ip else ip

    colo = row["colo"] or ""
    if suffix_mode == "none":
        suffix = ""
    elif suffix_mode == "fixed":
        suffix = tag
    elif suffix_mode == "colo":
        suffix = colo if colo and colo != "N/A" else tag
    elif suffix_mode == "custom":
        tpl = (os.environ.get("SUFFIX_CUSTOM") or "{colo}").strip() or "{colo}"
        c = colo if colo and colo != "N/A" else tag
        suffix = (tpl.replace("{colo}", c)
                     .replace("{latency}", str(row["latency"] or ""))
                     .replace("{speed}", str(row["speed"] or "")))
    else:                                   # source：备注 > 域名 > 机房码 > tag
        suffix = row["remark"] or row["domain"] or (colo or tag)
    return "%s:%d%s" % (host, port, ("#" + suffix) if suffix else "")


def to_float(v, default=0.0):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


def load_targets(src, probe_ips, ip_mode, warnings):
    """源文件 -> [(domain, ip, port, remark), ...]，已按域名去重

    纯 IP / 网段行的处理：
      纯 IP 当成一个「域名就是它自己」的候选，照测；
      CIDR 网段在域名视角下没有意义，跳过并提示。
    """
    try:
        with open(src, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except FileNotFoundError:
        warnings.append("找不到源文件 %s" % src)
        return []

    targets, seen = [], set()
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.strip() or line.strip().startswith("#"):
            continue
        parsed = resolve._split_line(line)
        if not parsed:
            continue
        host, port, remark = parsed

        if "/" in host:                       # 网段
            warnings.append("域名模式跳过网段 %s" % host)
            continue

        # 纯 IP：domain 字段就用它自己
        try:
            ip = str(ipaddress.ip_address(host))
        except ValueError:
            ip = ""

        if ip:
            if not resolve._want_ip(ip, ip_mode):
                continue
            if resolve.CF_ONLY and not resolve.is_cf_ip(ip):
                warnings.append("非 Cloudflare IP 已跳过 %s (%s)" % (ip, remark or "-"))
                continue
            key = (ip, ip)
            if key not in seen:
                seen.add(key)
                targets.append((ip, ip, port, remark))
            continue

        # 域名：展开成 IP，取前 probe_ips 个
        if not resolve._DOMAIN_RE.match(host):
            warnings.append("无法识别 %s" % host)
            continue
        ips = resolve._domain_ips(host, warnings, ip_mode)
        if not ips:
            continue
        kept = 0
        for one in ips:
            if kept >= probe_ips:
                break
            if resolve.CF_ONLY and not resolve.is_cf_ip(one):
                warnings.append("非 Cloudflare IP 已跳过 %s (%s)" % (one, host))
                continue
            key = (host, one)
            if key in seen:
                continue
            seen.add(key)
            targets.append((host, one, port, remark))
            kept += 1
        if kept == 0:
            warnings.append("域名可用 IP 全部被过滤 %s" % host)
    return targets


def build_url(template, domain, sni_mode):
    """按 SNI 模式算出这次请求真正要访问的 URL

    probe() 会从 URL 里取 hostname 当 SNI / Host 头，所以这里等价于「选 SNI」。
    """
    if "{domain}" in template:
        return template.replace("{domain}", domain)
    if sni_mode != "domain":
        return template
    u = urllib.parse.urlsplit(template)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    return "https://%s%s" % (domain, path)


def main(argv):
    if len(argv) < 9:
        sys.stderr.write(__doc__ or "")
        return 2

    src, out_csv = argv[1], argv[2]
    probe_ips = max(1, int(float(argv[3] or 2)))
    parallel = max(1, min(32, int(float(argv[4] or 4))))
    seconds = max(1, int(float(argv[5] or 8)))
    port = int(float(argv[6] or 443))
    url = argv[7] or "https://speed.cloudflare.com/__down?bytes=50000000"
    speed_min = to_float(argv[8], 0.0)
    ip_mode = (argv[9] if len(argv) > 9 else "ipv4").strip().lower()
    if ip_mode not in ("ipv4", "ipv6", "both"):
        ip_mode = "ipv4"
    sni_mode = (argv[10] if len(argv) > 10 else "ip").strip().lower()
    if sni_mode not in ("ip", "domain"):
        sni_mode = "ip"

    warnings = []
    targets = load_targets(src, probe_ips, ip_mode, warnings)

    # 同一个域名可能解析出多个 IP，这里统计每个域名有几个任务，用来算「域名级」进度
    per_domain = {}
    for domain, _ip, _port, _remark in targets:
        per_domain[domain] = per_domain.get(domain, 0) + 1

    total_domains = len(per_domain)
    sys.stderr.write("[dl] 域名直测：%d 个候选域名，共 %d 个 IP，每个域名最多测 %d 个，"
                     "并发 %d，单个上限 %ds，SNI=%s\n"
                     % (total_domains, len(targets), probe_ips, parallel, seconds, sni_mode))

    # 先把计划报给上层：域名 -> 端口 -> 解析到的 IP。
    # Web 控制台据此把「待测清单」铺出来，用户开跑就能看到这次要测哪些域名。
    plan, plan_order = {}, []
    for d, i, p, _r in targets:
        if d not in plan:
            plan[d] = {"port": p, "ips": []}
            plan_order.append(d)
        plan[d]["ips"].append(i)
    for d in plan_order:
        try:
            pport = int(plan[d]["port"])
        except (TypeError, ValueError):
            pport = port
        sys.stderr.write("[dl] plan %s %d %s\n" % (d, pport, ",".join(plan[d]["ips"])))
    sys.stderr.flush()
    for w in warnings[:6]:
        sys.stderr.write("[dl] %s\n" % w)
    if len(warnings) > 6:
        sys.stderr.write("[dl] ...另有 %d 条提示\n" % (len(warnings) - 6))
    if not targets:
        sys.stderr.write("[dl] 没有可测的域名（列表为空，或全部被非 CF 段过滤）\n")
        return 3

    lock = threading.Lock()
    results = {}                 # domain -> 该域名当前最快的一条
    left = dict(per_domain)      # domain -> 还没测完的 IP 数
    done_domains = 0
    alive = 0

    def report(domain, mbps, mb):
        with lock:
            sys.stderr.write("[dl] now %s %.2f %.2f\n" % (domain, mbps, mb))
            sys.stderr.flush()

    def one(domain, ip, sport, remark):
        real_url = build_url(url, domain, sni_mode)
        mb, colo, tls_ms, _bytes = probe(
            ip, sport, real_url, seconds,
            (lambda _i, m, b: report(domain, m, b)),
            False, True)
        return domain, ip, sport, remark, mb, colo, tls_ms

    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futs = {ex.submit(one, d, i, p, r): (d, i) for d, i, p, r in targets}
        for fut in as_completed(futs):
            d, i = futs[fut]
            try:
                domain, ip, sport, remark, mb, colo, tls_ms = fut.result()
            except Exception:                       # noqa: BLE001
                domain, ip, sport, remark, mb, colo, tls_ms = d, i, 443, "", 0.0, "", 0.0
                with lock:
                    left[domain] = left.get(domain, 1) - 1
                continue

            with lock:
                row = {
                    "domain": domain,
                    "ip": ip,
                    "port": sport,
                    "latency": "%.0f" % tls_ms if tls_ms > 0 else "",
                    "speed": "%.2f" % mb,
                    "colo": colo,
                    "remark": remark,
                }
                cur = results.get(domain)
                if cur is None or to_float(row["speed"]) > to_float(cur["speed"]):
                    results[domain] = row
                if mb > 0:
                    alive += 1
                left[domain] = left.get(domain, 1) - 1
                finished = left[domain] <= 0
                if finished:
                    done_domains += 1
                    best = results[domain]
                    n = done_domains

            if finished:
                sys.stderr.write("[dl] 进度 %d/%d  %s  %.2f MB/s %s\n"
                                 % (n, total_domains, domain,
                                    to_float(best["speed"]), best["colo"] or "-"))
                sys.stderr.flush()

    rows = list(results.values())

    # 阈值过滤 + 剔除测不出速度的（0 MB/s 的域名不能当优选域名用）
    if speed_min > 0:
        kept = [r for r in rows if to_float(r["speed"]) >= speed_min]
        sys.stderr.write("[dl] 速度 >=%.2f MB/s 的有 %d / %d 个域名\n"
                         % (speed_min, len(kept), len(rows)))
        rows = kept
    usable = [r for r in rows if to_float(r["speed"]) > 0]
    if len(usable) != len(rows):
        sys.stderr.write("[dl] 剔除 %d 个测速失败的域名（0 MB/s）\n" % (len(rows) - len(usable)))
    rows = usable

    rows.sort(key=lambda r: (-to_float(r["speed"]), to_float(r["latency"], 9999)))

    # 保留条数（网页上的「保留条数」，域名个数，不是 IP 个数）
    try:
        topn = int(float(os.environ.get("TOP_N") or 0))
    except (TypeError, ValueError):
        topn = 0
    if topn > 0 and len(rows) > topn:
        sys.stderr.write("[dl] 按速度取前 %d 个域名（共 %d 个达标）\n" % (topn, len(rows)))
        rows = rows[:topn]

    tmp = out_csv + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(CSV_HEADER)
        for r in rows:
            wr.writerow([r["domain"], r["ip"], r["port"], r["latency"],
                         r["speed"], r["colo"], r["remark"]])
    os.replace(tmp, out_csv)

    # 推送用的文本：IP:端口#后缀，一行一条。后缀规则跟 format_result.py 保持一致，
    # 由 run.sh 通过环境变量传进来（SUFFIX_MODE / TAG）。
    suffix_mode = (os.environ.get("SUFFIX_MODE") or "source").strip().lower()
    tag = (os.environ.get("TAG") or "auto").strip()
    txt_path = os.path.splitext(out_csv)[0] + ".txt"
    lines = [build_line(r, suffix_mode, tag, port) for r in rows]
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))

    if rows:
        best = rows[0]
        sys.stderr.write("[dl] 完成：%d 个域名有速度，最快的 %s（%s）%.2f MB/s %s\n"
                         % (len(rows), best["domain"], best["ip"],
                            to_float(best["speed"]), best["colo"] or "-"))
    else:
        fastest = max((to_float(r["speed"]) for r in results.values()), default=0.0)
        if speed_min > 0:
            sys.stderr.write("[dl] 没有域名达标：最快的只有 %.2f MB/s，阈值 %.2f MB/s。"
                             "把速度下限调到 %.1f 以下再试\n"
                             % (fastest, speed_min, max(0.1, fastest * 0.8)))
        else:
            sys.stderr.write("[dl] 所有域名都没测出有效速度，检查测速地址是否可访问、"
                             "端口是否被阻断\n")
    return 0 if rows else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
