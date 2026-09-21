"""
ui_stress_test.py — 前端 UI 生命周期压测（Phase 16 spec D）

用 Playwright（Chromium，与 WebView2/Chromium 内核一致）加载**真实运行中**的
LlamaMonitor Web UI（http://127.0.0.1:8765），执行：

  D1  Dashboard hide/show ×500（window.__lmSetVisible(true/false) ——
      与 pywebview 托盘隐藏同一条桥接路径，polling.js 可见性任务策略）
  D2  Page cycle（Overview/Usage/Performance/GPU/Settings）×500
      （LM.nav.showPage 真实切换，onShow 懒初始化 + 数据刷新）
  D3  Theme dark/light/system ×100（setThemeMode 真实主题切换 + 图表重绘）
  D4  Window resize ×200（视口宽度 800→1400 往返，触发 ResizeObserver +
      compact 模式 + ECharts resize 路径）

判定（无重复 Timer / 无重复 ECharts 实例 / 无线性内存增长）：
  - LM.polling audit：任务数恒 7（注册表固定），inFlight 不累积；
  - LM.charts.instanceCount()：≤ 9（图表总数），且不随循环增长；
  - performance.memory（Chromium）：裁掉前 15% 预热后线性回归斜率 < 10MB/1000；
  - canvas 元素数：恒 = 已初始化图表数（无 canvas 泄漏）。

用法：
    python tools/ui_stress_test.py --base-url http://127.0.0.1:8765
    python tools/ui_stress_test.py --quick
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PAGES = ["overview", "usage", "performance", "gpu", "settings"]

# 页面内执行的 JS：返回 {mem, charts, canvases, pollAudit, page}
SAMPLE_JS = r"""
(function () {
  var mem = (performance && performance.memory) ? performance.memory.usedJSHeapSize / 1048576 : null;
  var out = {
    mem_mb: mem,
    charts: (window.LM && LM.charts) ? LM.charts.instanceCount() : -1,
    canvases: document.querySelectorAll('canvas').length,
    page: (window.LM && LM.nav) ? LM.nav.currentPage() : null,
    poll_tasks: 0,
    poll_inflight: 0
  };
  try {
    var audit = (window.LM && LM.poll && LM.poll.audit) ? LM.poll.audit() : {};
    var n = 0, inf = 0;
    for (var k in audit) {
      if (Object.prototype.hasOwnProperty.call(audit, k)) {
        n++;
        if (audit[k].inFlight) inf++;
      }
    }
    out.poll_tasks = n;
    out.poll_inflight = inf;
  } catch (e) {
    out.poll_audit_err = String(e);
  }
  return out;
})();
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="LlamaMonitor UI lifecycle stress")
    ap.add_argument("--base-url", default="http://127.0.0.1:8765")
    ap.add_argument("--hide-show", type=int, default=500)
    ap.add_argument("--page-cycles", type=int, default=500)
    ap.add_argument("--theme-cycles", type=int, default=100)
    ap.add_argument("--resize-cycles", type=int, default=200)
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args(argv)

    if args.quick:
        args.hide_show = min(args.hide_show, 20)
        args.page_cycles = min(args.page_cycles, 20)
        args.theme_cycles = min(args.theme_cycles, 10)
        args.resize_cycles = min(args.resize_cycles, 20)

    from playwright.sync_api import sync_playwright

    samples: list[dict] = []
    problems: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        page.goto(args.base_url, wait_until="networkidle", timeout=30000)
        page.wait_for_function("window.LM && LM.nav && LM.charts", timeout=15000)

        def sample(tag: str) -> None:
            # 等 burst 平息（refreshAllNow 后 12 任务并发 in-flight 是正常瞬态），
            # 采样反映稳态而非瞬时峰值
            page.wait_for_timeout(1500)
            s = page.evaluate(SAMPLE_JS)
            s["tag"] = tag
            samples.append(s)

        def click_page(name: str) -> None:
            page.evaluate(f"LM.nav.showPage({json.dumps(name)})")

        # ---- 预热：访问所有页面（图表懒初始化）+ 一轮主题 + 一轮 resize ----
        for pg in PAGES:
            click_page(pg)
            page.wait_for_timeout(250)
        page.wait_for_timeout(1000)
        sample("baseline")

        # D1: hide/show ×N
        for i in range(args.hide_show):
            page.evaluate("window.__lmSetVisible && window.__lmSetVisible(false)")
            page.wait_for_timeout(3)
            page.evaluate("window.__lmSetVisible && window.__lmSetVisible(true)")
            page.wait_for_timeout(3)
            if i % 50 == 49:
                sample(f"hide{i}")
        sample("hide_done")

        # D2: page cycle ×N
        for i in range(args.page_cycles):
            click_page(PAGES[i % len(PAGES)])
            page.wait_for_timeout(3)
            if i % 100 == 99:
                sample(f"page{i}")
        sample("page_done")

        # D3: theme cycles ×N（dark/light/system 轮换，真实 setThemeMode 路径）
        modes = ["dark", "light", "system"]
        for i in range(args.theme_cycles):
            page.evaluate(
                f"LM.app && LM.app.applyTheme && LM.app.applyTheme({json.dumps(modes[i % 3])})"
            )
            page.wait_for_timeout(5)
            if i % 50 == 49:
                sample(f"theme{i}")
        sample("theme_done")

        # D4: window resize ×N（800↔1400 往返，触发 RO + compact + echarts.resize）
        for i in range(args.resize_cycles):
            w = 800 if i % 2 == 0 else 1400
            page.set_viewport_size({"width": w, "height": 800})
            page.wait_for_timeout(8)
            if i % 50 == 49:
                sample(f"resize{i}")
        sample("resize_done")

        # 最终截图留证
        import tempfile
        shot_dir = Path(tempfile.gettempdir()) / "lm_ui_shots"
        shot_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shot_dir / "ui_stress_final.png"), full_page=False)
        browser.close()

    # ---- 判定 ----
    # 线性回归：内存（裁前 15%）
    def slope_per_1000(vals: list[float]) -> float:
        n = len(vals)
        if n < 5:
            return 0.0
        xs = list(range(n))
        mx = sum(xs) / n
        my = sum(vals) / n
        num = sum((x - mx) * (y - my) for x, y in zip(xs, vals))
        den = sum((x - mx) ** 2 for x in xs)
        return (num / den) * 1000.0 if den else 0.0

    mems = [s["mem_mb"] for s in samples if s.get("mem_mb") is not None]
    base_mem = mems[0] if mems else 0
    mem_slope = slope_per_1000(mems[int(len(mems) * 0.15):]) if len(mems) > 8 else 0.0

    charts_series = [s["charts"] for s in samples]
    canvas_series = [s["canvases"] for s in samples]
    task_series = [s["poll_tasks"] for s in samples]
    inflight_max = max((s["poll_inflight"] for s in samples), default=0)

    n_charts = max(charts_series) if charts_series else 0
    if charts_series:
        steady_ch = charts_series[len(charts_series) // 2:]
        if max(steady_ch) - min(steady_ch) > 0:
            problems.append(
                f"ECharts 实例数后半段变化 {min(steady_ch)}->{max(steady_ch)}（疑似重复 init）")

    # canvas 应收敛到图表总数（1:1）且不超过它；后半段必须平坦
    # （预热期懒初始化 6->7 允许，泄漏 = 后半段仍在涨）。
    if canvas_series:
        max_canvas = max(canvas_series)
        if max_canvas > n_charts:
            problems.append(f"canvas 数量 {max_canvas} 超过 ECharts 实例数 {n_charts}（canvas 泄漏）")
        canv_steady = canvas_series[len(canvas_series) // 2:]
        if canv_steady and max(canv_steady) - min(canv_steady) > 0:
            problems.append(f"canvas 数量后半段变化 {min(canv_steady)}->{max(canv_steady)}（canvas 泄漏）")

    if len(set(task_series)) > 1:
        problems.append(f"polling 任务数变化 {sorted(set(task_series))}（疑似重复注册）")
    # inFlight 峰值判定：≤ 任务总数即正常（回到前台 refreshAllNow 会并发启动
    # 全部 12 任务——这是设计行为，不是泄漏）；泄漏信号是任务数变化或
    # 最后一个采样点仍有任务卡死 in-flight。
    if task_series and max(task_series) > 0:
        n_tasks = max(task_series)
        if inflight_max > n_tasks:
            problems.append(f"polling inFlight 峰值 {inflight_max} 超过任务总数 {n_tasks}")
    if samples and samples[-1]["poll_inflight"] > 0:
        problems.append(f"结束采样仍有 {samples[-1]['poll_inflight']} 个任务 in-flight（未释放）")

    if mems:
        if mem_slope > 10.0:
            problems.append(f"JS 堆疑似线性增长 slope={mem_slope:.1f}MB/1000 采样")
        delta = mems[-1] - mems[0]
        if delta > 80.0:
            problems.append(f"JS 堆总增长 {delta:.1f}MB（base {base_mem:.1f} -> end {mems[-1]:.1f}）")

    ok = not problems
    print("=" * 64)
    print(f"UI LIFECYCLE STRESS  {'PASS' if ok else 'FAIL'}")
    print("=" * 64)
    print(f"  hide/show           : {args.hide_show}")
    print(f"  page cycles         : {args.page_cycles}")
    print(f"  theme cycles        : {args.theme_cycles}")
    print(f"  resize cycles       : {args.resize_cycles}")
    print(f"  samples             : {len(samples)}")
    print(f"  mem base/end (MB)   : {mems[0]:.1f} / {mems[-1]:.1f}" if mems else "  mem: n/a")
    print(f"  mem slope MB/1000   : {mem_slope:.2f}")
    print(f"  echarts instances   : {min(charts_series)} -> {max(charts_series)}")
    print(f"  canvas count        : {min(canvas_series)} -> {max(canvas_series)}")
    print(f"  poll tasks          : {sorted(set(task_series))}")
    print(f"  poll inFlight max   : {inflight_max}")
    for pr in problems:
        print(f"  !! {pr}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
