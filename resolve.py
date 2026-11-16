#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把各种格式的候选列表解析成 CloudflareST 能直接吃的纯 IP 列表。

为什么需要它：
    CloudflareST 只认 IP / CIDR，遇到 `域名`、`IP:端口`、`#备注` 任何一样
    都会直接 ParseCIDR err 退出（一条结果都不会有）。
    而网上流传的优选列表（比如 bestcf 的 domain/all.txt）几乎都是
    `host:443#备注名` 或纯域名，原样喂进去必然 0 结果。

支持的输入行（可混写）：
    1.1.1.1
    1.1.1.0/24
    2606:4700::/32
    1.1.1.1:443
    1.1.1.1:8443#EDT导航
    www.shopify.com:443#企业域名 | Shopify
    # 整行注释、空行会被忽略

输出：
    clean 文件  每行一个 IP / CIDR，给 cfst -f 用
    meta 文件   TSV，三列：IP \t 端口 \t 备注（端口、备注来自源行，供推送时用）

命令行：
    python3 resolve.py <源文件> <clean输出> <meta输出>

环境变量：
    ONLY_IPV4=1          域名只解析 A 记录（默认 1；=0 时 AAAA 也一起要）
    MAX_IP_PER_DOMAIN=4  单个域名最多取几个 IP（默认 4）
    DEFAULT_PORT=443     源行没写端口时用的端口
"""
import ipaddress
import os
import re
import socket
import sys

ONLY_IPV4 = os.environ.get("ONLY_IPV4", "1") != "0"
CF_ONLY = os.environ.get("CF_ONLY", "1") != "0"
try:
    MAX_PER_DOMAIN = max(1, int(os.environ.get("MAX_IP_PER_DOMAIN", "4")))
except ValueError:
    MAX_PER_DOMAIN = 4
DEFAULT_PORT = (os.environ.get("DEFAULT_PORT", "443") or "443").strip()

# IP 版本：ipv4（默认）/ ipv6 / both
# 可以用环境变量 IP_MODE 指定，也可以在命令行第 4 个参数传（命令行优先）。
# 兼容老的 ONLY_IPV4=0 → both
IP_MODE = (os.environ.get("IP_MODE", "") or "").strip().lower()
if not IP_MODE:
    IP_MODE = "ipv4" if ONLY_IPV4 else "both"
if IP_MODE not in ("ipv4", "ipv6", "both"):
    IP_MODE = "ipv4"


def _fams(ip_mode):
    """按 IP 版本返回要解析的地址族"""
    if ip_mode == "ipv4":
        return [socket.AF_INET]
    if ip_mode == "ipv6":
        return [socket.AF_INET6]
    return [socket.AF_INET, socket.AF_INET6]


def _want_ip(text, ip_mode):
    """这一行/这个 IP 是否属于当前要测的版本"""
    if ip_mode == "both":
        return True
    is_v6 = ":" in str(text)
    return is_v6 if ip_mode == "ipv6" else (not is_v6)

# Cloudflare 官方 anycast 段（https://www.cloudflare.com/ips-v4 + ips-v6）
# 域名列表里常混进别的 CDN（比如 Shopify 的 23.227.38.x），
# 这些 IP 测速可能很快，但拿来当"优选 IP"是错的，必须剔掉。
CF_NETS = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
]
_CF_NET_OBJS = [ipaddress.ip_network(n) for n in CF_NETS]


def is_cf_ip(text):
    """是否属于 Cloudflare 官方段"""
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    for net in _CF_NET_OBJS:
        if ip.version == net.version and ip in net:
            return True
    return False


def is_cf_net(net):
    """整个网段是否落在 Cloudflare 官方段内"""
    for cf in _CF_NET_OBJS:
        if net.version != cf.version:
            continue
        try:
            if net.subnet_of(cf):
                return True
        except (TypeError, ValueError):
            continue
    return False

_BRACKET_RE = re.compile(r"^\[([0-9A-Fa-f:.]+)\](?::(\d{1,5}))?$")
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}\.?$")


def _split_line(line):
    """拆成 (host, port, remark)，不是有效行返回 None"""
    s = line.strip()
    if not s or s.startswith("#") or s.startswith("//"):
        return None

    remark = ""
    if "#" in s:
        s, remark = s.split("#", 1)
        remark = remark.strip()
    s = s.strip().strip(",").strip()
    if not s:
        return None

    port = ""
    m = _BRACKET_RE.match(s)          # [2606:4700::1]:443
    if m:
        s, port = m.group(1), m.group(2) or ""
    elif s.count(":") == 1:           # 1.1.1.1:443 / www.a.com:443
        head, tail = s.split(":", 1)
        if tail.isdigit():
            s, port = head.strip(), tail

    if not s:
        return None
    return s, (port or DEFAULT_PORT), remark


def _domain_ips(host, warnings, ip_mode="ipv4"):
    """解析域名的 A / AAAA 记录，返回去重后的 IP 列表。

    双栈模式（both）下每个地址族各取 MAX_PER_DOMAIN 个，避免 A 记录把
    AAAA 记录挤掉（原来按总数限制，v6 永远轮不上）。
    """
    out, seen = [], set()
    for fam in _fams(ip_mode):
        got = 0
        try:
            infos = socket.getaddrinfo(host, None, fam, socket.SOCK_STREAM)
        except socket.gaierror as e:
            warnings.append("解析失败 %s (%s)" % (host, e))
            continue
        except Exception as e:                      # noqa: BLE001
            warnings.append("解析异常 %s (%s)" % (host, e))
            continue
        for info in infos:
            ip = info[4][0]
            if "%" in ip:                     # 去掉链路本地地址的 scope id（fe80::1%eth0）
                ip = ip.split("%", 1)[0]
            if ip in seen:
                continue
            seen.add(ip)
            out.append(ip)
            got += 1
            if got >= MAX_PER_DOMAIN:
                break
    if not out:
        warnings.append("无可用记录 %s" % host)
    return out


def resolve_text(raw, ip_mode="ipv4"):
    """解析文本，返回 (clean_text, meta_lines, stats, warnings)

    clean_text : 每行一个 IP / CIDR
    meta_lines : 每行 "IP\\t端口\\t备注"
    stats      : dict(ip/domain/cidr/fail/in/out)
    """
    warnings = []
    meta = {}          # ip -> (port, remark)，重复 IP 只留第一次出现的
    order = []
    stats = {"in": 0, "ip": 0, "domain": 0, "cidr": 0, "fail": 0,
             "noncf": 0, "skipfam": 0, "out": 0}

    def keep(ip, port, remark):
        """登记一条，已存在则跳过（先出现的端口/备注优先）"""
        if ip in meta:
            return
        meta[ip] = (port, remark)
        order.append(ip)

    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.strip() or line.strip().startswith("#"):
            continue
        stats["in"] += 1
        parsed = _split_line(line)
        if not parsed:
            stats["fail"] += 1
            continue
        host, port, remark = parsed

        # 1) CIDR 网段，原样交给 cfst
        if "/" in host:
            if not _want_ip(host, ip_mode):
                stats["skipfam"] += 1
                continue
            try:
                net = ipaddress.ip_network(host, strict=False)
            except ValueError:
                stats["fail"] += 1
                warnings.append("无效网段 %s" % host)
                continue
            if CF_ONLY and not is_cf_net(net):
                stats["noncf"] += 1
                warnings.append("非 Cloudflare 网段已跳过 %s" % host)
                continue
            keep(host, port, remark)
            stats["cidr"] += 1
            continue

        # 2) 纯 IP
        try:
            ip = str(ipaddress.ip_address(host))
        except ValueError:
            ip = ""

        if ip:
            if not _want_ip(ip, ip_mode):
                stats["skipfam"] += 1
                continue
            if CF_ONLY and not is_cf_ip(ip):
                stats["noncf"] += 1
                warnings.append("非 Cloudflare IP 已跳过 %s (%s)" % (ip, remark or "-"))
                continue
            keep(ip, port, remark)
            stats["ip"] += 1
            continue

        # 3) 域名 -> 解析成 IP
        if not _DOMAIN_RE.match(host):
            stats["fail"] += 1
            warnings.append("无法识别 %s" % host)
            continue
        ips = _domain_ips(host, warnings, ip_mode)
        if not ips:
            stats["fail"] += 1
            continue
        stats["domain"] += 1
        for one in ips:
            if CF_ONLY and not is_cf_ip(one):
                stats["noncf"] += 1
                warnings.append("非 Cloudflare IP 已跳过 %s (%s)" % (one, host))
                continue
            # 备注里补上来源域名，方便在面板上认出来
            keep(one, port, remark or host)

    clean = order
    stats["out"] = len(clean)
    clean_text = "\n".join(clean) + ("\n" if clean else "")
    meta_lines = "".join("%s\t%s\t%s\n" % (ip, meta[ip][0], meta[ip][1])
                         for ip in clean)
    return clean_text, meta_lines, stats, warnings


def load_meta(path):
    """读 meta 文件 -> {ip: (port:int, remark:str)}"""
    out = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                parts = ln.rstrip("\n").split("\t")
                if len(parts) < 3:
                    continue
                ip, port, remark = parts[0].strip(), parts[1].strip(), parts[2].strip()
                if not ip:
                    continue
                try:
                    p = int(port)
                except ValueError:
                    p = int(DEFAULT_PORT or 443)
                out[ip] = (p, remark)
    except FileNotFoundError:
        pass
    return out


def main(argv):
    if len(argv) < 4:
        sys.stderr.write("usage: resolve.py <src> <clean_out> <meta_out>\n")
        return 2
    src, clean_out, meta_out = argv[1], argv[2], argv[3]
    try:
        with open(src, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except FileNotFoundError:
        sys.stderr.write("找不到源文件: %s\n" % src)
        return 1

    ip_mode = (argv[4].strip().lower() if len(argv) > 4 and argv[4].strip() else IP_MODE)
    if ip_mode not in ("ipv4", "ipv6", "both"):
        ip_mode = IP_MODE

    clean_text, meta_lines, stats, warns = resolve_text(raw, ip_mode)
    with open(clean_out, "w", encoding="utf-8") as f:
        f.write(clean_text)
    with open(meta_out, "w", encoding="utf-8") as f:
        f.write(meta_lines)

    print("[resolve] 输入 %d 行 -> IP %d 个（%s；域名展开 %d 个、网段 %d 个，"
          "跳过 %d 行、非 CF 剔除 %d 个、版本不符 %d 个），去重后 %d 个"
          % (stats["in"], stats["ip"], ip_mode, stats["domain"], stats["cidr"],
             stats["fail"], stats["noncf"], stats["skipfam"], stats["out"]))
    for w in warns[:8]:
        print("[resolve] %s" % w)
    if len(warns) > 8:
        print("[resolve] ...另有 %d 条提示" % (len(warns) - 8))
    return 0 if stats["out"] else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
