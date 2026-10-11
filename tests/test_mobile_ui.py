"""
1.1.2/1.1.3 Mobile & Visual Experience 回归测试（静态层）。

静态断言（前端字符串级，风格同 test_ui_terminology.py）：
- 真 Mobile 模式（≤760px）：Bottom Navigation（1.1.3：5 核心页）+
  每页页头 ••• overflow More Sheet（focus trap + 滚动锁）+ safe-area
- 表格 → Card Rows（td[data-label] 注入 + 显式 .mobile-card-table CSS 规则）
- Settings 横向 tabs + sticky save bar
- Touch / tooltip / remote read-only / 逐页 max-width
- 设计 token 唯一视觉源（mobile 变量存在）

动态行为（overflow/touch target/结构）由 CDP 巡检覆盖：
scripts/ui_patrol_113.js（17 viewport×page 矩阵）。

运行：python -m unittest discover -s tests
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
CSS = STATIC / "css"
JS = STATIC / "js"


def _read(rel):
    return (STATIC / rel).read_text(encoding="utf-8")


class MobileSkeletonTests(unittest.TestCase):
    """≤760px 真 Mobile：Bottom Nav + More Sheet + safe-area。"""

    @classmethod
    def setUpClass(cls):
        cls.html = _read("index.html")
        cls.layout = _read("css/layout.css")
        cls.mobile_css = _read("css/mobile.css")
        cls.nav_js = _read("js/navigation.js")

    def test_mobile_nav_dom_present(self):
        # 1.1.3：底部导航 = 5 个核心监控域（概览/用量/性能/GPU/系统）；
        # 辅助页（历史/设置/关于）改由每页页头 ••• overflow（.page-overflow）触发。
        for frag in [
            '<nav class="mobile-nav"',
            'data-page="overview"',
            'data-page="usage"',
            'data-page="performance"',
            'data-page="gpu"',
            'data-page="system"',
        ]:
            self.assertIn(frag, self.html, "Mobile Nav DOM 缺失: %s" % frag)

    def test_more_sheet_dom_present(self):
        # 1.1.3：overflow sheet 只含辅助页（历史/设置/关于），system 已进底部导航。
        for frag in [
            'id="moreSheetScrim"',
            'id="moreSheet" role="dialog" aria-modal="true"',
            'data-page="history"',
            'data-page="settings"',
            'data-page="about"',
            'id="moreSheetVersion"',
        ]:
            self.assertIn(frag, self.html, "More Sheet DOM 缺失: %s" % frag)

    def test_bottom_nav_css_and_safe_area(self):
        self.assertIn(".mobile-nav", self.layout)
        self.assertIn("env(safe-area-inset-bottom)", self.layout,
                      "Bottom Nav 必须预留 safe-area")
        self.assertIn("--bottom-nav-height", self.layout)
        # ≤760px 隐藏桌面侧边栏、内容占满
        m760 = self.layout
        self.assertIn("@media (max-width: 760px)", m760)
        self.assertIn("display: none", m760)  # .navview

    def test_nav_js_wires_mobile(self):
        self.assertIn("initMobileNav", self.nav_js)
        self.assertIn("registerNavGuard", self.nav_js,
                      "nav guard 必须存在（设置页未保存确认 desktop/mobile 共用）")
        # 1.1.3：••• overflow 按钮由 navigation.js 注入并接线（旧 mnav-more tab 已移除）
        self.assertIn("page-overflow", self.nav_js)
        self.assertIn("openSheet", self.nav_js)
        self.assertIn("closeSheet", self.nav_js)
        # focus trap + 滚动锁（spec §49）
        self.assertIn("trapFocus", self.nav_js, "sheet 必须有 focus trap")
        self.assertIn("lockBodyScroll", self.nav_js, "sheet 打开必须锁 body 滚动")

    def test_bottom_nav_click_wired(self):
        # Round-8 修复：底栏 .mnav-item 必须接线到 showPage
        # （之前只绑了 .nav-item 桌面侧边栏 -> 手机端点底部导航无效果）
        self.assertIn('.mnav-item', self.nav_js,
                      "navigation.js 未接线 .mnav-item 点击（手机端底部导航无响应）")
        self.assertIn('mobile-nav', self.nav_js)

    def test_mobile_css_registered_in_html(self):
        self.assertIn('href="/static/css/mobile.css"', self.html,
                      "mobile.css 未挂到 index.html")

    def test_more_icon_exists(self):
        icons = _read("js/icons.js")
        self.assertIn("more:", icons, "icons.js 缺少 more 图标（Bottom Nav 更多）")


class TableCardRowTests(unittest.TestCase):
    """表格 → Card Rows：JS 注入 data-label + CSS 规则。"""

    @classmethod
    def setUpClass(cls):
        cls.app_js = _read("js/app.js")
        cls.mobile_css = _read("css/mobile.css")

    def test_daily_rows_have_data_labels(self):
        # 1.1.4 Round 4：用量页术语冻结——输入→Prompt、输出→生成（缓存复用不变）。
        for label in ["data-label='Prompt'", "data-label='缓存复用'", "data-label='生成'",
                      "data-label='实际计算'", "data-label='Token 总量'",
                      "data-label='缓存复用率'", "data-label='采集覆盖率'", "data-label='缺口'"]:
            self.assertIn(label, self.app_js, "每日明细行缺少 %s" % label)

    def test_gap_rows_have_data_labels(self):
        # Round-6：采集缺口表列 = 时间 / 持续时间 / 来源 / 原因 / Token 风险
        for label in ["data-label='时间'", "data-label='持续时间'", "data-label='来源'",
                      "data-label='原因'", "data-label='Token 风险'"]:
            self.assertIn(label, self.app_js, "缺口行缺少 %s" % label)

    def test_event_rows_have_data_labels(self):
        # Round-6：监控事件表列 = 时间 / 事件 / 来源 / 详情
        for label in ["data-label='时间'", "data-label='事件'", "data-label='来源'",
                      "data-label='详情'"]:
            self.assertIn(label, self.app_js, "事件行缺少 %s" % label)

    def test_card_row_css_present(self):
        # 1.1.3：card row 选择器由 :has() 改为显式 .mobile-card-table 类
        # （app.js init 时对 daily/gap/events 的 .table-wrap 注入），规避旧浏览器 :has 缺失。
        for frag in [
            ".table-wrap.mobile-card-table",
            "content: attr(data-label)",
            "thead",
        ]:
            self.assertIn(frag, self.mobile_css, "card rows CSS 缺失: %s" % frag)

    def test_card_row_class_injection(self):
        # app.js 必须注入 .mobile-card-table（CSS 依赖它定位三张卡行表）
        self.assertIn("mobile-card-table", self.app_js,
                      "app.js 未注入 mobile-card-table 类")


class SettingsMobileTests(unittest.TestCase):
    """Settings ≤760px：单行横向 tabs + sticky save bar + 未保存守卫。"""

    @classmethod
    def setUpClass(cls):
        cls.mobile_css = _read("css/mobile.css")
        cls.settings_js = _read("js/settings.js")

    def test_settings_rail_single_row(self):
        self.assertIn("flex-wrap: nowrap", self.mobile_css,
                      "settings rail 必须单行（禁多行 wrap）")
        self.assertIn("overflow-x: auto", self.mobile_css, "rail 需可横滚")

    def test_settings_sticky_save_bar_above_bottom_nav(self):
        # save bar 固定，bottom = bottom-nav 高度 + safe-area
        self.assertIn(
            "bottom: calc(var(--bottom-nav-height) + env(safe-area-inset-bottom))",
            self.mobile_css, "save bar 必须钉在 Bottom Navigation 上方（含 safe-area）")

    def test_unsaved_nav_guard(self):
        self.assertIn("unsavedGuard", self.settings_js, "缺少设置页未保存守卫")
        self.assertIn('registerNavGuard("settings"', self.settings_js,
                      "未保存守卫必须注册到 LM.nav")


class TouchAndA11yTests(unittest.TestCase):
    """Touch target / tooltip tap / remote banner / forced-colors。"""

    @classmethod
    def setUpClass(cls):
        cls.mobile_css = _read("css/mobile.css")
        cls.html = _read("index.html")
        cls.app_js = _read("js/app.js")
        cls.comp_js = _read("js/components.js")
        cls.tokens = _read("css/tokens.css")

    def test_touch_targets(self):
        self.assertIn("--touch-target: 44px", self.tokens, "缺少 --touch-min 44px token")
        self.assertIn("min-height: 44px", self.mobile_css, "btn 触屏高度 <44px")
        self.assertIn(".tip-btn::after", self.mobile_css,
                      "tooltip 按钮需放大 hit area（::after 扩展）")

    def test_tooltip_tap_toggle(self):
        self.assertIn("tip-open", self.comp_js, "tooltip 缺少 tap 开/关逻辑")
        self.assertIn(".info-tip.tip-open .tip-text", self.mobile_css,
                      "tap 打开态 CSS 缺失")
        self.assertIn("max-width: min(300px, calc(100vw - 32px))", self.mobile_css,
                      "tooltip 不得超屏")

    def test_remote_banner_removed_121(self):
        """1.2.1：顶部远程只读 banner 已移除（用户要求去掉顶部提示）；
        远程只读状态改由 Overflow Sheet 内的 more-sheet-remote 行表达。"""
        self.assertNotIn('id="remoteBanner"', self.html, "1.2.1 应移除顶部远程 banner")
        self.assertNotIn(".remote-banner", self.layout_css, "1.2.1 应移除 remote-banner 死样式")
        self.assertIn('id="moreSheetRemote"', self.html, "Overflow Sheet 远程只读行保留")

    @property
    def layout_css(self):
        return _read("css/layout.css")

    def test_hover_only_on_hover_devices(self):
        self.assertIn("@media (hover: none)", self.mobile_css,
                      "hover 效果需限定到 hover 设备（防手机点按残留）")

    def test_forced_colors(self):
        self.assertIn("@media (forced-colors: active)", self.mobile_css,
                      "缺少 Windows 高对比（forced-colors）基础支持")


class TokenSourceTests(unittest.TestCase):
    """tokens.css 为唯一视觉源：mobile token 体系存在。"""

    @classmethod
    def setUpClass(cls):
        cls.tokens = _read("css/tokens.css")

    def test_mobile_typography_tier(self):
        # ≤760px 覆盖：body 15 / card 16 / hero 32 / metric 24
        self.assertIn("@media (max-width: 760px)", self.tokens)
        self.assertIn("--font-size-body: 15px", self.tokens)
        self.assertIn("--font-size-hero: 32px", self.tokens)

    def test_chart_height_tokens(self):
        self.assertIn("--chart-height-mobile: 260px", self.tokens)

    def test_page_max_width_tokens(self):
        self.assertIn("--content-max-settings: 1280px", self.tokens)
        self.assertIn("--content-max-about: 960px", self.tokens)

    def test_chart_mobile_height_in_mobile_css(self):
        mobile = _read("css/mobile.css")
        self.assertIn(".chart { height: 240px; }", mobile,
                      "chart 手机高度需 240-280px")


class OverviewSubtitleTests(unittest.TestCase):
    """规格 90：概览副标题去营销化。"""

    def test_subtitle(self):
        html = _read("index.html")
        # 1.2.1：概览副标题已移除（用户要求去掉无效说明）；营销文案保持不存在
        self.assertNotIn("查看 llama.cpp 服务、用量、推理状态、主机资源与监测完整性。",
                         html)
        self.assertNotIn("10 秒看懂", html, "营销文案未清理")


if __name__ == "__main__":
    unittest.main()
