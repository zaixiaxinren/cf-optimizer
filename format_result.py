#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 cfst 的 result.csv 转成 `IP:端口#后缀` 列表（定时任务那条链路用）。

用法：
    python3 format_result.py <result.csv> <meta.tsv> <输出文件> \
            [SUFFIX_MODE] [TAG] [PORT] [TOP_N]

SUFFIX_MODE：
    source  优先用源列表里那一行自带的 #备注（没备注就回退到数据中心码）
    colo    数据中心码（如 LAX / SJC）
    fixed   固定用 TAG
    none    不加后缀

端口优先取源行里的端口（比如 all.txt 里那条 8443），否则用 PORT。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from resolve import load_meta  # noqa: E402


def main(argv):
    if len(argv) < 4:
        sys.stderr.write("usage: format_result.py <csv> <meta> <out> "
                         "[mode] [tag] [port] [topn]\n")
        return 2
    csv_path, meta_path, out_path = argv[1], argv[2], argv[3]
    mode = (argv[4] if len(argv) > 4 else "source") or "source"
    tag = argv[5] if len(argv) > 5 else "auto"
    try:
        default_port = int(argv[6]) if len(argv) > 6 else 443
    except ValueError:
        default_port = 443
    try:
        topn = int(argv[7]) if len(argv) > 7 else 10
    except ValueError:
        topn = 10

    meta = load_meta(meta_path)

    rows = []
    try:
        with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return 4

    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < 7:
            continue
        ip = parts[0].strip()
        if not ip:
            continue
        colo = parts[6].strip()
        remark, port = "", default_port
        if ip in meta:
            port, remark = meta[ip]
        if not port:
            port = default_port

        if mode == "none":
            suffix = ""
        elif mode == "fixed":
            suffix = tag
        elif mode == "source":
            suffix = remark or (colo if colo and colo != "N/A" else tag)
        else:  # colo
            suffix = colo if colo and colo != "N/A" else tag

        host = "[%s]" % ip if ":" in ip else ip
        line = "%s:%d" % (host, port)
        if suffix:
            line += "#" + suffix
        rows.append(line)
        if len(rows) >= topn:
            break

    if not rows:
        return 3

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(rows) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
