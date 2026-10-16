# 单测耗时分析（临时）
import io
import os
import sys
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

loader = unittest.TestLoader()
suite = loader.discover(sys.argv[1], pattern=sys.argv[2], top_level_dir=sys.argv[1])
cases = []


def flatten(s):
    for item in s:
        if isinstance(item, unittest.TestSuite):
            flatten(item)
        else:
            cases.append(item)


flatten(suite)
rows = []
for t in cases:
    t0 = time.time()
    res = unittest.TestResult()
    res.buffer = False
    t.run(res)
    rows.append((time.time() - t0, t.id(), res.wasSuccessful()))

rows.sort(reverse=True)
for dur, tid, ok in rows[:15]:
    print("%.1fs  %s  %s" % (dur, "OK " if ok else "FAIL", tid))
print("\ntotal: %.1fs, tests: %d" % (sum(r[0] for r in rows), len(rows)))
