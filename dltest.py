#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并行下载测速：给 cfst -dd 生成的 CSV 补上「下载速度」列。

为什么要自己写：cfst 自带的下载测速是**串行**的（-dn 个 IP 一个接一个测），
10 个 IP × 10 秒就是 100 秒。这里用线程池同时测多个 IP，--parallel 控制并发数。

顺带解决另一个问题：cfst 在 -dd（只测延迟）模式下「地区码」列恒为 N/A，
而这里会把响应的 cf-ray 头解析出来（形如 8f3abc-LAX），把数据中心补上。

用法：
  dltest.py <in.csv> <out.csv> <dn> <parallel> <seconds> <port> <url> <speed_min>

只用 Python3 标准库。
"""
import csv
import os
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

MB = 1024.0 * 1024.0
UA = "Mozilla/5.0 (cf-optimizer)"


def parse_colo(head):
    """从响应头里的 cf-ray 取数据中心，如 `8f3abc123456-LAX` -> LAX"""
    for line in head.split(b"\r\n"):
        if line[:7].lower() == b"cf-ray:":
            try:
                v = line.split(b":", 1)[1].strip().decode("latin-1")
            except (IndexError, UnicodeDecodeError):
                return ""
            if "-" in v:
                return v.rsplit("-", 1)[1].strip().upper()
    return ""


def probe(ip, port, url, seconds):
    """对单个 IP 做一次限时下载，返回 (MB/s, 数据中心码)。

    关键点：要测的是「这个 IP」，但 HTTP 请求的 Host / TLS 的 SNI 必须是测速站点的域名，
    否则 Cloudflare 会返回 400。所以手工建连接：socket 连到指定 IP，
    SSL 用 server_hostname=域名 握手（相当于 curl --resolve）。
    """
    u = urllib.parse.urlsplit(url)
    host = u.hostname or "speed.cloudflare.com"
    path = u.path or "/"
    if u.query:
        path += "?" + u.query

    ctx = ssl.create_default_context()
    ctx.check_hostname = False          # 我们是按 IP 连的，只测速不校验身份
    ctx.verify_mode = ssl.CERT_NONE

    total = 0
    colo = ""
    t0 = time.time()
    sock = None
    ssock = None
    try:
        sock = socket.create_connection((ip, port), timeout=8)
        ssock = ctx.wrap_socket(sock, server_hostname=host)
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: %s\r\n"
               "Accept: */*\r\nAccept-Encoding: identity\r\n"
               "Connection: close\r\n\r\n" % (path, host, UA))
        ssock.sendall(req.encode())

        deadline = t0 + seconds
        head_buf = b""
        body_started = False
        first_byte = 0.0

        while True:
            left = deadline - time.time()
            if left <= 0:
                break
            ssock.settimeout(min(3.0, max(0.3, left)))
            try:
                chunk = ssock.recv(65536)
            except (socket.timeout, ssl.SSLError):
                break
            if not chunk:
                break
            if not body_started:
                head_buf += chunk
                idx = head_buf.find(b"\r\n\r\n")
                if idx < 0:
                    if len(head_buf) > 32768:     # 头异常大，放弃
                        break
                    continue
                colo = parse_colo(head_buf[:idx])
                rest = head_buf[idx + 4:]
                body_started = True
                first_byte = time.time()
                total += len(rest)
            else:
                total += len(chunk)

        if not body_started:
            return 0.0, colo
        # 用「首字节到结束」算吞吐，避开 TLS 握手那几百毫秒的干扰
        elapsed = max(0.2, time.time() - first_byte)
        if total <= 0:
            return 0.0, colo
        return total / elapsed / MB, colo
    except Exception:  # noqa: BLE001
        return 0.0, colo
    finally:
        for s in (ssock, sock):
            try:
                if s:
                    s.close()
            except Exception:  # noqa: BLE001
                pass


def read_csv(path):
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        rd = csv.reader(f)
        header = next(rd, None)
        for r in rd:
            if len(r) < 7:
                continue
            ip = r[0].strip()
            if not ip:
                continue
            rows.append({
                "ip": ip,
                "sent": r[1].strip(),
                "recv": r[2].strip(),
                "loss": r[3].strip(),
                "latency": r[4].strip(),
                "speed": r[5].strip(),
                "colo": r[6].strip(),
            })
    return header, rows


def write_csv(path, header, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        wr = csv.writer(f)
        if header:
            wr.writerow(header)
        for r in rows:
            wr.writerow([r["ip"], r["sent"], r["recv"], r["loss"],
                         r["latency"], r["speed"], r["colo"]])
    os.replace(tmp, path)


def to_float(v, default=0.0):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


def main(argv):
    if len(argv) < 9:
        sys.stderr.write(__doc__ or "")
        return 2

    in_csv, out_csv = argv[1], argv[2]
    dn = max(1, int(float(argv[3] or 10)))
    parallel = max(1, min(32, int(float(argv[4] or 4))))
    seconds = max(1, int(float(argv[5] or 10)))
    port = int(float(argv[6] or 443))
    url = argv[7] or "https://speed.cloudflare.com/__down?bytes=50000000"
    speed_min = to_float(argv[8], 0.0)

    header, rows = read_csv(in_csv)
    if not rows:
        sys.stderr.write("[dl] 输入 CSV 为空\n")
        return 1

    # cfst -dd 的输出已按延迟升序，取最靠前的 dn 个做下载测速
    targets = rows[:dn]
    sys.stderr.write("[dl] 并行下载测速：%d 个 IP，并发 %d，单次上限 %ds，阈值 >=%.2f MB/s\n"
                     % (len(targets), parallel, seconds, speed_min))

    total = len(targets)
    done = 0
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futs = {ex.submit(probe, t["ip"], port, url, seconds): t for t in targets}
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                mb, colo = fut.result()
            except Exception:  # noqa: BLE001
                mb, colo = 0.0, ""
            t["speed"] = "%.2f" % mb
            if colo and (not t["colo"] or t["colo"] == "N/A"):
                t["colo"] = colo
            with lock:
                done += 1
                cur = done
            # 给上层（WebUI / 跑批日志）看的进度，一行一条，便于正则提取
            sys.stderr.write("[dl] 进度 %d/%d  %s  %.2f MB/s %s\n"
                             % (cur, total, t["ip"], mb, colo or "-"))

    # 按速度降序；没测到速度的排最后（保持原来的延迟顺序）
    targets.sort(key=lambda r: (-to_float(r["speed"]), to_float(r["latency"], 9999)))

    kept = [r for r in targets if to_float(r["speed"]) >= speed_min] if speed_min > 0 else list(targets)
    if speed_min > 0:
        sys.stderr.write("[dl] 速度 >=%.2f MB/s 的有 %d / %d 个\n"
                         % (speed_min, len(kept), len(targets)))

    # 测速失败（0 MB/s）的 IP 不能当优选 IP 用，直接剔除
    usable = [r for r in kept if to_float(r["speed"]) > 0]
    if len(usable) != len(kept):
        sys.stderr.write("[dl] 剔除 %d 个测速失败（0 MB/s）的 IP\n" % (len(kept) - len(usable)))
    kept = usable

    write_csv(out_csv, header, kept)
    if kept:
        best = kept[0]
        sys.stderr.write("[dl] 完成，最快 %s  %.2f MB/s  %s\n"
                         % (best["ip"], to_float(best["speed"]), best["colo"]))
    else:
        sys.stderr.write("[dl] 没有 IP 达到速度下限，输出为空\n")
    return 0 if kept else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
