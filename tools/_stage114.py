#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1.1.4 发布：git add 全部（modified + 新增 tools/scripts + 精选 artifacts）。
精选规则同 _curate114.py。"""
import os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

def run(*args):
    r = subprocess.run(args, capture_output=True)
    if r.returncode != 0:
        sys.stderr.buffer.write(r.stderr)
        sys.exit(f"git {args[1] if len(args)>1 else ''} failed: {r.returncode}")

PROFILE_PARTS = {"_profile", "_p2", "_p3", "_p4", "profile", "profile-r2", "dbgprofile", "edge-profile"}
MAX = 1 * 1024 * 1024

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

def git(*args):
    out = subprocess.run(["git"] + list(args), capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"git {' '.join(args)}: {out.stderr[:500]}")
    return out.stdout

# *.log 被全局 gitignore；审计 log 精选入库（先例：1.1.1 交付截图+日志）
run("git", "config", "advice.addIgnoredFile", "false")

# 1) 所有 modified tracked 文件
mod = git("status", "--short").splitlines()
modified = [l[3:].strip() for l in mod if l[:2] in (" M", "MM")]
run("git", "add", "--", *modified)
print(f"modified: {len(modified)}", file=sys.stderr)

# 2) 新增 tools/ + scripts/ 全部
new_ts = [l[3:].strip() for l in mod if l[:3] == "?? " and (l[3:].startswith("tools/") or l[3:].startswith("scripts/"))]
run("git", "add", "--", *new_ts)
print(f"new tools/scripts: {len(new_ts)}", file=sys.stderr)

# 3) 精选 artifacts（先 reset 再 -f 添加：*.log 被全局 gitignore）
for d in DIRS:
    run("git", "reset", "-q", "--", d)
count = 0
for d, rule in DIRS.items():
    dpath = d.replace("/", os.sep)
    for dirpath, dirnames, filenames in os.walk(dpath):
        dirnames[:] = [x for x in dirnames if x not in PROFILE_PARTS]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if os.path.getsize(full) > MAX:
                continue
            rel = os.path.relpath(full, dpath).replace(os.sep, "/")
            if rule is not None and not rule(fn, rel):
                continue
            run("git", "add", "-f", "--", full)
            count += 1
print(f"curated artifacts: {count}", file=sys.stderr)
