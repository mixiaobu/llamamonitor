#!/usr/bin/env python3
"""汇总 r8_mobile-audit/shots 下全部 *_result-*.json：
- 每视口每页：docHScroll / contentHScroll / overCount / nestedScroll 数量
- 列出所有失败项
- 输出 PASS/FAIL 汇总
"""
import json, glob, sys
from collections import defaultdict

base = r"artifacts/r8_mobile-audit/shots"
files = glob.glob(base + r"/*/*_result-*.json")
combos = defaultdict(dict)
missing = defaultdict(list)
pages = ["overview","usage","performance","system","gpu","history","settings","about"]
combos_exp = ["320x568","360x800","390x844","430x932","844x390","932x430","988x1394","1920x1080","2560x1440"]
for f in files:
    d = json.load(open(f, encoding="utf-8-sig"))
    tag = "%dx%d" % (d["w"], d["h"])
    combos[tag][d["page"]] = d["probe"]

fails = 0
for tag in combos_exp:
    for p in pages:
        pr = combos.get(tag, {}).get(p)
        if pr is None:
            missing[tag].append(p); continue
        hs = pr["docHScroll"] or pr["contentHScroll"]
        over = pr["overCount"]
        nested = len(pr["nestedScroll"])
        if hs or over or nested:
            fails += 1
            print("FAIL %s %s: docHS=%s contHS=%s over=%s nested=%s" %
                  (tag, p, pr["docHScroll"], pr["contentHScroll"], over,
                   [n.get("cls") for n in pr["nestedScroll"]]))
for tag in combos_exp:
    if tag in missing:
        fails += len(missing[tag])
        print("MISSING %s: %s" % (tag, missing[tag]))
total = len(combos_exp) * len(pages)
print("---")
print("total=%d checked=%d fails=%d" % (total, len(files), fails))
print("RESULT:", "PASS" if fails == 0 and len(files) == total else "FAIL")
