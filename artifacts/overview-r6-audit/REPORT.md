# 概览页 · Round-6 产品化验收审计（A–BM，65 项）

- 对象：`/` 应用「概览」页（Sidebar「概览」，`#page-overview`）及其直接依赖（后端只读聚合端点 `/api/overview`、`app.js` Round-6 概览模块、状态聚合、告警/需要关注、实时摘要、统一 Formatter、只读 API 聚合、响应式）。
- 版本：1.1.3（`version.py`，未改动）。
- 验收方式：Microsoft Edge（CDP 端口 9333，tab `40DD…D96`），Desktop 1920×1080 / 平板 988×800 / 移动 390×844 / 小屏 320×568，含 Console / Network / Screenshot + 计算样式栅格探针 + 横向溢出探针。
- 服务：`http://127.0.0.1:8790`（`.venv-final` python server.py），真实库 `%LOCALAPPDATA%\LlamaMonitor\monitor.db`（user_version=6 → `db_status=normal`）。
- 说明：本审计全部条目均为本轮（Round-6）实现/验证内容；「验证」列给出实测证据。截图存于 `artifacts/overview-r6-audit/shots/ov-final-{1920,988,390,320}.png`。本轮前端数据统一改由只读端点 `/api/overview` 驱动（service/usage/inference/system/gpus/integrity/attention 一次取齐、按域隔离渲染），替代原先 6 个端点分别写同一组 `ov*`/`dq*` 元素造成的互相覆写。
- 测试基线：全量 `python -m unittest discover -s tests` → **Ran 576 tests · OK**（0 failures / 0 errors）。

| # | 项目 | 规格要点 | 实现 | 验证（实测证据） |
|---|------|---------|------|------------------|
| A | 现有功能审计·概览页 IA 顺序 | 页头(分域跳转)→服务卡→需要关注→今日用量→推理状态→主机状态→GPU 状态→监测完整性 | `static/index.html` `#page-overview` L120–345 按此顺序排布 8 个 `.section`/`.status-strip` | 1920 全页截图 `ov-final-1920.png` 自上而下顺序与规格一致；各 Section 标题「需要关注/今日用量/推理状态/主机状态/GPU 状态/监测完整性」均在位 |
| B | 页副标题 | 「查看 llama.cpp 服务、用量、推理状态、主机资源与监测完整性。」 | `index.html` L126 `<p class="page-subtitle">查看 llama.cpp 服务、用量、推理状态、主机资源与监测完整性。</p>` | CDP 读 `.page-subtitle` = 该文案（去营销化，无「10 秒看懂」）；`test_mobile_ui.OverviewSubtitleTests.test_subtitle` 通过 |
| C | Page Header 分域跳转 | 右侧 5 链接：查看用量/查看性能/查看系统/查看显卡/查看历史 | `index.html` L128–134 `.ov-page-links` 5 个 `a.link[data-goto]` | CDP 实测 5 个链接存在；点击 `data-goto=usage` 等切换成功；390 移动端 `.ov-page-links{width:100%}` 整行铺满换行 |
| D | 服务卡·无 Section 标题 | 服务卡不带「服务状态」标题；右侧元信息 | `index.html` L139 `.status-strip.card.ov-service-card`（无 sh-title）+ `.ov-meta-grid` | 截图可见服务卡无标题行；右侧 = 上下文窗口/并发 Slot/模态 三项 + 最后更新 |
| E | 服务状态词 | 就绪 / 不可达 / 监测异常（detecting→检测中） | 后端 `service.status`（ready/unreachable/monitoring_error/detecting）；`app.js` 全局 `applyStatus` 驱动徽章（更细粒度含模型加载态） | CDP 读 `#ovServerState` = 「就绪」（当前服务在线）；离线/检测中/监测异常态由 `/api/status` 徽章与「需要关注」覆盖 |
| F | 服务卡地址 | 显示 `127.0.0.1:9091`（去 scheme/path）；/metrics 走 tooltip | `server.py` `_base_address_public`（L454）；前端 `#ovServerUrl` | CDP 读 `#ovServerUrl` = `127.0.0.1:9091`（非 `http://127.0.0.1:9091/metrics`） |
| G | 模型行统一量化形式 | `qwen3.8-27b-medium · Q4_K · 27.32B`（" - " 拆成 " · "） | 前端模型行拼接（quantization 统一 " · " 分隔） | CDP 读 `#ovModelLine` = `qwen3.8-27b-medium · Q4_K · 27.32B` |
| H | 上下文窗口 | 服务卡右侧上下文窗口值（精确整数 tooltip） | `#ovContext` + `renderOvService`（F.formatTokenCount + 千分位 title） | CDP 读 `#ovContext` = `262.14K`（tooltip 精确 262144） |
| I | 并发 Slot | 服务卡右侧并发 Slot 数 | `#ovSlots`（model.total_slots） | CDP 读 `#ovSlots` = `1` |
| J | 模态 | 文本 / 视觉 / 视频 / 音频（多模态 "·" 连接） | `#ovModal` + `renderOvService`（vision/video/audio → 视觉/视频/音频） | CDP 读 `#ovModal` = `视觉 · 视频`（当前模型 vision+video 受支持） |
| K | 最后更新 N 秒前 | 服务卡次要信息「最后更新 X 秒前」；离线/后端不可达时显示说明 | 全局 1s ticker `updateLastUpdateText`（`#ovLastUpdate`），`renderOvService` 不覆写避免冲突 | CDP 实测 `#ovLastUpdate` 由 1s ticker 驱动；在线显示「最后更新：刚刚」，离线/后端不可达分支显示「最后成功采样：…/后端不可达，正在重试...」 |
| L | 需要关注·显示条件 | 仅当存在 item 时显示；0 项整块隐藏 | `#ovAttention[hidden]` + `renderOvAttention`（a.hidden 或空 → box.hidden=true） | CDP 当前有 2 项 → 块显示（`attHidden=false`）；`build_attention_items` 返回 `hidden: total==0` |
| M | 需要关注·排序 | Error > Warning > Info；同级保持传入顺序 | `server.py` `build_attention_items`（L432）`_attention_rank` 稳定排序 | 代码路径 + 单元：severity 序正确；同级保序（各域组装顺序） |
| N | 需要关注·最多 3 + 还有 N 项 | 最多 3 条 + 「还有 N 项 查看监控历史 →」 | `build_attention_items` `shown=items[:3]`、`remaining=max(0,total-3)`；前端 `#ovAttentionMoreText`「还有 N 项」+ `#dqLink`「查看监控历史 →」 | CDP：2 项 → 无「还有」；`remaining` 计算正确（8 项场景=5，代码路径验证）；0 项隐藏 |
| O | 需要关注·来源 | 只消费可靠既有状态（service/gpu/system/integrity），不重新发明健康判断 | `build_attention_items` 仅排序+截断；各域在端点内组装 item | 文档化约束（L434–441）；前端不重算健康，只渲染传入 item |
| P | 需要关注·实测 | 当前可靠问题呈现 | 各域组装 + 渲染 | CDP 实测 2 条 `.ov-attention-item`（`attItems=2`），title/subtitle 中文，无 raw 字段 |
| Q | 今日用量·6 指标 | 总量/实际计算（hero）+ 输入/缓存复用/输出/缓存复用率 | `index.html` 今日卡（2 hero + `.ov-today-breakdown` 4 项）；`renderOvUsage` | CDP 读 `#ovTodayLogical=5.45M`、`#ovTodayCompute=191.87K`、`#ovTodayCacheRate=97.9%`，其余项在位 |
| R | 今日用量·总量/实际计算 tooltip | hero 数字精确整数 tooltip | `setFullTip`（F.formatTokenCountFull） | `#ovTodayLogical`/`#ovTodayCompute` title 为精确整数（CDP 读 title 非缩写） |
| S | 缓存复用率 | rate=cached/(prompt+cached)；分母 0 → `--`（不显示 0%） | `server.py` L2078 `cache_reuse_rate_percent`；前端 `renderOvUsage`（null→F.NA） | CDP 当前 = `97.9%`；分母 0 分支返回 null → 前端 `--`（代码路径验证） |
| T | 今日用量·布局 | Wide 2+4 / 988 2+2×2 / 移动 2+2×2 | `pages.css` `.today-breakdown.ov-today-breakdown`（base 4 / 988→2）复合选择器 | 栅格探针：1920 today=4、988=2、390=2、320=2（`todayRaw` 列数实测） |
| U | 推理状态·6 指标 | Prompt TPS / Decode TPS / 活动请求 / 等待请求 / MTP 接受率 / 当前上下文 | `index.html` 单张合并卡 `.ov-inference-grid`；`renderOvInference` | CDP：`#ovRequestsProcessing=0`、`#ovRequestsDeferred=0`、MTP=59.0%、当前上下文=0/1 Slot，TPS 见 X/Y |
| V | 推理状态·布局 | 6 列 / 3×2 / 2×3 | `pages.css` `.stat-grid.ov-inference-grid`（base 6 / 988→3）+ `mobile.css`（≤760→2） | 栅格探针：1920=6、988=3、390=2、320=2 |
| W | 当前上下文 | §91 优先级：单活跃 Slot→used/limit；否则 Busy/总 Slot；否则省略 | `server.py` L2122–2140（n_prompt_tokens+n_decoded，不用 n_prompt_tokens_processed）；前端 `renderOvInference` usage/busy 分支 | CDP 当前 = `0 / 1 Slot`（busy 分支，1 slot 未处理）；usage 分支（单活跃 Slot used/limit + 使用率 tooltip）代码路径验证 |
| X | TPS·空闲 | 指标受支持但本轮无 delta → `0 t/s` + 副标「当前无对应推理活动」 | `server.py` `_overview_tps_display`（L472）idle 分支；前端 `_ovTps` idle→"0 t/s"+tip | CDP 当前 `#ovPromptTps=0 t/s`、`#ovDecodeTps=0 t/s`（idle，副标显示）；未混入 `--` |
| Y | TPS·不可用 | 离线 / 指标不受支持 → `--`（绝不当 0） | `_overview_tps_display` unavailable 分支（value None）；前端 unavailable→F.NA+dim | 离线场景 → `--`（dim）；在线无 counter → `--`（代码路径）；与 idle 的 `0 t/s` 严格区分 |
| Z | TPS·活动 | value 非 None → `N tok/s` | `_overview_tps_display` active 分支；前端 active→formatTps+" tok/s" | 有 delta 时显示 `N tok/s`（代码路径 + 无活动实测走 idle） |
| AA | MTP 接受率·未启用 | 未启用 → 「未启用」 | `server.py` mtp_state="disabled"；前端 dim | 未启用场景显示「未启用」（代码路径） |
| AB | MTP 接受率·暂无数据 | 启用但无 draft 样本 → 「暂无数据」 | mtp_state="no_data"（draft<=0）；前端 dim | 启用无样本场景显示「暂无数据」（代码路径） |
| AC | MTP 接受率·百分比 | 有样本 → 接受率百分比 | mtp_state="value"，accept_rate=accepted/draft*100 | CDP 当前 `#ovMtpRate=59.0%`（今日 draft 有样本） |
| AD | MTP 接受率·服务不可达 | 离线 → `--` | mtp_state="unavailable"（not online）；前端 `--`+dim | 离线场景显示 `--`（代码路径） |
| AE | 主机状态·6 指标 | CPU / 内存 / 磁盘 I/O / 网络 / 监测组件功耗 / 系统运行时间 | `index.html` `.ov-host-grid`；`renderOvSystem`（与 /api/system/status 同源，系统页 sys* 元素独立） | CDP：`#ovHostCpu=5%`、`#ovHostMem=50%`、`#ovHostPower=41 W`、`#ovHostUptime=1天13小时`，磁盘/网络双行在位 |
| AF | 主机状态·布局 | 6 / 3×2 / 2×3 | `pages.css` `.stat-grid.ov-host-grid`（base 6 / 988→3）+ `mobile.css`（≤760→2） | 栅格探针：1920=6、988=3、390=2、320=2 |
| AG | 主机状态·内存 | 主值百分比 + 次值 `122.7/255.7 GiB`（不 ellipsis） | `renderOvSystem`（formatMemory used/total）；`.stat-value{white-space:nowrap}` | CDP 内存主值 50%，次值 used/total GiB 完整（无省略号） |
| AH | 监测组件功耗·部分 | 部分组件可读 → 总值 + 「N 个组件可读取」 | `renderOvSystem` power 分支（components_present + 缺失组件标注） | CDP `#ovHostPower=41 W`（次值标注组件数）；缺失组件 →「…不可读（总值非整机）」（代码路径） |
| AI | 监测组件功耗·全不可用 | 全不可读 → 「不可用」（绝不 0W / `--`） | `renderOvSystem`（monitored_components_w None → "不可用"+dim） | 全不可用场景显示「不可用」（非 0W/`--`，代码路径） |
| AJ | 系统运行时间 | 新增 `#ovHostUptime` | `index.html` 主机卡末项 + `renderOvSystem`（F.formatDuration(uptime_seconds)） | CDP `#ovHostUptime=1天13小时`（中文单位） |
| AK | 无 CPU 频率 | 主机状态不含 CPU 频率项 | `index.html` 主机 6 项无频率（系统页保留） | 概览主机卡仅 6 项（无「CPU 频率」），系统页 sysCpuFreq 独立 |
| AL | GPU 状态·卡头 | 「● 2 张 GPU」；每卡短名 + 全名 tooltip | `renderOvGpu`（ui.setStatusBadge online count「N 张 GPU」）+ `gpuShortName` | CDP `#ovGpuState=2 张 GPU`、`#ovGpuMini .gpu-mini`×2（`gpuCards=2`）；卡名 tooltip=全名 |
| AM | GPU 状态·每卡 4 项 | 仅 利用率/显存占用/温度/功耗（2×2） | `renderOvGpu` items 固定 4 项（.gm-metrics） | 每卡 4 个 `.gm-metric`（截图 ov-final-1920 可见 2×2） |
| AN | GPU 状态·功耗不支持 | 功耗不受支持 → 「不可用」（非 0W / `--`） | `renderOvGpu`（power_draw_w None → "不可用"） | power 不支持场景显示「不可用」（代码路径） |
| AO | GPU 状态·布局 | auto-fit 280–340 左对齐 / 988=2 列 / 移动=1 列 | `pages.css` `.ov-gpu-grid`（auto-fit minmax(280,340) / 988→2）+ `mobile.css .gpu-mini-grid`（≤760→1fr） | 栅格探针：1920 gpu=2、988=2、390=1、320=1；2 张卡 1920/988 两列、移动单列 |
| AP | 监测完整性·5 指标 | 今日采集覆盖率 / 今日缺口 / Token 数据风险 / 数据库状态 / 最后采样 | `index.html` `.stat-grid.ov-integrity-grid` 5 项；`renderOvIntegrity` | CDP：`#dqCoverage=99.0%`、`#dqGapsToday=55 个`、`#dqTokenRisk`、`#dqDb=正常`、`#dqLastSample=刚刚` |
| AQ | 监测完整性·布局 | Wide 5 / 988 3+2 / 移动 2（末项跨 2） | `pages.css` `.stat-grid.ov-integrity-grid`（base 5 / 988→3）+ `mobile.css .dq-summary .stat-grid.ov-integrity-grid`（≤760→2，末项 span 2） | 栅格探针：1920=5、988=3、390=2、360=2、320=2（末项跨 2，`integrityComputed` 列数实测） |
| AR | 监测完整性·覆盖率色 | ≥99.9 ok / ≥95 warn / 否则 bad | `renderOvIntegrity`（cov 阈值赋 ok/warn/bad） | CDP 99.0% → warn 色（<99.9 阈值），语义色正确 |
| AS | 数据库状态·4 态 | 正常 / 只读兼容模式 / 只读 / 异常 + 次值 | `renderOvIntegrity` db 映射 + `#dqDbHint` | CDP 当前库 v6 → `#dqDb=正常`、`#dqDbHint=WAL 已启用`（normal） |
| AT | 只读兼容模式文案 | 「只读兼容模式」+「Schema v{db} · 当前版本支持至 v{CURRENT_SCHEMA_VERSION}」（需关注 + 监测完整性双处，不出现 raw 英文） | 后端 `db_status=readonly_compat` + schema 版本对照；前端 attention + integrity 双处 | 当前库 normal 隐藏横幅；readonly_compat 场景双处中文 + Schema 版本对照（代码路径 + 单元 `test_newer_schema_guard`） |
| AU | 今日缺口·次值 | 「无已知 Token 丢失」/「N 个可能存在 Token 丢失」 | `renderOvIntegrity`（gc 分支）+ `#dqLossToday` | CDP 55 个缺口 → `#dqLossToday=可能存在 Token 丢失`；0 缺口 →「无已知 Token 丢失」（代码路径） |
| AV | Token 数据风险·3 态 | 无已知风险 / 可能丢失 / 时间归属不确定 | `renderOvIntegrity`（token_risk lost/time_uncertain/none）+ data-tone | CDP `#dqTokenRisk` 三态之一（tone 配色 ok/warn/bad 正确） |
| AW | 最后采样 | 「刚刚」+ HH:MM:SS 次值 | `renderOvIntegrity`（F.formatAgo + `#dqLastSampleTs` F.formatTime） | CDP `#dqLastSample=刚刚`（实时采样活跃）+ 次值 HH:MM:SS |
| AX | 只读聚合端点 | `/api/overview` 只读（仅返回摘要，不写库） | `server.py` L2022 `@app.get("/api/overview")`（单 GET，无副作用） | CDP Network：整页加载 `/api/overview` 命中 1 次（`ovr_requests=1`），只读 GET |
| AY | 端点域隔离 | 单域不可用只影响该 Section，不拖垮整页 | 端点内各域 try/独立组装；前端 `renderOverview` 按域分别 render | 各域缺失只影响对应 Section（代码路径）；前端 catch 保留上次值不清屏 |
| AZ | 前端竞态保护 | gen 自增，响应回来 gen 已变则丢弃（慢响应不覆盖新数据） | `app.js` `refreshOverview`（ovGen 守卫） | 快速轮询无交叉渲染；最终态与最后响应一致（代码路径 + 20s 轮询观察无闪烁） |
| BA | loading 语义 | 首轮占位 `--`（非 0）；后续失败保留上次值 | `renderOverview`（null→F.NA）+ `ovLoaded` 门控 | 首轮未取到数据时各指标 `--`；失败不清屏（代码路径） |
| BB | 刷新节奏 | 前台 ~5s、hidden 页停跑/降频（采集器继续） | `app.js` `LM.poll.register("overview",{intervalMs:R,visibleOnly:true})`（仅 overview 页跑） | Network：overview 页 5s 节奏刷新；切走 overview 后 `/api/overview` 停止（采集器后端继续） |
| BC | 离线语义 | 离线 → usage 仍 DB 数据，inference 服务不可达（非 0 t/s） | 端点 online 判定；inference offline → unavailable | 离线场景：今日用量仍显示 DB 聚合值，TPS 显示 `--`（服务不可达），非 `0 t/s`（代码路径） |
| BD | 不覆写冲突（clobber） | 6 端点不再各写 `ov*`/`dq*` 同一元素 | 本轮将 `ov*`/`dq*`/GPU 概览元素所有权收敛到 `/api/overview`；legacy `renderSummaryCards`/`renderDataQuality`/`system.refreshStatus`/`renderGpuOverviewSummary`/`renderOvMtpRate` 在 `ovLoaded` 后不再覆写（仅首载前 bootstrap） | CDP 各元素值稳定来自 overview 源（无抖动/跳字）；`ovLoaded` 门控后 legacy 写路径不触发（代码路径） |
| BE | 统一 Formatter | 数字走统一 F.*（formatTokenCount/formatPercent/formatTps/formatDuration/formatAgo/formatMemory） | `static/js/formatters.js` F.* 全量复用 | 各指标格式统一（token K/M/B、百分比、tok/s、中文时长、相对时间、GiB），无裸数字/裸单位 |
| BF | 响应式·桌面 1920 | 无 app 级横滚 | `pages.css` 栅格 | 1920 实测无横滚；5 栅格列数 4/6/6/2/5 正确（截图 ov-final-1920.png） |
| BG | 响应式·平板 988 | 无横滚 | 同上 | 988 无横滚；列数 2/3/3/2/3（integrity 3+2）正确（ov-final-988.png） |
| BH | 响应式·移动 390 | 无横滚 + Timeline/单列 GPU | 同上 + mobile.css | 溢出探针 390：`scrollW=390=innerW`、`hs=false`、超界元素 0；列数 2/2/2/1/2 正确（ov-final-390.png） |
| BI | 响应式·小屏 320 | 同上 | 同上 | 溢出探针 320：`scrollW=320=innerW`、0 超界；列数 2/2/2/1/2（integrity 2 末项跨 2）正确（ov-final-320.png） |
| BJ | 无横向溢出（320–1920） | 关键数字不 ellipsis（仅模型名/GPU 全名/路径/长错误允许） | `.stat-value{white-space:nowrap;overflow:visible;text-overflow:clip}` + 栅格 minmax(0,1fr) | 390/320 溢出探针 0 超界元素；关键数字完整（次值 used/total GiB 不裁切） |
| BK | Console 零错误 | 无 uncaught / unhandled / RO 噪音 / API 错误噪音 | — | CDP 最终校验：`CONSOLE_WARN_ERR`（error+warning）= 空、`EXCEPTIONS=(none)`；`/api/overview` 无 4xx/5xx 刷屏 |
| BL | Network 无重复风暴 | 无 /props /v1/models /slots /system /gpu /history 重复风暴；overview 只读 | 各 poller visibleOnly + overview 单端点取齐 | CDP Network：overview 页仅 `/api/overview`×1 + 全局 `/api/status`×1；无重复端点风暴 |
| BM | 遗留概览问题（Remaining Overview Issues） | 记录未决项供人工审核 | — | ① 4K（≥2560px）`layout.css` 覆盖 `.content-inner{max-width:1920px}` 仍超出规格 1500–1580px 上限——基础 token `--content-max:1560px` 正确，4K 覆盖为跨页历史行为，需决策是否对概览收敛；② `/api/overview` 后端尚无独立单元/接口测试模块（逻辑经全量 576 用例 + 实测 CDP 覆盖，建议补 `tests/test_api_overview.py` 固化 TPS 5 态 / MTP 4 态 / 缓存率 4 态 / 当前上下文优先级 / 需关注 0/1/3/8 截断 / DB 4 态 / 覆盖率阈值）；③ 本轮改动均未 Commit（工作区待人工审核，含 `server.py`/`app.js`/`system.js`/`index.html`/`pages.css`/`mobile.css`/2 测试文件）。 |

---

## 附：本轮关键实现与验证记录

1. **数据源统一（clobber 根治）**：概览页 7 个 Section 原先由 `/api/status`、`/api/summary`、`/api/data/quality`、`/api/system/status`、`/api/gpu/status`、`/api/mtp` 分别轮询写同一组 `ov*`/`dq*` 元素，互相覆写（今日数字/完整性/GPU 迷你卡/主机摘要各 2–3 处写者）。本轮：
   - 后端 `/api/overview`（`server.py` L2022）一次性只读聚合 service / usage_today / inference / system / gpus / integrity / attention 各域摘要（每域带 `sample_timestamp`/`available`），attention 由 `build_attention_items`（L432）只消费各域可靠状态、仅排序+截断。
   - 前端 `app.js` 新增 Round-6 概览模块（`refreshOverview` + `renderOverview` 按域拆 `renderOvService/Attention/Usage/Inference/System/Gpu/Integrity`），`LM.poll.register("overview",{intervalMs:R,visibleOnly:true})`；`ovLoaded` 门控让 legacy 写者仅在首载前做 bootstrap 填充，首载后不再覆写，杜绝两数据源打架。
2. **TPS 三态 / MTP 四态 / 当前上下文优先级**（`server.py` `_overview_tps_display` L472、L2097–2140）：idle=`0 t/s`+副标「当前无对应推理活动」，unavailable=`--`（dim），active=`N tok/s`；MTP value/disabled/no_data/unavailable；当前上下文单活跃 Slot→used/limit（n_prompt_tokens+n_decoded），否则 Busy/总 Slot，否则省略。前端 `_ovTps`/`renderOvInference` 严格映射，绝不当 0。
3. **响应式栅格确定性修复**：概览各 Section 栅格同时携带共享基础类（`.today-breakdown`/`.stat-grid`/`.host-grid`/`.gpu-mini-grid`），与 `ov-*` 覆盖规则同特异性（0,1,0）下由源序（mobile.css 最晚）决定胜负，导致 988 完整性误 4 列、390 今日语误 3 列、320 完整性误 1 列。本轮把概览栅格选择器升级为复合（`.today-breakdown.ov-today-breakdown` 等，0,2,0）；监测完整性卡在 `.dq-summary` 容器内、受 ≤360 的 `.dq-summary .stat-grid{1fr}`(0,2,0) 压制，进一步用 `.dq-summary .stat-grid.ov-integrity-grid`(0,3,0) 确定性保 2 列。修复后 5 栅格在 1920/988/390/360/320 全部命中规格列数（见 A/T/V/AF/AO/AQ）。
4. **测试对账（7 项陈旧断言修正，非功能回退）**：`test_ui_terminology`/`test_mobile_ui` 中 7 处断言引用的是 Round-4/早期文案（副标题、`主机概况`、`Token 可能缺失`、缺口/事件表旧列名、前端硬编码事件标题）。R5/R6 已把缺口/事件表列改为「时间/持续时间/来源/原因/Token 数据风险」「时间/事件/来源/详情」、事件标题集中到后端 `server.EVENT_PRESENTATION`、概览副标题改为规格规范文案。已将这些断言对齐实现并新增 `test_event_titles_canonical`（守护后端事件标题），全量 **576 tests OK**。
5. **实测证据**：CDP 终态 `ovr_requests=1`、`other_apis=/api/status×1`、`CONSOLE_WARN_ERR` 空、`EXCEPTIONS=(none)`；5 栅格列数（1920/988/390/360/320）与 390/320 横向溢出探针（`hs=false`、0 超界元素）全部通过；截图 `artifacts/overview-r6-audit/shots/ov-final-{1920,988,390,320}.png`。

## 截图清单
- `artifacts/overview-r6-audit/shots/ov-final-1920.png`（桌面 1920×1080）
- `artifacts/overview-r6-audit/shots/ov-final-988.png`（平板 988×800）
- `artifacts/overview-r6-audit/shots/ov-final-390.png`（移动 390×844 @2x）
- `artifacts/overview-r6-audit/shots/ov-final-320.png`（小屏 320×568）
- `artifacts/overview-r6-audit/cdp_verify_final.log`（DOM 值 + Console + Network 终态）
- `artifacts/overview-r6-audit/full_suite_r6c.log`（576 tests OK）
