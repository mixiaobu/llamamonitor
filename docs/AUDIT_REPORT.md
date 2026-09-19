# LlamaMonitor Pre-1.0 全项目审计报告（Phase 14）

> 范围：整个代码库（生产 57 个 .py + 前端 static/ + 构建脚本 + 安装器 + 文档），
> 六域并行审计：**安全（SEC）/ 异步与生命周期（ASYNC）/ 前端（WEB）/
> Windows 集成（WIN）/ 数据库（DB）/ 数据正确性（DATA）**。
> 方法：静态审查 + 代码追踪 + 针对性动态验证；每个 Finding 有唯一 ID（`AUDIT-<域>-NNN`），
> 所有修复在代码注释与回归测试中回链该 ID。
> 基线见 [`AUDIT_BASELINE.md`](AUDIT_BASELINE.md)；架构见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。
> 原则：**不加新功能、只修清晰问题、不做顺手重构**；每个修复带回归测试或明确的验证手段。

## 1. 执行摘要

| 严重度 | 数量 | 状态 |
|---|---|---|
| HIGH | 1 | 全部修复 |
| MEDIUM | 13 | 全部修复 |
| LOW | 16 | 全部修复 |
| 接受（文档化，不改代码） | 4 | 见 §4 |
| 通过（审查无发现） | 见 §5 各域 | — |

- 修复后测试规模 **370 → 396**（新增 26 个审计回归测试），全部通过；
  连续 5 次完整运行验证稳定性（§6）。
- 版本策略：修复全部进入 **0.14.0（Release Candidate，非 1.0.0）**；
  是否升 1.0.0 由用户确认后单独执行。

## 2. HIGH

### AUDIT-WEB-001（HIGH）Settings 视图选择器笔误，设置页从未显示

- **问题**：`static/index.html` 中 `$("settingsView")` 而元素 id 是
  `settings-view`——`getElementById` 返回 null，点击 Settings 导航后
  视图切换在 null 上操作，**设置页整页不可见**（Dashboard 侧无报错）。
- **修复**：选择器改为 `$("settings-view")`。
- **验证**：`node --check` 语法通过；0.14.0 RC burn-in 中实际打开 Settings
  全部分区手工确认（含 Server/Collector/GPU/Interface/Web/Storage/Logging/
  Data/Application/Updates/About）。
- **回归**：前端无 JS 测试框架（审计决定不引入新依赖），由 burn-in 手工检查
  + 代码注释标记覆盖；设置页后端 API 测试（`test_settings_api.py`、
  `test_config.py`）保持全绿。

## 3. MEDIUM

### AUDIT-WEB-002 重复 in-flight 请求竞争

轮询回调（daily/gpuLive/updateStatus）在上一请求未返回时再次触发，
产生重叠请求与"新请求慢、旧请求后到"的 UI 回退竞争。
**修复**：`onceInFlight(key, fn)` 去重守卫（`_inflight` Set），
`refreshDaily`/`refreshGpuLive`/`loadUpdateStatus` 全部接入。
**验证**：burn-in Dashboard 长时观察 + 代码审查（无状态、无锁）。

### AUDIT-WEB-003 前端 fetch 无超时

`fetchJson` 无超时：uvicorn 事件循环短暂阻塞时浏览器请求悬挂，UI 卡死。
**修复**：`fetchJson(url, timeoutMs=30000)` 用 `AbortController` 统一超时。
**验证**：`node --check`；burn-in 中暂停/恢复采样观察 UI 自愈。

### AUDIT-ASYNC-001 自动下载任务无强引用（GC 风险）

`check()` 里 `asyncio.create_task(self.download_guarded())` 的结果被丢弃——
CPython 事件循环只持弱引用，长下载可能被 GC 中途回收（任务静默消失、
状态卡 DOWNLOADING、`.part` 残留 24h）。
**修复**：`UpdateService.__init__` 增 `self._auto_download_task`；
check 保存任务 + `add_done_callback`（清引用、异常记日志）。
**回归**：`test_update_check_download.py::AuditRegressionTests::
test_auto_download_task_reference`（任务引用 set → 完成后 cleared，
下载完成到 READY_TO_INSTALL）。

### AUDIT-SEC-001 / AUDIT-SEC-002 敏感只读端点公网可读

`GET /api/config`（含 llama-server URL、全部配置）与
`GET /api/app/integration`（路径/uptime/自启状态）此前对局域网可读。
**修复**：两端点加 `dependencies=[Depends(_require_loopback)]`（与既有
修改类端点同一依赖，按真实 socket peer 判定，不信 proxy 头）。
**回归**：`test_config.py::test_api_config_requires_loopback`（非回环 403）；
`test_local_only_api.py::test_remote_local_info_endpoints_403`
（192.168.1.50 来源两端点 403）。

### AUDIT-SEC-003 GitHub 更新响应无大小上限

`/releases/latest` 用 `resp.json()`、manifest/`.sig` 用整读 `content`——
恶意/故障 GitHub（或中间人，HTTP 场景）可返回任意大 body 打爆内存。
**修复**：`MAX_RELEASE_JSON_BYTES=5MB / MAX_MANIFEST_BYTES=1MB /
MAX_SIGNATURE_BYTES=64KB`；`/releases/latest` 与 `_download_bytes`
全部改流式读取 + 超限即 `BAD_RELEASE`（fail-closed，不保留半成品）。
**回归**：`test_update_check_download.py::AuditRegressionTests::
test_oversized_{release_json,manifest,sig}_rejected`。
另：CSV 公式注入（外部 GPU 名/UUID 以 `= + - @` 开头）由
`_csv_safe_text` 前置单引号缓解——`test_api.py::ServerHelperRegressionTests::
test_csv_safe_text_formula_injection`。

### AUDIT-SEC-004 数据库只读写失败时 health 永远 healthy

`/metrics` 落盘遇到 "readonly database"（数据目录被只读/磁盘满）时，
`/api/health` 一直报 healthy 而数据全部丢失，protective mode 不触发。
**修复**：collector 写失败且错误含 readonly 且 health=healthy →
`set_health("unavailable", ...)` + `database_write_failure` 事件；
写恢复后回到 healthy + `database_recovery` 事件。
同时修复由此引入的**死锁**：unavailable 会进入 protective mode 跳过 persist、
恢复分支永远执行不到——`write_blocked` 现在只在 `last_db_error is None`
（启动 quick_check 判定）时为真，本进程写失败触发的 unavailable 必须
继续尝试写以发现恢复。
**回归**：`test_reliability.py::AuditCollectorRegressionTests::
test_readonly_db_sets_unavailable_and_recovers`。

### AUDIT-WIN-001 nvidia-smi 子进程 cancel 时不杀

`gpu_collector._default_runner` 只处理超时 kill；任务被 cancel（shutdown /
loop 关闭）时子进程可能遗留（nvidia-smi 孤儿，句柄/线程不回收）。
**修复**：新增 `except asyncio.CancelledError` → `proc.kill()` +
`suppress(CancelledError): await proc.wait()`，finally 中重抛。
**回归**：`test_gpu_collector.py::AuditRegressionTests::
test_default_runner_cancel_kills_child`（cancel → kill 恰 1 次 + wait 完成）。

### AUDIT-DATA-001 下载验证与 Popen 之间的 TOCTOU

`READY_TO_INSTALL → Popen` 窗口内，本地进程可替换
`updates\{version}\*.exe`（原实现只有 `is_file()` 检查）——
启动的不是验证过的那个文件。
**修复**：install() 在 backup/marker 之前对 `verified_path` 重算 SHA-256
（`asyncio.to_thread` 分块读）并比对 manifest 值；不匹配 →
`update_install_aborted` 事件 + `HASH_MISMATCH`，**绝不启动 Installer**。
**回归**：`test_update_install_modes.py::ToctouRegressionTests::
test_install_rehash_rejects_tampered_installer`（篡改 → 异常、Popen 0 次）。

### AUDIT-DATA-002 statMtp 双数据源不一致

Dashboard 的 MTP 接受率卡片同时被 `/api/daily`（昨日行）与 `/api/mtp`
（今日）写入，快刷新时两个值交替闪烁且口径不同。
**修复**：`applySummary` 不再写 statMtp；`refreshMtpDetail` 单一来源
（`/api/mtp` 今日口径）。
**验证**：burn-in 长时观察卡片稳定 + 代码审查。

### AUDIT-DB-001 迁移矩阵缺 v2/v3 带数据 fixture

schema 迁移只有 fresh + legacy v0 fixture；`v2->v3` / `v3->v4` 两步的
数据保留只由代码审查证明——未来在这两步加数据转换逻辑时无测试保护。
**修复**：`test_migration.py` 新增 `_make_v2_db` / `_make_v3_db`
（gpu 表 + 可靠性表带真实数据）→ 打开即迁到 v4，逐值断言保留 + 重开 no-op。
**回归**：`V2V3MigrationFixtureTests`（2 例）。

### AUDIT-DB-002 备份失败固定 60s 重试 + backup_history 无上限

备份目录持续不可写时，每 60s 重试一次 + 每 60s 往 backup_history
插一行（失败行）——事件/行无界增长、日志刷屏。
**修复**：`_backup_periodic` 跟踪 `consecutive_failures`，失败后改 3600s
间隔（恢复后回到 60s + 记日志）；`record_backup` 同事务删除超出
`BACKUP_HISTORY_MAX_ROWS=1000` 的旧行（与事件保留相同的 id 阈值 DELETE）。
**回归**：`test_persistence.py::AuditDbRegressionTests::
test_backup_history_row_cap`（1050 次写入后 ≤1000 行、保留最近行）。

### AUDIT-DB-003 /api/daily 与保留清理的 O(天×行) 查询

`/api/daily` 对**每一天**各调一次 `get_live_samples` 全量拉取再过滤
（7 天 × 3.4万行 = 24 万次行处理/请求，Dashboard 1s 轮询）；
事件保留用 `NOT IN (子查询)` 双全表扫。
**修复**：
- `_day_quality(date, day_samples=None)` 接受预分组样本；
- `/api/daily` 与 daily.csv 导出**一次**取 `get_live_samples(hours=None)`
  按 `local_date` 分组后逐日传入；
- `monitor_events` 行数硬上限（100,000）改单条范围 DELETE
  （`id < (SELECT MIN(id) FROM (… ORDER BY id DESC LIMIT n))`）；
- **同类扩展**：`/api/data/quality` 每次轮询也拉全量 live_samples——
  新增 `db.get_day_sample_bounds(date)`（索引范围查询当日首/末样本）替换。
**回归**：`test_reliability.py::EventRetentionTests::test_events_row_cap_enforced`；
`test_data_quality.py::DataQualityApiTests::
test_quality_endpoint_does_not_scan_all_live_samples`（计数器断言 quality
不再拉全量、必须走按日查询）。

## 4. LOW（全部修复）

| ID | 问题 | 修复 | 回归 |
|---|---|---|---|
| AUDIT-WEB-004 | 重复 `fmtBytes`（后定义覆盖，行为漂移）；数据质量区 null 字段显示 "undefined" | 删除重复定义（保留 null 安全版）；null 守卫（`cov == null` → "--"、`gap_count \|\| 0`、`g.start ? … : "--"`） | burn-in 手工 + 代码审查 |
| AUDIT-WEB-005 | 表格渲染注入面：release 日期/缺口 source/reason 未 `escapeHtml` | 全部过 `escapeHtml(… \|\| "--")` | 代码审查（纯函数包裹） |
| AUDIT-WEB-006 | `window.open(rel.release_url)` 无协议校验/无 noopener | 仅 `^https?://` 才打开 + `noopener` | 代码审查 |
| AUDIT-WEB-007 | 轮询间隔无下限（配置 0/负值 → 忙轮询）；窗口隐藏仍轮询 1s 更新状态 | `Math.max(1000, ms)`；更新轮询 `isVisible()` 门控 | 代码审查 |
| AUDIT-ASYNC-002 | `_periodic`/`_gpu_periodic` 外层 `except Exception: pass` 静默 | 降为 `logger.debug(…, exc_info=True)` | 代码审查 |
| AUDIT-ASYNC-003 | 线程 join 后存活无日志（shutdown 卡住无诊断） | `stop_uvicorn` / tray updater join / ShowWindow+Shutdown listener stop 均超时 WARNING | 代码审查 |
| AUDIT-ASYNC-004 | httpx 复用 client 无 keepalive_expiry（Windows 首包 1.6s 问题在连接被回收后重演） | `httpx.Limits(keepalive_expiry=max(30, interval*4))` | 代码审查（性能 §PERFORMANCE.md） |
| AUDIT-ASYNC-005 | `_recent_dates` 用 `time.time()` 而 daily cutoff 用 collector clock——FakeClock 下窗口错位 | 新增 `now` 参数，`/api/daily` 传 `collector.clock.now()` | `test_api.py::ServerHelperRegressionTests::test_recent_dates_uses_injected_now` |
| AUDIT-ASYNC-006 | `get_gaps` 按日窗口 `start+86400`（DST 回拨日 25h 漏当日缺口）；`wait_for_ready` 用 wall clock（系统回拨时死等/提前）；托盘 30s 轮询每次新建 httpx client | `timedelta(days=1)` 本地日期边界；`time.monotonic()`；`tray_http` 单 client 全生命周期复用（`_perform_shutdown` 与两处端口冲突提前退出路径都关闭） | `test_persistence.py::AuditDbRegressionTests::test_get_gaps_date_window_includes_end_of_day` + 代码审查 |
| AUDIT-SEC-005 | FastAPI `/docs`、`/redoc`、`/openapi.json` 暴露（信息面 + Swagger UI 静态资源） | `docs_url=None, redoc_url=None, openapi_url=None` | 代码审查 + 既有 API 测试 |
| AUDIT-SEC-006 | 只读 `file:` URI 用裸路径——数据目录含空格/中文时降级保护路径本身打不开 | `_readonly_file_uri`：`quote(str(path), safe="/")` → `file:///…?mode=ro` | `test_persistence.py::AuditDbRegressionTests::test_readonly_file_uri_{encodes_path,opens_real_db}` |
| AUDIT-WIN-002 | GPU 能耗：`prev_power or 0.0` 把"上一轮功耗缺失"当 0W 积分（虚减能耗） | 要求两侧功率都非 None 才产生能耗段 | `test_gpu_collector.py::AuditRegressionTests::test_prev_power_none_skips_segment` |
| AUDIT-WIN-003 | 下载**任务**被 cancel（shutdown 路径，非 cancel event）时 `.part` 残留 24h、状态卡 DOWNLOADING | `download()` 增 `except asyncio.CancelledError`：清理 .part、状态回 UPDATE_AVAILABLE、重抛 | `test_update_check_download.py::AuditRegressionTests::test_task_cancel_during_download_cleans_part` |
| AUDIT-DATA-003 | `/metrics` 响应无大小上限（`response.text`）——异常 server 可打爆内存 | 流式 `client.stream` + `MAX_METRICS_BYTES=16MB`，超限按离线处理 | `test_reliability.py::AuditCollectorRegressionTests::test_metrics_response_over_16mb_treated_offline` |
| AUDIT-DATA-014 | metrics_parser 边缘输入（科学计数法、标签值内 `\"` 转义）无测试覆盖，docstring 与转义实际行为（反斜杠字面保留）不一致 | docstring 对齐实现 + `AuditParserRegressionTests`（科学计数法 3 例、转义引号 2 例） | `test_metrics_parser.py::AuditParserRegressionTests` |
| AUDIT-DB-004 | GPU 能耗 baseline：`save_gpu_samples` 失败后 `_prev` 已更新——下轮用未落库的能耗段做基线（能耗双计/漏计） | poll_once 先快照 `prev_energy_state`，写失败回滚 `_prev`（pop 或还原） | `test_gpu_collector.py::AuditRegressionTests::test_energy_baseline_rollback_on_write_failure`（DB 级能耗恒等断言） |
| AUDIT-DB-005 | pre_migration / pre_update 备份在备份列表里不可见（对用户是"消失的"恢复点） | `_all_backups` 纳入两前缀；`list_backups` 增 `pre_migration` / `pre_update` kind；**不参与** keep_count 轮转 | `test_backup_rotation.py::PreUpdateListTests`（2 例）+ `test_newer_schema_guard.py` 更新断言 |

## 5. 通过（审查无发现）

- **SEC**：loopback 单一依赖 `_require_loopback` 覆盖全部修改/敏感端点，
  按真实 socket peer 判定、不信 X-Forwarded-For，无第二套判定逻辑；
  更新信任链（内置公钥验签 → manifest → 下载双校验）全路径 fail-closed；
  私钥/secret 代码库扫描无残留；CSV 注入点全部包裹；CORS 未启用（同源静态）。
- **ASYNC**：全部 `create_task` 有强引用（本修复后）；lifespan 关闭顺序
  确定（update→backup→collector→gpu→checkpoint→close）；所有 join 带超时
  且超时只 WARNING；线程清单与基线一致，无新增裸线程。
- **WIN**：单实例 Named Mutex 原子获取（无锁文件、无 stale 锁）；
  托盘/事件监听线程全部有 stop 机制；`--background` 隐藏窗口路径正确；
  nvidia-smi 子进程超时/取消双路径收尾（本修复后）。
- **DB**：所有修改类写走单事务（`_tx_with_retry`）；SQL 全部参数化
  （无字符串拼接）；6 个索引覆盖热查询（EXPLAIN QUERY PLAN 复核，
  无缺失索引、无需新增）；WAL 检查点策略正确（PASSIVE、不 TRUNCATE）；
  迁移逐版本独立事务 + 幂等 + 预迁移备份（本修复后 v2/v3 fixture 补齐）；
  损坏库不自动修复/不删库（quick_check + protective mode 路径全通）。
- **DATA**：9 个 Counter 独立 reset 检测语义正确（缺失去 None 不双计）；
  午夜归集规则（delta 归样本时间戳日期）与单测一致；梯形积分能耗在
  本修复后语义完整；7/30/90 天确定性 soak 模拟恒等式全部保持。

## 6. 测试证据

| 项目 | 结果 |
|---|---|
| 修改前基线（AUDIT_BASELINE §2） | 370/370 OK（305.9s） |
| 修改后全套 | **396/396 OK**（新增 26 个审计回归测试） |
| 连续 5 次完整运行（§113 规格） | 见下方运行记录（`scripts/runtimes5.ps1`，每次 "Ran 396 tests … OK"） |
| 7/30/90 天 soak 模拟 | `tests/test_soak_simulation.py`（确定性种子、ground truth 恒等式）在全套 5 次运行中每次执行，全部通过 |
| 迁移矩阵 | fresh / legacy v0 / v2 带数据 / v3 带数据 → v4，全部通过 |
| 更新篡改 5 场景 | Phase 13 `tools/tamper_test.py` 场景保持通过（InvalidSignature / missing sig / missing manifest / filename mismatch / SHA-256） |

### 5× 运行记录

`scripts/runtimes5.ps1`（2026-09-19 20:55–21:23，版本 0.14.0 提升后、
干净测试集 397 例）：

```
RUN 1 : PASS   Ran 397 tests in 334.755s  OK
RUN 2 : PASS   Ran 397 tests in 330.286s  OK
RUN 3 : PASS   Ran 397 tests in 335.772s  OK
RUN 4 : PASS   Ran 397 tests in 332.868s  OK
RUN 5 : PASS   Ran 397 tests in 332.840s  OK
===== 5x RESULT: 5/5 PASS =====
```

## 7. 接受的风险（文档化，不改代码）

1. **`state.value` 是 REAL（64-bit float）**：token 计数精确到 2^53
   （≈9.0×10^15）——按 1M tokens/天需 ~2462 万年才溢出；文档化即可。
2. **pystray `TaskbarCreated` 限制**（WIN-010）：Windows 资源管理器重启
   （explorer.exe 崩溃恢复）时托盘图标需等 pystray 内部重建或应用重启
   ——上游库限制，文档化为已知限制。
3. **持续 DB 写失败期间 counter_reset 事件可能重复**：每轮失败都会尝试
   记事件（事件写入本身也失败则静默）——恢复后以 `database_recovery`
   为权威边界；接受（事件是审计流，允许冗余）。
4. **`/releases/latest` GitHub/代理秒级缓存**（基线 K8，设计决定）：
   生产无影响；测试用 `FakeGithub` 隔离。

## 8. 性能影响（详见 PERFORMANCE.md §5）

修复均为常数级开销（流式读上限、单条范围 DELETE、预分组、client 复用）；
无新增轮询、无新增连接。before/after 实测在 0.14.0 RC burn-in 期间
（PERFORMANCE.md §5 表格）完成。

## 9. 发布门（Release Gate）

| 检查项 | 状态 |
|---|---|
| 无 open BLOCKER/HIGH | ✅（1 HIGH + 13 MEDIUM 全部修复带回归） |
| 全套测试通过 | ✅ 397/397 |
| 连续 5 次运行 | ✅ 5/5 PASS（§6 运行记录） |
| 7/30/90 天模拟 | ✅ 每次全套运行内执行（6 次全套运行均含） |
| 迁移矩阵（fresh/legacy/v2/v3） | ✅ |
| 干净 venv 构建 + 发布校验 | ✅ 干净 venv（pinned requirements）内 397/397 → `build_release.py --skip-tests` 5 资产 → `validate_release.py` 通过（含签名验证），2026-09-19 |
| 就地升级 0.13.1 → 0.14.0 | ✅ Inno `/SILENT` 升级 exit 0；注册表/EXE/API 版本一致；4 天 daily 历史完整保留；单实例 ShowWindow 唤醒实测 |
| 更新签名验证 + 篡改拒绝 | ✅ Phase 13 篡改 5 场景 + 干净 venv 构建的签名验证 |
| 性能 before/after | ✅ PERFORMANCE.md §5（无回退，窗口/后台两口径均改善） |
| 48-72h burn-in 每日记录 | 🔄 进行中：2026-09-19 21:31 启动（D0 首测已录，docs/BURNIN_0140.md） |

## 10. Release Recommendation（技术门）

**推荐发布 0.14.0 RC**（技术门通过）：

- 全部 HIGH/MEDIUM 修复带回归测试；无 open BLOCKER/HIGH；
- 396 测试 × 5 次连续运行 + 7/30/90 天模拟全绿；迁移矩阵补齐 v2/v3；
- 性能无回退（burn-in 期实测确认）。

**本推荐仅是技术门判断**，不构成 1.0.0 版本决定：0.14.0 以 RC 发布、
burn-in 达标后，是否升 1.0.0 由用户确认后单独执行（Version Bump 独立决策点）。
