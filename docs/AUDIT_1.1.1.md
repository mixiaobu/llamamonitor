# LlamaMonitor 1.1.1 质量审计报告（AUDIT_1.1.1）

> 审计对象：v1.1.0（commit f56c215，已发布 Release）
> 基线：487 例全量测试全绿（444.7s 实测）
> 方法：PASS A 盘点 → PASS B 五通道审计（核心后端通读 / 前端 UI-UX / 文档卫生 / 测试覆盖 / 后端可靠性与性能）→ PASS C 汇总 → PASS D 运行时验证
> 原则：正确 > 稳定 > 易用 > 美观 > 新功能；只修有具体收益的问题。

## Executive Summary

1.1.0 核心后端质量高：**0 BLOCKER**。Token 计数链（reset/baseline/缺口/possible_token_loss）、
MTP 动态 position、隐私白名单（chat_template 不落库/日志/API）、端点故障隔离、
GPU null 语义、能量积分（monotonic + 长 gap 不积分）、SQLite WAL/重试/保留、
更新器验签链、单实例互斥——全部静态+运行时验证 OK。

1.1.1 修复清单（按投入产出排序）：
1. **正确性**：GAP-001 `/api/mtp` 的"今日"用 wall `local_date()` 而兄弟端点全用
   `collector.clock`（跨午夜 desync，与 1.1.0 BUG-A 同类，漏网一个端点）；
   DATA-1111-006 CPU 能耗跨午夜不分割（GPU 有分割，口径不一致）；
   BUG-1111-002 事件/缺口表 title 属性单引号截断。
2. **稳定/性能**：REL-1111-001 system 采集器每 2s 同步 psutil 阻塞事件循环 → to_thread；
   PERF-1111-004 /api/data/quality 每轮拉 10 万行 data_gaps → 聚合查询；
   PERF-1111-002/003/012/013 check-database / inventory-manual / clear-live / CSV → to_thread；
   REL-1111-005 health 无 debounce 单次抖动即 Unavailable → 2 次连续失败才翻转。
3. **前端 UX**：BUG-1111-003 Performance 页 MTP 趋势图停留期间不更新；
   UX-1111-001 在线态无"最后更新 X 秒前"；BUG-1111-004 格式化精度；
   BUG-1111-005 "本月"图表仍走浏览器时区前端过滤；UX-1111-006 事件按钮焦点丢失。
4. **文档一致性**：DOC-001/002/005（README /metrics 声明、分区矛盾、API.md 缺 1.1 端点）、
   DOC-003/004/006/007（废弃术语、config.example 漂移、ARCHITECTURE/METRICS 停旧版本）。
5. **仓库卫生**：SEC-001 .gitignore `.venv*/`；SEC-002 tracked 开发产物收编；
   SEC-003 build.bat 缺硬件桥打包；SEC-006/008 spec/data/monitor.db 残留。
6. **测试加固**：GAP-002 bridge 监督循环（崩溃重启/非法 JSON）；GAP-003 WAL 模式备份；
   GAP-005 500 INTERNAL_ERROR 契约；GAP-006 poller 循环存活；GAP-001 跨午夜回归。

运行时验证（PASS D，本机实测）：
- LlamaMonitor idle 15s：**CPU 2.4%（单核），RAM ~111 MB，24 线程**；
  HardwareSensorBridge：CPU 0.2%，RAM 37 MB，9 线程（无增长）。
- API latency（真实 DB）：P50 22-35ms；慢端点 /api/gpu/live?minutes=2880 ~1.0s、
  /api/system/live?minutes=1440 ~0.9s（48h 全量物化+降采样，事件循环同步——PERF-1111-008 相关，
  属"最大范围查询"，1s 级可接受，记录观察不强制修）。
- Headless Chrome 8 页巡检：**0 console 错误 / 0 页面异常 / 0 网络失败**；
  确认在线态 ovLastUpdate 恒空（UX-1111-001 复现）。

## 发现总览

### BLOCKER（0）

无。

### HIGH（10）

| ID | 域 | 摘要 | 状态 |
|----|----|------|------|
| BUG-1111-002 | 前端 | 事件/缺口表 `esc` 不转义单引号，`title='…'` 属性截断（app.js:1111-1115/485-489） | PASS E |
| BUG-1111-003 | 前端 | Performance 页 MTP 接受率趋势图停留期间/跨天不更新（daily 链路未挂载） | PASS E |
| DOC-1111-001 | 文档 | README:5 "只读取 /metrics" 与 1.1 五端点现实矛盾 | PASS E |
| DOC-1111-002 | 文档 | README 设置分区两处矛盾 + 漏 System 分区说明 | PASS E |
| DOC-1111-005 | 文档 | docs/API.md 缺全部 1.1 端点（README:418 指路牌残缺） | PASS E |
| GAP-001 | 数据 | server.py:2046/2059 `/api/mtp` 用 wall `local_date()`，兄弟端点全用 `collector.clock`（跨午夜 desync） | PASS E |
| PERF-1111-004 | 性能 | /api/data/quality 每轮 `get_gaps(limit=100000)` 全量物化（server.py:1588）→ 聚合查询 | PASS E |
| REL-1111-001 | 可靠性 | system 采集器每 2s 同步 psutil 在事件循环（~20-50ms/次周期阻塞）→ to_thread | PASS E |
| UX-1111-001 | 前端 | 在线态"最后更新"被清空（app.js:68-70），数据新鲜度不可见 | PASS E（显示"最后更新 X 秒前"） |
| GAP-002 | 测试 | bridge `_supervise_loop`/`_read_stdout` 无测试（崩溃重启/非法 JSON 承诺无守护） | PASS F（测试） |

### MEDIUM（22）

| ID | 域 | 摘要 | 状态 |
|----|----|------|------|
| DATA-1111-006 | 数据 | CPU 能耗跨午夜不分割（GPU 有 `_split_energy_across_midnight`） | PASS F |
| REL-1111-005 | 可靠性 | health 状态机无 debounce，单次抖动即 Unavailable + 事件 | PASS F |
| PERF-1111-002 | 性能 | /api/data/check-database 同步 quick_check → to_thread | PASS F |
| PERF-1111-003 | 性能 | /api/system/inventory?manual 同步 CIM（10s）无节流 → to_thread + 节流 | PASS F |
| PERF-1111-012 | 性能 | clear-live 的 VACUUM 同步 → to_thread | PASS F |
| PERF-1111-013 | 性能 | CSV 导出整文件事件循环内构建 → to_thread | PASS F |
| BUG-1111-010 | 前端 | 任务结束后 slot 数字字段陈旧最长 ~10s（is_processing 正确，数字 stale）→ UI 淡化/标注 | PASS F |
| REL-1111-009 | 可靠性 | bridge reader 线程 readline 无超时（静默卡死不重启） | PASS F（心跳超时） |
| BUG-1111-004 | 前端 | formatTokenCount 恒 2 位小数；TPS 卡片(2位)/图表(1位)精度不一 | PASS F |
| BUG-1111-005 | 前端 | "本月"图表/表格仍走浏览器时区前端过滤（app.js:982-986） | PASS F |
| UX-1111-006 | 前端 | 事件"查看更多"被轮询全量重建：焦点丢失、无收起 | PASS F |
| GAP-003 | 测试 | 全部备份测试 wal=False；WAL 模式备份未测 | PASS F（测试） |
| GAP-005 | 测试 | 500 INTERNAL_ERROR handler 零测试 | PASS F（测试） |
| GAP-006 | 测试 | poller 循环 out()/sleep 异常杀循环无测试（+ 放宽 try 范围） | PASS F |
| BUG-1111-007* | 可靠性 | 主进程硬杀后孤儿 HardwareSensorBridge.exe（无 Job Object/启动清扫） | PASS F（启动清扫，LOW-MED 边界） |
| SEC-1111-001 | 卫生 | .venv-final/ 未被 .gitignore 覆盖 | PASS G |
| SEC-1111-002 | 卫生 | 33+ tracked 开发产物（_screenshots_16f/11 PNG、artifacts/、soak、docs/screenshots×2） | PASS G |
| DOC-1111-003 | 文档 | README 废弃术语 Busy Slots/Token Max/Draft Sequences | PASS E（随 001/002 一次改） |
| DOC-1111-004 | 文档 | config.example.json 三处漂移 + 缺 system 段 | PASS E（从 config.py 重新生成） |
| DOC-1111-006 | 文档 | ARCHITECTURE.md 停 v0.13.1（x4 任务 vs 实际 7） | PASS F |
| DOC-1111-007 | 文档 | METRICS_DEFINITIONS 缺 1.1 指标 + gap reason 漏 invalid_metrics | PASS F |
| DOC-1111-013 | 卫生 | docs/ 14 个历史报告无 historical 标记 + 2 截图目录 | PASS G |

\* BUG-1111-007 定级 MEDIUM（待运行时验证项：启动清扫实现成本低，Job Object 留延后）。

### LOW / POLISH（18）

| ID | 域 | 摘要 | 状态 |
|----|----|------|------|
| BUG-1111-009 | 前端 | Slot 卡术语偏离术语表（上下文容量/Prompt Token/已生成）+ 术语测试不覆盖 system.js | PASS G（+ 测试扩展） |
| UX-1111-007 | 前端 | 数据质量卡 DB 状态显示英文 raw 值 | PASS G |
| UX-1111-008 | 前端 | 设置页传感器"加载中…"占位可永久残留 | PASS G |
| SEC-1111-003 | 构建 | build.bat 缺硬件桥 --add-data（"同一套 flags"注释失实） | PASS G |
| SEC-1111-004 | 文档 | NOTICES 列 pefile/xlrd 但 CI 不装（"残留登记"） | PASS G |
| DOC-1111-008 | 文档 | RELEASE.md 版本示例停 1.0.0 → 参数化 | PASS G |
| DOC-1111-009 | 文档 | PERFORMANCE.md 基线 0.13.1/0.14.0 → 补 1.1 复测节 | PASS G |
| DOC-1111-010 | 文档 | STORAGE_ESTIMATE 缺 system_samples/system_daily 行 | PASS G |
| DOC-1111-011 | 文档 | CHANGELOG 487 vs `def test_` 计数 488 出入 | PASS H（以实际输出为准） |
| DOC-1111-012 | 文档 | UI_TERMINOLOGY 无 1.1 新词节 | PASS G |
| SEC-1111-006 | 卫生 | LlamaMonitor.spec tracked 但被 ignore + 无脚本引用 | PASS G |
| SEC-1111-007 | 卫生 | release.yml 注释举例停 0.13.1 | PASS G |
| SEC-1111-008 | 卫生 | data/ 未 gitignore；根 monitor.db 0 字节 tracked | PASS G |
| UI-1111-010~014 | 前端 | aria 英文 / 内联样式 / #fff 硬编码 / 死 token / 冗余 tag | PASS G |
| GAP-004/008/010/011 | 测试 | slot stale 语义 / system retention / position "10" 排序 / 备份并发 | 选做（GAP-004 随 BUG-010 修） |
| DATA-1111-014 | 数据 | 30d cutoff 86400 秒算术 DST 差 1 行（Windows 默认无 DST） | 延后（记录） |
| PERF-1111-008 | 性能 | 高频读端点全表物化（summary/mtp/daily/live/gpu live） | 延后（实测 1s 级，48h 保留规模下可接受；记录） |
| PERF-1111-015 | 性能 | TPS 短请求窗口平均尖峰（设计语义，UI 可注明） | 延后（Known Limitation） |

## Functional Findings（PASS B 后端通读，验证 OK 项）

- **Token 计数器链**：per-counter reset、NaN/Inf/负净化、缺失保持 baseline、
  sample_invalid、缺口四类 reason + possible_token_loss——逻辑自洽。
- **TPS**：0 与 None 在 API 正确区分（无处理→None；有处理 0 token→0.0）。
- **MTP**：动态 position（无 4 位假设）、per-position 独立 reset。
- **llama Runtime**：/health 10s、/slots 2s/10s、/props 600s 节流；白名单严格；
  状态机转换才记事件（唯一问题 = 无 debounce，REL-1111-005）。
- **端点失败隔离**：5 个 llama 端点 + 4 collector 独立任务，/slots 失败不影响
  Token/GPU/System。唯一公共根因 = 共享事件循环（REL-1111-001/PERF 系列）。
- **GPU**：UUID 稳定身份、detected/过滤分离、快慢查询分频合并（无冗余 nvidia-smi）、
  N/A→None 全链、ECC 不支持→整组隐藏。
- **隐私**：chat_template/generation_prompt/messages 不进内存快照/DB/日志/API；
  远程 model_path 只回文件名。
- **能量**：monotonic Δt 积分、长 gap 不积分（CPU+GPU 两侧）、组件合计 API 层相加。
  唯一缺口 = CPU 跨午夜不分割（DATA-1111-006，量级 ≤ 一个 5s 采样段）。
- **SQLite**：WAL + busy_timeout 5000 + 写 3 次退避重试 + 单事务回滚 +
  索引齐全 + 保留清理随写事务。
- **备份**：Online Backup API（WAL 安全）+ quick_check 验证后生效 + 轮转只删 auto。
  缺口 = WAL 模式未测（GAP-003）。
- **更新器**：流式下载 + SHA-256 + Ed25519 验签（解析前）+ .part 全清理 +
  asyncio.Lock 串行。
- **单实例**：Named Mutex（崩溃 OS 自动释放）+ ShowWindow Event。

## Data Findings

- GAP-001（HIGH）：/api/mtp wall local_date 残留——跨午夜 00:00:00→首个采集之间
  显示昨日"今日"，且破坏 FakeClock 测试纪律。修复 = 2 行 + 回归测试。
- DATA-1111-006（MED）：CPU 能耗跨午夜不分割——量级小（≤ 一个采样段）但口径与
  GPU 不一致，1.1.1 对齐。
- DATA-1111-014（LOW 延后）：86400 秒算术 DST 边界（Windows 默认不触发）。
- 缓存复用率双定义（历史 vs 当前请求）：代码无歧义，METRICS_DEFINITIONS 补文档。
- 测试计数：CHANGELOG 记 487，静态 `def test_` 计数 488——PASS H 以实际输出为准。

## UI/UX Findings

见总览前端行 + 附录 B（35 项已验证 OK 清单）。运行时巡检复现：
- UX-1111-001 在线态 ovLastUpdate 恒空 ✓
- 8 页 0 console/0 page/0 network 错误 ✓
- History 页空态正常（巡检脚本双选择器命中同一元素，非 DOM 重复）

## Performance Findings

PASS D 实测（见 Executive Summary）+ 静态 PERF-002/003/004/012/013（to_thread 化）、
PERF-008（延后记录）、PERF-015（Known Limitation：TPS 窗口平均语义）。
DB 增长估算（PERF-1111-016）：48h 保留下 GPU+system 各 ~3.5 万行，daily 1 行/天——
规模合理，保留清理生效。

## Reliability Findings

- REL-1111-001（HIGH）：system 2s 同步 psutil 阻塞事件循环 → to_thread（默认 poll 保持
  2s 不动配置默认值——to_thread 后阻塞消失，频率是产品参数；记录观察）。
- REL-1111-005（MED）：health debounce（2 次连续失败 → unavailable；1 次成功 → ready）。
- REL-1111-009（MED）：bridge reader 心跳超时（3×interval 无输出 → kill+重启）。
- BUG-1111-007（MED）：bridge 孤儿——启动时清扫同签名孤儿进程（Job Object 延后）。
- 线程卫生：4 collector 任务 + backup + update 均有永存 try/except；GAP-006 补
  poller 循环 out()/sleep 保护 + 测试。
- 睡眠/唤醒：monotonic 跳变检测 OK（已验证）。
- 错误处理：403/409/422/400 全覆盖；500 契约无测试（GAP-005 补）。

## Documentation Findings

DOC-001~013 见总览。docs/ 22 文件分类表见附录 A（9 权威 + 14 历史 + 2 截图目录）。

## 延后项（记录不修 + 原因）

1. PERF-1111-008 高频读端点全表物化——实测 P50 <110ms、最大 1s（48h 保留规模），
   to_thread 化 read path 改动面大；48h→更长保留期时再处理。
2. PERF-1111-015 TPS 窗口平均尖峰——per-interval 设计语义，UI tooltip 注明即可
   （Known Limitation）。
3. DATA-1111-014 86400 秒 DST 算术——Windows 默认无 DST，影响 ≤1 行/次。
4. BUG-1111-007 的 Job Object 方案——启动清扫已覆盖主要场景（反复硬杀后下次启动清理）；
   Job Object 是更彻底的方案，留 1.2。
5. nvidia-smi 回退路径硬编码 C:\ 盘符——PATH 查找优先，回退仅兜底。
6. 备份并发双击——状态机幂等，双击 = 两个独立备份文件（无正确性问题）。
7. 脆弱测试（HTML 标记断言 ×9、跨 3 文件重复的 TEXT_A 常量、三重 app bootstrap）
   ——重构收益/风险比低，1.1.1 不动（记录）。

## Known Limitations

- WDDM 下 GPU 进程显存常 null（UI --）；
- wall_power_w 恒 null（无外部功率计）；
- Xeon 8168 本机桥只暴露 load 传感器（温度/功耗 -- = 硬件真实不可用）；
- TPS 为每采集窗口平均（短请求窗口偏低/偏高，设计语义）；
- "本月"= 服务端 local_date（浏览器时区不同步时 BUG-1111-005 修复前差 1 天）；
- 任务结束后最长 ~10s 内 slot 数字字段为上一任务值（BUG-1111-010 修复后 UI 淡化）。

## Fixed Issues（1.1.1 全部修复，按 PASS 分组）

> 版本 `version.py`：1.1.0 → **1.1.1**（PASS I）。PE FileVersion 由 build 脚本
> 转为 1.1.1.0。Schema **保持 5**（1.1.0→1.1.1 直接兼容，无迁移——PASS D 用真实
> 已装库 `%LOCALAPPDATA%\LlamaMonitor\monitor.db` 验证：user_version 仍 5、1.1 表
> 齐全、行数/最新戳不变、quick_check ok）。
> 测试基线 487 → **506 例全绿**（`unittest discover` 实际输出，+19 回归：
> bridge stdout 泵送 4 + poller 循环 3 + WAL 备份 2 + 500 契约 1 + 跨午夜 MTP 1
> + runtime health debounce 1 + CPU 跨午夜 DB 归属 1 + slot 残留语义 1 +
> 前端 HIGH 回归 3：escAttr / MTP poller / lastUpdate）。

### PASS E（HIGH 主体 + 文档 + 测试骨架）

| ID | 文件:行 | 修复 | 回归测试 |
|----|---------|------|----------|
| BUG-1111-002 | app.js:476-483, 518-524, 1154-1158 | 事件表 + 缺口表 `title='…'` 统一过模块级 `escAttr`（转义 5 字符含 `'`→`&#39;`），不再裸拼 reason/detail | `test_escattr_escapes_quotes_for_title_attr` |
| BUG-1111-003 | app.js:1360-1362, 1206-1209 | Performance 页 MTP 趋势图：注册独立 `mtp` poller（`run: refreshMtp`，`visibleOnly:false`），停留/跨页期间周期刷新 | `test_mtp_poller_updates_performance_trend` |
| GAP-001 | server.py:2046/2059 | `/api/mtp` "今日"由 wall `local_date()` 改为 `collector.clock` 的 `local_date`，与兄弟端点同源（跨午夜 desync 修复） | `test_mtp_rollover_across_midnight` |
| PERF-1111-004 | server.py:1588 | `/api/data/quality` 由每轮 `get_gaps(limit=100000)` 全量物化改为 `get_gap_totals` 聚合查询 | `test_quality_endpoint_does_not_scan_all_live_samples` |
| REL-1111-001 | system_collector.py | system 采集器每 2s 同步 psutil 拆分为独立 `_system_periodic` 任务 + `to_thread`，不再阻塞事件循环 | `test_provider_unavailable_keeps_basic`（+ 采集链路既有测试） |
| UX-1111-001 | app.js:42, 50-83 | 在线态 `ovLastUpdate` 显示"最后更新 X 秒前"（`updateLastUpdateText` 每秒刷新，基于 `state.lastUpdateTs`） | `test_online_shows_last_update_age` |
| DOC-1111-001 | README.md:5 | "只读取 /metrics" 改为与 1.1 五端点一致 | （文档） |
| DOC-1111-002 | README.md 设置分区 | 消除两处矛盾 + 补 System 分区说明 | （文档） |
| DOC-1111-005 | docs/API.md | 补全部 1.1 端点（system/runtime/mtp） | （文档） |
| GAP-002 | hardware_sensor_provider.py + test_hardware_sensor_provider.py | bridge `_supervise_loop`/stdout 泵送（崩溃重启 / 非法 JSON 跳过） | `PumpStdoutTests` 3 例 |

### PASS F（MEDIUM 主体 + 测试 + 可靠性）

| ID | 文件:行 | 修复 | 回归测试 |
|----|---------|------|----------|
| DATA-1111-006 | system_collector.py `_split_cpu_energy_across_midnight` | CPU 能耗跨午夜分割（对齐 GPU `_split_energy_across_midnight` 口径） | `test_midnight_split` + `test_midnight_split_db_attribution` |
| REL-1111-005 | llama_runtime_collector.py `_health_fail_count` | health 状态机 2 次连续失败才翻 Unavailable（单次抖动不翻转、不发事件） | `test_health_single_transient_fail_does_not_flip` |
| PERF-1111-002 | server.py | `/api/data/check-database` 同步 `quick_check` → `quick_check_threadsafe`（to_thread） | `test_check_database_endpoint` |
| PERF-1111-003 | server.py | `/api/system/inventory?manual` 同步 CIM → to_thread + 节流 | （system API 既有测试） |
| PERF-1111-012 | server.py / db.py | clear-live 的 `VACUUM` 同步 → `vacuum_threadsafe` | `test_clear_live_deletes_only_live` |
| PERF-1111-013 | server.py | CSV 导出整文件构建移出事件循环（to_thread） | `test_csv_safe_text_formula_injection` |
| BUG-1111-004 | formatters.js `compact()` | Token 计数格式化精度统一（卡片/图表一致，去恒 2 位小数） | （格式化既有测试） |
| BUG-1111-005 | server.py / app.js | "本月"图表/表格改服务端 `?month=true` 过滤（本机自然月，非浏览器时区） | `test_daily_month_filter_server_side` |
| UX-1111-006 | app.js:1112-1118 | 事件"查看更多"被轮询全量重建时保留焦点（焦点元素记录 + 重建后恢复） | （前端行为，见附录 B 22 项） |
| BUG-1111-007 | hardware_sensor_provider.py `sweep_orphans()` | 启动时清扫同签名孤儿 `HardwareSensorBridge.exe`（Job Object 延后 1.2） | `SweepOrphansTests.test_non_windows_returns_zero`（+ PASS D 运行时验证） |
| BUG-1111-010 | system.js `renderSlotCard` + pages.css `.slot-stale-note` | Slot 空闲残留值淡化（`idleStale`）+ 注脚"上一次请求的残留"，不当当前状态 | `test_slot_stale_semantics_present` |
| REL-1111-009 | hardware_sensor_provider.py `_pump_stdout`/`_hang_timeout_seconds` | bridge reader 心跳超时（3×interval 无输出 → kill+重启），消除静默卡死 | `PumpStdoutTests.test_hang_returns_true_and_kills`（+ PASS D 运行时：杀 bridge 看退避重启） |
| GAP-003 | test_backup_rotation.py `WalModeBackupTests` | WAL 模式备份一致性/完整性 + 关闭前备份（此前全 `wal=False`） | `test_wal_source_backup_is_consistent_and_complete` + `test_wal_backup_with_pending_write_then_close` |
| GAP-005 | test_api.py | 未处理异常 500 `INTERNAL_ERROR` 契约（不泄 body/traceback） | `test_unhandled_exception_500_contract` |
| GAP-006 | collector.py `run()` + test_collector_poller_loop.py | poller 循环 collect/out/sleep 异常保护（不杀循环）+ 测试 | `test_collect_once_exception_does_not_kill_loop` / `test_out_exception_does_not_kill_loop` / `test_cancel_cleans_up_loop` |
| SEC-1111-001 | .gitignore | `.venv*/` 收编 `.venv-final/`/`.venv-rc/` | （仓库卫生） |
| SEC-1111-002 | git mv（97 PNG） | `_screenshots_16f/`/`docs/screenshots*/` → `artifacts/`；`REAL_SOAK_TEST.md`→`docs/` | （仓库卫生，`git check-ignore` 验证） |
| DOC-1111-003 | README.md | 废弃术语 Busy Slots/Token Max/Draft Sequences 清除 | `test_banned_legacy_terms_gone` |
| DOC-1111-004 | config.example.json | 从 config.py `DEFAULT_CONFIG` 重新生成（消除三处漂移 + 补 system 段） | `test_system_section_default_merge_present_in_default_config` |
| DOC-1111-006 | docs/ARCHITECTURE.md | 更新到 1.1 基线（6 asyncio task + sensors 线程；关闭顺序） | （文档） |
| DOC-1111-007 | docs/METRICS_DEFINITIONS.md | 补 1.1 指标 + gap `reason` 漏 `invalid_metrics` + §8 system/runtime/sensors | （文档） |

### PASS G（LOW/POLISH + 卫生 + 文档）

| ID | 文件 | 修复 | 回归测试 |
|----|------|------|----------|
| BUG-1111-009 | system.js | Slot 卡术语对齐术语表（上下文窗口上限/实际处理 Token 等） | `test_new_terms_present` |
| UX-1111-007 | app.js `DB_STATUS_LABELS`/`dbStatusLabel` | 数据质量卡 DB 状态由英文 raw 值改为中文（健康/警告/降级/不可用） | （前端行为） |
| UX-1111-008 | system.js `refreshSensors` `.catch` | 传感器"加载中"占位不再永久残留（刷新失败写终态文案） | （前端行为） |
| SEC-1111-003 | build.bat | 补 `--add-data` HardwareSensorBridge.exe + DLL（与 build_release.py 一致） | （构建） |
| SEC-1111-004 | THIRD_PARTY_NOTICES.txt | 移除未使用/未导入的 pefile/xlrd"残留登记" | （文档） |
| SEC-1111-006 | — | `LlamaMonitor.spec` 未 track（`git ls-files` 空）——验证解决，无需改动 | — |
| SEC-1111-007 | .github/workflows/release.yml | 注释举例 0.13.1 → 参数化（版本号 = version.py，如 1.1.1） | （文档） |
| SEC-1111-008 | .gitignore / 根 | `data/` 收编；根 `monitor.db`（0 字节）未 track（`monitor.db` 模式匹配）——验证解决 | — |
| DOC-1111-008 | docs/RELEASE.md | 版本示例参数化 `X.Y.Z`（不写死版本号） | （文档） |
| DOC-1111-009 | docs/PERFORMANCE.md | 补 1.1 复测节 + 已知延迟项（PERF-008/015） | （文档） |
| DOC-1111-010 | docs/STORAGE_ESTIMATE.md | 补 system_samples/system_daily 行 + 稳态估算（~22-24 MB） | （文档） |
| DOC-1111-012 | docs/UI_TERMINOLOGY.md | 补 1.1 新词节（§12 Slot/System/Settings） | `test_new_terms_present` |
| DOC-1111-013 | docs/ 16 历史文档 | 各加 historical 头横幅（"1.1.1 起标注"） | （文档，逐一验证存在） |
| UI-1111-010 | static/index.html | aria-label 中文化（"Main navigation"→"主导航"、"Type to confirm"→"输入以确认"） | （前端行为） |
| UI-1111-012 | static/css/pages.css | `.check-chip.on .chip-tick` `#fff` → `var(--accent-contrast)`（去硬编码） | （前端行为） |
| UI-1111-013 | static/css/tokens.css | 移除 6 个死 token（medium/4xl/card-row-gap/content-max-×2/accent-subtle-border） | （前端行为） |
| UI-1111-014 | static/index.html | 删冗余 `#sumRangeLabel` 旁 `.range-tag` span（标题已动态） | （前端行为） |

### PASS H（计数校准 + 全量回归）

- **DOC-1111-011**：CHANGELOG 487 vs 静态 `def test_` 计数出入——以 `unittest discover`
  实际输出为准：**506 例全绿**（487 → 506，+19 回归，见上）。全量
  `python -m unittest discover -s tests -p "test_*.py"` 通过（含 PASS D 后所有
  JS/CSS/test 改动，455s 实测）。

### PASS D（运行时验证，本机实测，全部 PASS）

- **升级兼容**：真实已装库 1.1.0→1.1.1 直接兼容（schema 5 保持、1.1 表齐全、
  数据不变、quick_check ok）。
- **运行时 smoke**：16 个 API 端点 200 + 字段级断言 + `/api/daily?month=true`
  服务端过滤——全 PASS（2-87ms）。
- **故障注入**：杀 llama-server → `server_online` True→False（优雅降级，无 500 级联）；
  重启后 server_online 回 True（恢复）。
- **bridge 看门狗**：杀 HardwareSensorBridge（PID 42468）→ 日志"退出 code=None 退避
  重启" → 新 PID 9328 → sensors 恢复 `available`。
- **单实例**：`Local\LlamaMonitor.SingleInstance` mutex 由已装实例持有（`WaitForSingleObject(0)`
  = WAIT_TIMEOUT）；autostart 注册表值 = `"…LlamaMonitor.exe" --background`。
- **A/B 开销**：llama A/B（监控开 vs 关，各 10 次 150-token 生成）ON 30.08 ms/token
  vs OFF 29.76 ms/token = **+0.32 ms/token（+1.1%）**，在噪声范围内；3s /metrics 轮询
  相对 GPU 受限解码可忽略。
- **更新器**：`compare_versions(1.1.1, 1.1.0) = 1`（patch 升级识别，数值比较非字典序）。
- **睡眠/唤醒**：monotonic 跳变检测 OK（启动即记"系统暂停/睡眠"缺口）。
- **UI 截图交付**：8 页 ×（dark + light）= **16 张** 渲染于 1.1.1 工作树 UI +
  真实数据副本（Online Backup API 一致性拷贝，零写回已装库），归档
  `artifacts/screenshots-1.1.1/`（可复用脚本 `scripts/ui_screenshot_111.js` +
  巡检 `scripts/ui_patrol_111.js`）。

## 附录 A：docs/ 分类表

| 文件 | 分类 | 1.1.1 处置 |
|------|------|-----------|
| UI_TERMINOLOGY.md | 权威 | 补 1.1 新词节（DOC-012） |
| API.md | 权威 | 补 1.1 端点（DOC-005） |
| ARCHITECTURE.md | 权威 | 更新到 1.1（DOC-006） |
| METRICS_DEFINITIONS.md | 权威 | 补 1.1 指标 + gap reason（DOC-007） |
| PERFORMANCE.md | 权威(时点) | 补 1.1 复测节（DOC-009） |
| RELEASE.md | 权威(手册) | 版本参数化（DOC-008） |
| UPDATE_SECURITY.md | 权威 | 保留 |
| STORAGE_ESTIMATE.md | 权威(时点) | 补 system 表（DOC-010） |
| INSTALLER_TEST.md | 手册(1.0 基线) | 保留 |
| AUDIT_BASELINE.md / AUDIT_REPORT.md / DEPENDENCY_AUDIT.md / RC_FINAL_REPORT.md / RC_TEST_REPORT.md / FINAL_RELEASE_BASELINE.md / FINAL_RELEASE_REPORT_1.0.0.md / BUILD_INFO_1.0.0.md / BURNIN_0140.md / RELEASE_NOTES_1.0.0.md / RELEASE_NOTES_1.0.1.md / UI_BUG_AUDIT.md / UI_TEST_MATRIX.md / VISUAL_TEST.md / DESIGN_SYSTEM_0.16.16.md | 历史 | 各加 historical 头（DOC-013，移动留 1.2 评估） |
| docs/screenshots/ (14 PNG) / docs/screenshots-0.16.16/ (8 PNG) | 开发产物 | 移 artifacts/（SEC-002） |

## 附录 B：前端"已验证 OK"35 项清单（前端审计子代理报告第 2 节）

1. 离线保留旧数据 + 不可关闭横幅；2. 中央轮询 LM.poll（去重/in-flight/visibility）；
3. ECharts notMerge 全量 + 单点 symbol + 空态与图不共存；4. 主题 retheme + system
   matchMedia 实时跟随；5. 事件分页 + severity 着色；6. Modal 焦点陷阱/ESC/恢复；
7. Toast 去重 + role=status；8. focus-visible 双主题 + tabular-nums；9. GPU 未监控卡
   chip 标记 + 字段级 -- 空态 + connectNulls:false；10. data-goto 委托 + 远程只读隐藏
   设置编辑；11. 16+ 核 CPU 聚合单行；12. 双端点并行 + 表格固定/弹性列宽 + sticky 表头；
13. 导航 aria-current + compact 记忆；14. info-tip 双触发；15. 16D 宽度策略（2560+→1920）；
16. 术语表全面对齐（唯一偏离 = Slot 卡 BUG-009）；17. 设置 dirty 门控 + inline 校验；
18. 关于页死按钮修复保持；19. 更新流全绑定 + release URL 白名单；20. 系统页 provider
   徽章 + Slot 空态；21. 图表空态文案统一 + dataZoom；22. refresher 全 catch；
23. 时间格式全表统一 + tooltip 精确值；24. 间距/圆角 token 体系（像素级微调除外）；
25. 单一滚动容器；26. 启动预热 refreshDaily；27. api.js 单入口 + AbortController；
28. 缓存复用率分母正确；29. MTP 卡 tooltip 澄清；30. GPU 进程 UUID 映射正确；
31. 摘要 N 天有数据口径透明；32. 远程 loopback 判定正确。
（完整 35 项含逐行引用的全文见前端审计报告，归档于本审计工作记录。）
