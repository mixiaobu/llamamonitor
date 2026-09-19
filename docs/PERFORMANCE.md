# LlamaMonitor 性能基线（Phase 14）

> 测量环境：Windows 11 桌面机，LlamaMonitor 0.13.1（安装版，后台模式，
> 真实 llama-server 127.0.0.1:9091 在产生流量，5s 轮询）。
> 测量方法：Get-Process 采样（RSS/handles/threads/CPU 累计），间隔 30s。
> **这是 audit 前的 before 基线；修复完成后在此补 after 对比（§131 性能 Gate）。**

## 1. 进程资源（before）

| 状态 | RSS | handles | threads | CPU（30s 采样） |
|---|---|---|---|---|
| 后台/托盘（idle，~25min 运行后） | 195.9→195.8 MB（4 样本极差 1.1MB，**无增长趋势**） | 808→810（稳定） | 22 | 2.04~2.06s / 30s ≈ **~6.9% 单核** |
| Dashboard 打开 | 196.5~196.6 MB（+1MB） | 820~823（+12，webview） | 25（+3，webview UI） | 2.26s / 30s ≈ ~7.5% 单核 |
| 窗口 WM_CLOSE -> 隐藏托盘 | 198.6 MB | 818 | 24 | 正常回落 |

观察：
- 4 个 30s 采样点 RSS/handles 均**无单调增长**（tray 模式）；
  内存/句柄 soak 的长期验证见 §4（24h burn-in 期间每日记录）。
- idle CPU ~7% 单核：Python 全栈（uvicorn+FastAPI+httpx 每 5s 轮询 +
  30s 托盘状态 httpx.get 临时 client + 每 5s SQLite 写）的稳态开销。
  相对被监控对象（llama.cpp 推理占满 GPU + 多核 CPU）开销可忽略，
  但若 audit 修复后显著恶化需调查（§131）。

## 2. Collector 单轮开销（before，真实 /metrics）

| 项目 | 值 |
|---|---|
| /metrics 响应体 | 3,074 B（51 行 Prometheus 文本） |
| 抓取（**keep-alive 复用**，应用稳态） | ~10 ms（collector 内置复用 client；Phase 1 实测） |
| 抓取（新 TCP 连接，Windows 首包开销） | ~1.6 s（已知 Windows 行为，collector 复用连接规避） |
| parse_metrics | 0.19~0.43 ms |
| DB 写（单事务 state+daily+live+清理） | <5 ms（WAL，本机 SSD） |

结论：监控自身单轮开销 ~10-20ms 量级（每 5s 一次），远小于 llama.cpp 推理开销。

## 3. 慢轮询叠加检查（前端）

前端 polling 与 API 的叠加行为见 AUDIT_REPORT 的 WEB 域 findings；
后端单事件循环串行 + httpx 连接池，无重叠请求放大。

## 4. Soak 计划（burn-in 期间每日记录）

0.14.0 candidate burn-in（≥48h）期间每日记录：RSS / handles / DB size /
WAL size / log size / 覆盖率 / gaps / Today & Total tokens。
无异常趋势即通过（§91/92/144/145）。

## 5. Before/After 对比（0.14.0 RC 实测，2026-09-19 21:34–21:40）

测量方法同 before：`scripts/perf_sample.ps1`（Get-Process 4×30s 采样），
0.14.0 RC 安装版、`--background` 托盘模式、真实 llama-server 流量、5s 轮询。

| 指标 | before（0.13.1） | after（0.14.0） | 结论 |
|---|---|---|---|
| RSS（托盘 idle） | ~196 MB | **146.3–147.8 MB**（4 样本极差 1.5 MB，无增长） | 无回退；after 实例全程未开过窗口（无 webview 常驻），属最小占用形态（自启场景） |
| handles（托盘 idle） | ~809 | **488–491**（稳定） | 无回退（口径同上） |
| threads（托盘 idle） | 22 | 16 | 无回退 |
| CPU（托盘 idle） | ~2.05 s/30s ≈ 6.9% 单核 | **0.49–0.53 s/30s ≈ 1.7% 单核** | 无回退；before 窗口曾打开（隐藏后 30s/120s 轮询仍跑，且打的是旧的全表扫 quality/daily 端点），after 纯后台无前端轮询 |
| RSS（Dashboard 打开） | ~196.5 MB | **150.6→151.7 MB**（唤醒后 ~1min，ECharts 仍在加载，取保守上界） | 无回退 |
| CPU（Dashboard 打开） | ~2.26 s/30s ≈ 7.5% 单核 | **0.58–0.62 s/30s ≈ 2.0% 单核** | 改善：窗口轮询打的是修复后的端点（quality 按日索引查询、daily 单次取数 + 预分组、in-flight 去重） |

说明：
- 两组测量的窗口生命周期不同（before 实例运行期间开过窗口后隐藏；
  after 第一组纯后台、第二组唤醒后 ~1min）。两组都满足"无回退"；
  after 的改善主要来自 AUDIT-DB-003 端点修复 + AUDIT-WEB-002/003/007
  前端轮询优化，属预期内收益。
- /metrics 抓取与 parse 开销：代码路径未变（keep-alive 复用 client +
  parse_metrics 纯字符串解析），before 的 ~10 ms / ~0.3 ms 继续成立；
  AUDIT-ASYNC-004（keepalive_expiry 联动）与 AUDIT-DATA-003（16MB 上限）
  不改变正常路径延迟（上限只在异常响应时生效）。
- 长期泄漏验证（RSS/handles 单调性）见 §4 burn-in 每日记录。
