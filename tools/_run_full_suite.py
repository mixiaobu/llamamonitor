# 运行全量测试套件（1.3.3），逐例计时 + 汇总。
# 用法：python tools\_run_full_suite.py
# 输出：tools\tests_full_133d.log（逐例） + 结尾汇总 + 进度文件。
# 重要：主逻辑包在 main() + __main__ 守卫里——Windows multiprocessing 用
# spawn，子进程会重新导入本模块；若无守卫，任一使用 multiprocessing 的测试
# 都会让子进程重跑整个套件（资源争抢、进度文件互写、父进程被拖停）。
import sys
import os
import time
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    os.chdir(ROOT)
    # 与 `python -m unittest discover -s tests`（cwd=ROOT 在 sys.path）一致：
    # 必须同时把 ROOT 与 tests/ 加入 sys.path，否则部分测试模块导入失败、
    # discover 只发现子集（曾误得 418 而非 766）。
    for _p in (os.path.join(ROOT, "tests"), ROOT):
        if _p not in sys.path:
            sys.path.insert(0, _p)

    LOG = Path("tools") / "tests_full_133d.log"
    PROG = Path("tools") / "tests_full_133d_progress.log"
    LOG.parent.mkdir(exist_ok=True)
    with open(PROG, "w", encoding="utf-8") as _f:
        _f.write("")

    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(ROOT, "tests"), pattern="test_*.py")
    n = suite.countTestCases()

    results = []
    counters = {"done": 0}

    def _prog(msg):
        line = "[%3d/%d] %s" % (counters["done"], n, msg)
        print(line, file=sys.stderr, flush=True)
        with open(PROG, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    class TimingResult(unittest.TextTestResult):
        def startTest(self, test):
            super().startTest(test)
            self._t0 = time.time()

        def _finish(self, status, test):
            counters["done"] += 1
            dur = time.time() - self._t0
            results.append((dur, status, test))
            _prog("%.2fs %s %s" % (dur, status, test))

        def addSuccess(self, test):
            super().addSuccess(test)
            self._finish("OK", test)

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self._finish("FAIL", test)

        def addError(self, test, err):
            super().addError(test, err)
            self._finish("ERROR", test)

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            self._finish("SKIP", test)

    t0 = time.time()
    with open(LOG, "w", encoding="utf-8") as f:
        runner = unittest.TextTestRunner(
            stream=f, resultclass=TimingResult, verbosity=0)
        res = runner.run(suite)

    elapsed = time.time() - t0
    results.sort(key=lambda r: -r[0])
    with open(LOG, "a", encoding="utf-8") as f:
        f.write("\n=== SUMMARY ===\n")
        f.write("total cases: %d\n" % n)
        f.write("run: %d  ok: %s\n" % (res.testsRun, res.wasSuccessful()))
        f.write("failures: %d  errors: %d  skipped: %d\n"
                % (len(res.failures), len(res.errors), len(res.skipped)))
        f.write("elapsed: %.1fs\n" % elapsed)
        f.write("\n=== TOP 20 SLOWEST ===\n")
        for dur, status, test in results[:20]:
            f.write("%6.2fs  %-5s  %s\n" % (dur, status, test))

    print("SUITE_DONE cases=%d ok=%s fail=%d err=%d skip=%d elapsed=%.1fs"
          % (res.testsRun, res.wasSuccessful(), len(res.failures),
             len(res.errors), len(res.skipped), elapsed))
    return 0 if res.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
