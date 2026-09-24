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
            "随 Windows 启动",
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
        ]
        for term in banned:
            self.assertNotIn(term, self.blob, "已废弃术语仍存在：%s" % term)

    def test_log_backup_count_renamed_in_html(self):
        """日志设置的“备份数量”已改“轮转文件保留数”（备份卡描述文案中的
        “保留的自动备份数量”是数据库备份语境，允许保留）。"""
        self.assertIn("轮转文件保留数", self.html)
        self.assertNotIn("备份数量", self.html.replace("保留的自动备份数量", ""))

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
            "推测验证轮次",
            "上下文高水位",
            "上下文窗口上限",
            "平均忙碌 Slot 数",
            "处理中请求",
            "等待中请求",
            "Token 吞吐率",
            "服务器运行状态",
            "监控历史",
            "采集覆盖率",
            "Token 可能缺失",
            "GPU 监控",
            "GPU 利用率与显存占用",
            "显存占用",
            "功耗与温度",
            "登录时自动启动",
            "监听地址",
            "监听端口",
            "指标采集",
            "指标采集间隔",
            "GPU 采集间隔",
            "实时采样保留时长",
            "界面刷新间隔",
            "默认统计范围",
            "轮转文件保留数",
            "刷新状态",
            "立即备份",
            "检查数据库完整性",
            "清除实时采样历史",
            "重置统计数据",
            "安装类型",
            "最新版本",
            "更新状态",
            "数据库 Schema 版本",
            "llama.cpp 本地只读监控工具",
            "llama-server 已连接",
            "llama-server 连接中断",
            "LlamaMonitor 启动",
            "数据库备份完成",
            "数据库迁移",
            "tok/s",
        ]
        for term in required:
            self.assertIn(term, self.blob, "新术语缺失：%s" % term)

    def test_tps_unit_unified(self):
        """TPS 单位统一 tok/s，不得残留 t/s。"""
        self.assertNotIn(' + " t/s"', self.blob)


if __name__ == "__main__":
    unittest.main()
