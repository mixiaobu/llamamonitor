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

## 5. Before/After 对比（audit 修复后填写）

| 指标 | before | after | 结论 |
|---|---|---|---|
| idle RSS（tray） | ~196 MB | _待测_ | |
| idle handles | ~809 | _待测_ | |
| idle CPU | ~6.9% 单核 | _待测_ | |
| /metrics 抓取（keep-alive） | ~10 ms | _待测_ | |
| parse | ~0.3 ms | _待测_ | |
