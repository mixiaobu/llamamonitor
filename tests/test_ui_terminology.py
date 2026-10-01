"""
Phase 16E/16F 测试：UI 术语审计（唯一术语字典 docs/UI_TERMINOLOGY.md 的回归防护）。

扫描范围：static/index.html + static/js/*.js（用户可见文案 + 前端常量）。
不扫描后端 Python（API JSON 字段 / DB 列名 / parser keys 保持不变）。

断言：
- 已废弃术语（旧中文机器翻译感文案 / 开发者内部术语）不再出现；
- 新术语（术语字典规定的名称）确实出现；
- 单位统一为 tok/s。

运行：python -m unittest discover -s tests
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"


def _frontend_blob():
    parts = [(STATIC / "index.html").read_text(encoding="utf-8")]
    for p in sorted((STATIC / "js").glob("*.js")):
        parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)


class UiTerminologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blob = _frontend_blob()
        cls.html = (STATIC / "index.html").read_text(encoding="utf-8")

    def test_banned_legacy_terms_gone(self):
        """旧术语必须从前端消失（术语字典「废弃」条目）。"""
        banned = [
            "逻辑 Token",
            "提示 Token",
            "草稿序列",
            "服务器运行时",
            "最大 Token 记录",
            "Busy Slots",
            "API 主机",
            "API 端口",
            "Token 丢失？",
            "Token 丢失?",
            "服务器上线",
            "服务器离线",
            "面板刷新间隔",
            "投机解码",
            "轮询间隔",
            "排队（延迟）",
            "忙碌解码槽",
            # 注：「随 Windows 启动」Round-7 §171 起为正式名称（已移入 required）
            "最新 Release",
            "安装模式",
            "清空实时历史",
            "重置所有统计",
            "硬件趋势",
            "利用率 &amp; 显存",
            "利用率 & 显存",
            "不勾选",
            "实时数据保留",
            "默认历史范围",
            "llamacpp 指标",
            "config.json",
            # Round-7 设置/关于精修：旧设置项名称必须消失
            "指标端点路径",
            "连接超时",
            "监听地址",
            "监听端口",
            "登录时自动启动",
            "实时采样保留时长",
            "实时历史保存间隔",
            "基础采集间隔",
            "GPU 采集间隔",
            "历史保留时长",
            "数据库 Schema 版本",
            "复制版本信息",
            "轮转文件保留数",
            "检查数据库完整性",
            "清除实时采样历史",
            # Round 5 推理性能页：旧术语/错误命名必须消失
            "服务器运行状态",
            "推测验证轮次",
            "上下文高水位",
            "平均忙碌 Slot 数",
            "模型与服务",
            "当前 Slot",
            "忙碌 Slot（平均）",
            "Multi-Token 预测",
            "最近 60 分钟",
        ]
        for term in banned:
            self.assertNotIn(term, self.blob, "已废弃术语仍存在：%s" % term)

    def test_log_backup_count_renamed_in_html(self):
        """日志设置的轮转数量术语统一为「轮转文件数量」（§222 冻结）；
        设置 HTML 中不再出现裸「备份数量」（数据页用「保留数量」）。"""
        self.assertIn("轮转文件数量", self.html)
        self.assertNotIn("备份数量", self.html)

    def test_settings_rail_frozen_categories(self):
        """Round-7 §5/§222：设置内部分类 rail 固定 8 项两字分类，顺序冻结
        连接→采集→外观→显卡→系统→数据→应用→更新（旧名 服务器/GPU/系统监控/
        数据与备份 不得残留为 rail 项）。"""
        html = self.html
        rail = html[html.index('class="settings-rail"'):html.index('class="settings-pane"')]
        cats = re.findall(r'<button class="rail-item"[^>]*data-sec="(\w+)"[^>]*>([^<]+)</button>', rail)
        self.assertEqual([c[1] for c in cats],
                         ["连接", "采集", "外观", "显卡", "系统", "数据", "应用", "更新"],
                         "设置分类 rail 顺序/名称必须冻结")
        self.assertEqual([c[0] for c in cats],
                         ["server", "collector", "appearance", "gpu", "system", "data", "application", "updates"],
                         "设置分类 data-sec key 保持稳定（后端/深链接依赖）")
        for legacy in ["服务器", "GPU", "系统监控", "数据与备份"]:
            for _, label in cats:
                self.assertNotEqual(label, legacy, "rail 残留旧分类名：%s" % legacy)
        # 分类内容标题
        for title in ["llama-server", "指标采集", "GPU 监控", "系统监控", "软件更新"]:
            self.assertIn(">%s</h2>" % title, html, "设置分区缺少内容标题：%s" % title)

    def test_new_terms_present(self):
        """术语字典规定的新名称必须存在（防误删）。"""
        required = [
            "Token 总量",
            "实际计算 Token",
            "输入 Token",
            "缓存复用 Token",
            "输出 Token",
            "缓存复用率",
            "推测解码",
            "Draft Token 接受率",
            # Round 5 推理性能页产品化：4 个旧术语按新页面语义重命名
            "验证步数",          # 原「推测验证轮次」(MTP 指标)
            "上下文窗口上限",     # 原「上下文高水位」并入
            "平均忙碌 Slot / Decode",  # 原「平均忙碌 Slot 数」(含 Decode 语义)
            "处理中请求",
            "等待中请求",
            "Token 吞吐率",
            "运行时状态",          # 原「服务器运行状态」(推理性能页 Section C)
            "监控历史",
            "采集覆盖率",
            "Token 数据风险",          # Round-6：缺口/风险列统一为「Token 数据风险」
            "GPU 监控",
            "GPU 利用率与显存占用",
            "显存占用",
            "功耗与温度",
            # Round-7 设置/关于精修：术语最终冻结（§222）
            "请求超时",
            "Metrics 路径",
            "llama-server 地址",
            "指标采样间隔",
            "短期历史保留",
            "界面刷新间隔",
            "默认统计范围",
            "GPU 采样间隔",
            "监控的 GPU",
            "基础采样间隔",
            "历史写入间隔",
            "高级传感器采样间隔",
            "高级硬件传感器",
            "数据库文件",
            "SQLite WAL",
            "自动备份",
            "备份间隔",
            "保留数量",
            "数据维护",
            "危险操作",
            "清空短期历史",
            "重置统计数据",
            "随 Windows 启动",
            "访问范围",
            "端口",
            "日志级别",
            "日志文件上限",
            "轮转文件数量",
            "打开数据目录",
            "打开日志目录",
            "打开备份目录",
            "退出 LlamaMonitor",
            "软件更新",
            "自动检查更新",
            "检查间隔",
            "自动下载",
            "当前版本",
            "上次检查",
            "最新版本",
            "更新状态",
            "恢复本页默认",
            "有未保存的更改",
            # 关于页
            "数据库架构版本",
            "数据目录",
            "复制诊断信息",
            "项目与支持",
            "工作方式与隐私",
            "llama.cpp 本地只读监控工具",
            # Round-5：事件标题从前端 EVENT_TYPE_LABELS 硬编码改为后端
            # server.EVENT_PRESENTATION 统一来源（display_title），前端不再硬编码
            # "llama-server 已连接 / LlamaMonitor 启动 / 数据库备份完成 / 数据库迁移"，
            # 其规范名称改由 test_event_titles_canonical（后端）守护。
            "tok/s",
            # Round 5 推理性能页新术语（防误删）
            "模型与运行环境",
            "Slot 监控",
            "活跃 Slot",
            "数据新鲜度",
            "llama.cpp 构建",
            "模型文件大小",
            "已启用",
            "当前序列长度",
            "剩余生成预算",
            "新处理 Prompt",
            "仅展示运行元数据",
            "草稿 Token",
        ]
        for term in required:
            self.assertIn(term, self.blob, "新术语缺失：%s" % term)

    def test_event_titles_canonical(self):
        """Round-5：监控事件标题统一由后端 server.EVENT_PRESENTATION 提供
        （/api/events 的 display_title），前端不再硬编码 EVENT_TYPE_LABELS。
        守护规范标题（防误删 / 防回退到前端硬编码旧文案）。"""
        server_py = (ROOT / "server.py").read_text(encoding="utf-8")
        for title in [
            "llama.cpp 服务已恢复",
            "llama.cpp 服务不可达",
            "LlamaMonitor 已启动",
            "LlamaMonitor 已停止",
            "数据库备份完成",
            "数据库结构已升级",
        ]:
            self.assertIn(title, server_py, "后端事件标题缺失：%s" % title)
        # 前端不再硬编码旧事件标题表（已改为后端 display_title）
        self.assertNotIn("EVENT_TYPE_LABELS", self.blob,
                         "前端不应再硬编码 EVENT_TYPE_LABELS（已改后端统一来源）")

    def test_tps_unit_unified(self):
        """TPS 单位统一 tok/s，不得残留 t/s。"""
        self.assertNotIn(' + " t/s"', self.blob)

    def test_slot_stale_semantics_present(self):
        """Round 5 §127-§133：Slot 空闲时 per-request 字段是上一任务残留，
        绝不当"当前"展示——状态=空闲，per-request 数字（上下文/Prompt/新处理/
        缓存复用/已生成/剩余）一律 "--"；仅 MTP（配置值）保留。
        Active 才展示当前请求数据；当前上下文 = n_prompt_tokens + n_decoded
        （不用 n_prompt_tokens_processed 冒充完整上下文长度）。

        断言 system.js 的新语义（缺一即回退到"旧值冒充当前状态"）：
        1. renderSlotsOnly 用 active = !!s.is_processing 判定状态；
        2. _slotContextCell 空闲返回 F.NA，活跃用 n_prompt_tokens + n_decoded；
        3. per-request 单元格空闲走 F.NA（'--'）；
        4. CSS 定义 .slot-table 紧凑 Table（Desktop）+ 状态 pill。"""
        system_js = (STATIC / "js" / "system.js").read_text(encoding="utf-8")
        self.assertIn("renderSlotsOnly", system_js, "缺少 Slot 监控渲染 renderSlotsOnly")
        self.assertIn("active = !!s.is_processing", system_js, "必须用 !!s.is_processing 判定活跃")
        # 当前上下文 = n_prompt + n_decoded（不是 n_prompt_tokens_processed）
        self.assertIn("_slotContextCell", system_js, "缺少当前上下文单元格函数")
        self.assertIn("n_decoded", system_js, "当前上下文必须用 n_decoded 累加")
        # 空闲 per-request 字段走 F.NA（'--'），不当当前状态
        self.assertIn("F.NA", system_js, "空闲 per-request 字段必须 F.NA（--）")
        # CSS：紧凑 Table + 状态 pill + 当前上下文进度
        pages_css = (STATIC / "css" / "pages.css").read_text(encoding="utf-8")
        self.assertIn(".slot-table", pages_css, "CSS 缺少 .slot-table 样式")
        self.assertIn(".slot-state", pages_css, "CSS 缺少 .slot-state 状态 pill")
        self.assertIn(".slot-ctx", pages_css, "CSS 缺少 .slot-ctx 当前上下文列")

    def test_escattr_escapes_quotes_for_title_attr(self):
        """AUDIT-1.1.1 BUG-1111-002：事件/缺口表 title 属性必须过 escAttr，
        且 escAttr 转义单引号 '->&#39;（否则 title='…' 属性被单引号截断，
        tooltip 残缺 / class 被吞）。"""
        app_js = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
        # escAttr 定义存在且覆盖 5 字符（含单引号转义）
        self.assertIn("function escAttr(s)", app_js, "缺少模块级 escAttr")
        self.assertIn(".replace(/'/g, \"&#39;\")", app_js, "escAttr 未转义单引号（BUG-002 回退）")
        self.assertIn(".replace(/\"/g, \"&quot;\")", app_js, "escAttr 未转义双引号")
        # 事件表与缺口表的 title 属性都经 escAttr（不能裸拼 reason/detail）
        # Round-6：缺口表 reason 经 reasonTitle 转义（推定/实据分支），事件表 detail 经 rawDetail||detail。
        self.assertIn("escAttr(reasonTitle)", app_js, "缺口表 title 未过 escAttr")
        self.assertIn("escAttr(rawDetail || detail)", app_js, "事件表 title 未过 escAttr")

    def test_mtp_poller_updates_performance_trend(self):
        """AUDIT-1.1.1 BUG-1111-003：Performance 页 MTP 趋势图必须随轮询更新。
        修复 = 注册独立 mtp poller（run: refreshMtp）且 visibleOnly:false
        ——这样 performance 页停留期间（即使切到别的页）MTP 数据仍周期刷新，
        不再"停留期间/跨天不更新"。"""
        app_js = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn('LM.poll.register("mtp"', app_js, "缺少 mtp 轮询注册（BUG-003 回退）")
        self.assertIn("run: refreshMtp", app_js, "mtp poller 未绑定 refreshMtp")
        # visibleOnly:false = 后台也轮询（趋势图跨页/停留期间持续更新）
        self.assertIn("visibleOnly: false, run: refreshMtp", app_js,
                      "mtp poller 必须 visibleOnly:false（否则停留页不刷新）")
        # refreshMtp 自身从 /api/mtp 拉数据
        self.assertIn('return api.get("/api/mtp")', app_js, "refreshMtp 未拉 /api/mtp")

    def test_online_shows_last_update_age(self):
        """AUDIT-1.1.1 UX-1111-001：在线态必须显示"最后更新 X 秒前"（此前恒空，
        数据新鲜度不可见）。修复 = updateLastUpdateText 每秒刷新，用
        state.lastUpdateTs 计算 elapsed 秒并写入 ovLastUpdate。"""
        app_js = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("updateLastUpdateText", app_js, "缺少 updateLastUpdateText")
        self.assertIn("setInterval(updateLastUpdateText, 1000)", app_js,
                      "lastUpdate 未每秒刷新")
        self.assertIn("ovLastUpdate", app_js, "未写入 ovLastUpdate 元素")
        self.assertIn("最后更新", app_js, "缺少'最后更新'文案")

    # ---------- 1.1.4 精修 Round 2：主导航两字 + 顺序 ----------

    def test_sidebar_nav_two_char_in_order(self):
        """§2/§95/§96：Desktop 主导航必须真正渲染两字标签，顺序
        概览→用量→性能→系统→显卡→历史（1.1.4 Round 4：桌面侧栏 系统 先于 显卡；
        Mobile 底栏保持 显卡 先于 系统，见 test_mobile_nav_labels_in_order）。"""
        html = self.html
        nav = ["overview", "usage", "performance", "system", "gpu", "history"]
        # 只取 Desktop navview（mobile-nav 用 .mnav-item 另一套，单独断言）
        navview = html[:html.index('class="mobile-nav"')]
        seq = re.findall(r'data-page="(%s)"' % "|".join(nav), navview)
        # 标签：主导航 nav-label（仅取上面 6 页；设置/关于在底部单独断言）
        labels = [l.strip() for l in re.findall(r'<span class="nav-label">([^<]+)</span>', navview)]
        labels = labels[:len(seq)]
        self.assertEqual(seq, nav, "主导航顺序应为 概览/用量/性能/系统/显卡/历史")
        self.assertEqual(labels, ["概览", "用量", "性能", "系统", "显卡", "历史"],
                         "主导航标签必须两字且与顺序一致")
        # 底部：设置 / 关于
        self.assertIn('data-page="settings"', html)
        self.assertIn('data-page="about"', html)
        self.assertIn("设置", html)
        self.assertIn("关于", html)

    def test_sidebar_no_legacy_labels(self):
        """§2：旧导航词不再作为 nav-label 出现（标签必须两字）。"""
        navview = self.html[:self.html.index('class="mobile-nav"')]
        labels = re.findall(r'<span class="nav-label">([^<]+)</span>', navview)
        for legacy in ["Token 用量", "推理性能", "GPU", "监控历史"]:
            self.assertNotIn(legacy, labels, "主导航残留旧词：%s（现有：%s）" % (legacy, labels))

    def test_mobile_nav_labels_in_order(self):
        """§3/§98：Mobile Bottom Nav 顺序 概览→用量→性能→显卡→系统（与 Desktop 前五项一致）。"""
        html = self.html
        start = html.index('class="mobile-nav"')
        mnav = html[start:html.index('class="more-sheet"')]
        seq = re.findall(r'data-page="(overview|usage|performance|gpu|system)"', mnav)
        labels = [l.strip() for l in re.findall(r'<span class="mnav-label">([^<]+)</span>', mnav)]
        self.assertEqual(seq, ["overview", "usage", "performance", "gpu", "system"],
                         "Mobile 导航顺序应为 概览/用量/性能/显卡/系统")
        self.assertEqual(labels, ["概览", "用量", "性能", "显卡", "系统"],
                         "Mobile 导航标签必须两字（显卡 而非 GPU）")

    # ---------- 1.1.4 精修 Round 2：术语 / 文案 / 容器布局 ----------

    def test_overview_new_terms_present(self):
        """§18/§26/§50/§53/§66：Overview 新术语必须存在。"""
        html = self.html
        for term in ["主机状态", "组件功耗", "今日采集缺口", "今日采集覆盖率",
                     "数据库状态", "远程只读 · 管理操作仅限本机", "查看监控历史"]:
            self.assertIn(term, html, "缺少新术语：%s" % term)

    def test_overview_legacy_gap_terms_gone(self):
        """Round-6：监测完整性指标规范命名为「今日缺口」（今日口径），历史范围表用
        「采集缺口」；「今日采集缺口」为二者关联表述（概览今日口径）。
        R4 曾要求概览统一为「今日采集缺口」；R6 规格回归「今日缺口」为概览规范词，
        故本测试改为断言 R6 规范词「今日缺口」存在（而非 R4 的断言其消失）。"""
        html = self.html
        self.assertIn("今日缺口", html, "R6 概览监测完整性应使用「今日缺口」")
        self.assertIn("采集缺口", html, "R6 历史范围表应使用「采集缺口」")



    def test_app_js_wal_and_gap_wording(self):
        """§50-§55/§110：WAL 已启用/未启用 + 缺口/损耗新文案必须存在。"""
        app_js = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
        for term in ["WAL 已启用", "WAL 未启用", "未发现 Token 丢失",
                     "未发现采集缺口",
                     "个可能存在 Token 丢失", "n + \" 个\""]:
            self.assertIn(term, app_js, "缺少新文案：%s" % term)
        # §88/§162：0 缺口时 hint 描述缺口（"未发现采集缺口"），而非损耗
        self.assertIn('gc === 0 ? "未发现采集缺口"', app_js,
                      "0 缺口 hint 应显示「未发现采集缺口」")
        # 不再出现 "WAL ·" 残缺（journal_mode + " · " 旧拼接）
        self.assertNotIn('toUpperCase() + " \\u00B7 "', app_js, "残留 'WAL ·' 旧拼接")

    def test_tps_zero_vs_null_semantics(self):
        """§33/§34/§15：TPS 指标存在且为 0 -> '0 tok/s'；只有 null/缺失 -> '--'。
        修复要点：用 != null 判定（而非 truthy，避免 0 被当成 falsy）。"""
        app_js = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn(
            'data.prompt_tps != null ? F.formatTps(data.prompt_tps) + " tok/s"', app_js,
            "Prompt TPS 未用 != null 判定（0 会被误显示为 --）")
        self.assertIn(
            'data.decode_tps != null ? F.formatTps(data.decode_tps) + " tok/s"', app_js,
            "Decode TPS 未用 != null 判定")

    def test_host_grid_container_and_no_ellipsis(self):
        """§20/§21/§24/§25/§31：主机概况主/次值 + 双行 + 无 ellipsis + 容器宽度驱动。"""
        pages_css = (STATIC / "css" / "pages.css").read_text(encoding="utf-8")
        html = self.html
        # 双行（磁盘/网络 ↓/↑）与次值（.stat-sub）结构存在
        self.assertIn('class="stat-value mid host-dual"', html, "缺少磁盘/网络双行结构")
        self.assertIn('id="ovHostDiskR"', html, "缺少磁盘下行(读)")
        self.assertIn('id="ovHostNetW"', html, "缺少网络上行(发)")
        self.assertIn('id="ovHostMemSub"', html, "缺少内存次值(used/total)")
        # host-grid 用 5 列（宽屏），窄屏 3 列；host 数值不 ellipsis
        self.assertIn(".host-grid {", pages_css, "缺少 .host-grid 规则")
        self.assertIn("repeat(5, minmax(0, 1fr))", pages_css, "宽屏主机应为 5 列")
        self.assertIn(".host-grid .stat-value", pages_css, "缺少 host 值样式(应取消 ellipsis)")
        self.assertIn("text-overflow: clip", pages_css, "host 值应 clip 而非 ellipsis")

    def test_today_card_container_query(self):
        """§6/§7/§85/§86：Today Hero 容器驱动（container-type + @container），
        不再用视口 @media(1100) 强制单列。"""
        pages_css = (STATIC / "css" / "pages.css").read_text(encoding="utf-8")
        self.assertIn("container-type: inline-size", pages_css, "缺少容器查询")
        self.assertIn("@container", pages_css, "缺少 @container 规则")
        self.assertIn("repeat(2, minmax(0, 1fr))", pages_css, "Hero 应为 2 列 minmax(0,1fr)")
        # 旧粗暴规则移除：@media(max-width:1099) 不再把 today-hero 降 1fr
        self.assertNotIn(".today-hero, .today-breakdown, .perf-grid { grid-template-columns: 1fr; }",
                         pages_css, "残留旧 @media(1100) 强制单列")

    def test_mobile_no_desktop_scrollbar(self):
        """§62/§63：Mobile 隐藏 10px 桌面滚动条（scrollbar-width:none + webkit 0）。"""
        mobile_css = (STATIC / "css" / "mobile.css").read_text(encoding="utf-8")
        self.assertIn("scrollbar-width: none", mobile_css, "缺少 Mobile 滚动条隐藏")
        self.assertIn(".content::-webkit-scrollbar", mobile_css, "缺少 .content webkit 滚动条隐藏")

    # ---------- Round 3 系统页产品化：术语 / 信息架构 ----------

    def _system_section_html(self):
        html = self.html
        start = html.index('id="page-system"')
        end = html.index('id="page-gpu"')
        return html[start:end]

    def test_system_page_required_terms(self):
        """Round-3 A–I：系统页关键术语必须存在（防误删/回退）。"""
        sys_html = self._system_section_html()
        required = [
            "系统概览",            # A 概览区
            "CPU 利用率",          # 概览项（不是"CPU 负载"）
            "网络吞吐",            # 概览项
            "监测组件功耗",         # 概览项 + 功耗区
            "系统运行时间",         # 概览项
            "逻辑处理器利用率",     # CPU Heat Grid（不是"每核心负载"）
            "当前频率",            # CPU 区（区分 current）
            "基准频率",            # CPU 区（区分 base）
            "接收",                # 网络区（不是"下载"）
            "发送",                # 网络区（不是"上传"）
            "链路速度",            # 网络区
            "今日监测组件能耗（估算）",  # 功耗区（估算标注）
            "整机输入功耗",         # 功耗区
            "硬件传感器",           # 传感器区（不是"高级传感器"）
            "刷新硬件信息",         # 硬件信息区
        ]
        for term in required:
            self.assertIn(term, sys_html, "系统页缺少术语：%s" % term)
        # "未配置"：整机输入功耗无外部源时的展示（在 JS 或 HTML 任一）
        self.assertIn("未配置", self.blob, "缺少整机输入功耗'未配置'展示")

    def test_system_page_banned_terms(self):
        """Round-3：系统页旧/机器翻译感术语必须消失。"""
        sys_html = self._system_section_html()
        # 这些在系统页 section 内不允许出现
        for term in ["CPU 负载", "每核心负载", "墙插功耗", "下载速率", "上传速率"]:
            self.assertNotIn(term, sys_html, "系统页残留旧术语：%s" % term)
        # "高级传感器" 在系统页应改为"硬件传感器"（设置页"高级硬件传感器"是另一处，单独允许）
        self.assertNotIn("高级传感器", sys_html,
                         "系统页应使用'硬件传感器'而非'高级传感器'")

    def test_system_charts_registered(self):
        """Round-3：5 个系统图表（CPU/内存/磁盘/网络/功耗）必须注册到 ensurePageCharts
        且 charts.js 有对应 renderer。"""
        app_js = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
        charts_js = (STATIC / "js" / "charts.js").read_text(encoding="utf-8")
        # 页面 hook 注册全部 5 个图
        for cid in ["chartSysCpu", "chartSysMem", "chartSysDisk", "chartSysNet", "chartSysPower"]:
            self.assertIn('"' + cid + '"', app_js, "ensurePageCharts 缺少 %s" % cid)
        # charts.js 有 renderer 定义 + 导出
        for fn in ["renderSysCpuChart", "renderSysMemChart", "renderSysDiskChart",
                   "renderSysNetChart", "renderSysPowerChart"]:
            self.assertIn("function " + fn, charts_js, "charts.js 缺少 " + fn)
            self.assertIn(fn + ": " + fn, charts_js, "charts.js 未导出 " + fn)

    def test_system_page_heat_grid_fixed_order(self):
        """Round-3 §60：逻辑处理器 Heat Grid 按固定 CPU Index 顺序（不按利用率重排），
        Cell 显示序号 N + 百分比。断言 system.js 渲染逻辑。"""
        system_js = (STATIC / "js" / "system.js").read_text(encoding="utf-8")
        self.assertIn("renderCoreHeat", system_js, "缺少 Heat Grid 渲染 renderCoreHeat")
        self.assertIn("core-heat-grid", system_js, "Heat Grid 未用 .core-heat-grid")
        # 固定 index 顺序：for i in perCore 顺序渲染（不 sort）
        self.assertIn("for (var i = 0; i < perCore.length; i++)", system_js,
                      "Heat Grid 必须按固定 index 顺序渲染（不重排）")


if __name__ == "__main__":
    unittest.main()
