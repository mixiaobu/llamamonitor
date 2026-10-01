#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1.1.4 发布：计算 artifacts 精选文件清单（排除 Edge 浏览器 profile 目录 + >1MB 大文件），
输出 git add 参数列表（NUL 分隔）。"""
import os, sys, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

PROFILE_PARTS = {"_profile", "_p2", "_p3", "_p4", "profile", "profile-r2", "dbgprofile", "edge-profile"}
MAX = 1 * 1024 * 1024

# 目录 -> 保留规则（None = 保留全部；callable = 文件名过滤）
DIRS = {
    "artifacts/edge-ovr-r2": None,
    "artifacts/gpu-r4-audit": None,
    "artifacts/history-r5-audit": None,
    "artifacts/overview-r6-audit": None,
    "artifacts/ovr-audit": None,
    "artifacts/ovr-r4": None,
    "artifacts/perf-r5-audit": None,
    "artifacts/r7_settings-audit": None,
    "artifacts/r8_final-audit": None,
    "artifacts/r8_mobile-audit": lambda name, rel: rel.startswith(("after/", "before/")),
    "artifacts/sys-r3-audit": lambda name, rel: (
        re.match(r"^final-sys-|^after-func-|^after-sys-(1920x1080|320x568|390x844)-", name)
        or name in ("REPORT.md", "full_suite.log", "shots_matrix.log", "server_err.log")
    ),
}

keep = []
for d, rule in DIRS.items():
    for dirpath, dirnames, filenames in os.walk(d):
        # 剪掉 profile 目录
        dirnames[:] = [x for x in dirnames if x not in PROFILE_PARTS and not x.startswith("edge-profile")]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, d)
            if os.path.getsize(full) > MAX:
                continue
            if rule is not None and not rule(fn, rel):
                continue
            keep.append(full)

keep.sort()
total = sum(os.path.getsize(f) for f in keep)
print(f"# keep {len(keep)} files, {total/1024/1024:.1f} MB", file=sys.stderr)
sys.stdout.write("\0".join(keep) + "\0")
