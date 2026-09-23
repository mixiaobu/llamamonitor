# -*- coding: utf-8 -*-
"""v0.16.18 release：创建 release + 上传 5 个资产
（installer / portable / manifest / sig / SHA256SUMS）。
另给 v0.16.17 补传 manifest+sig（修复旧版更新检测）。"""
import json
import os
import urllib.request

TOKEN = os.environ["GITHUB_TOKEN"]
API = "https://api.github.com"
REL17 = "https://uploads.github.com/repos/mixiaobu/llamamonitor/releases/394305675/assets"

BODY = {
    "tag_name": "v0.16.18",
    "target_commitish": "main",
    "name": "LlamaMonitor 0.16.18",
    "body": (
        "## 0.16.18 — Phase 16D 细节精修（间距 / 列宽 / 表格 / 移动端 / 更新）\n\n"
        "- **MTP/投机解码**：行间距与列间距统一 16px\n"
        "- **监控事件**：改为表格（时间/类型/详情三列）；时间紧凑单行（今天 HH:MM:SS / 跨天 MM-DD HH:MM，完整时间进 tooltip）\n"
        "- **最近缺口**：表格取消内层滚动、完整平铺；列宽固定（开始/结束/时长/来源/丢失定宽 + 原因弹性换行）\n"
        "- **表格去卡片包裹**：缺口表/每日明细/事件表不再套 .card，.table-wrap 本身为卡片标准\n"
        "- **每日明细列宽**：固定布局（日期/数值列定宽 + 提示列弹性撑满），min-width 920 窄屏横滚不挤压\n"
        "- **数据质量卡**：移除「最近有效采样」，3 项均分；数值 22px + 语义色（覆盖率黄/数据库绿）\n"
        "- **侧边栏**：展开/收起零跳动（logo、图标、选中指示条位置完全一致）；compact 下 logo 水平居中\n"
        "- **用量页默认 7 天**（daily_default_days 30→7）\n"
        "- **设置页**：内容宽度与其他页一致（1520+32px）；保存栏固定钉在视口最底部、宽度对齐右侧内容列；未保存提示独占上一行不再挤压按钮\n"
        "- **关于页**：宽度与其他页一致\n"
        "- **概览状态条**：在线时不再显示「最后更新 X 秒前 / X 秒后刷新」（5s 轮询信息量低），仅离线/后端不可达显示\n"
        "- **按钮点按圆角修复**：focus-visible 不再覆写圆角（手机点按不再变直角）\n"
        "- **移动端适配**：GPU 卡网格 min(320px,100%) 修 390px 溢出；GPU 勾选 chip 竖排；图表时间轴窄屏抽稀+旋转防挤压；≤700px 图表 240px\n"
        "- **手机直连**：web.host 支持 0.0.0.0（局域网只读，修改类 API 仍限 loopback）\n"
        "- **静态资源 no-cache**：修复原地升级后浏览器缓存旧 JS（F.formatClock is not a function）\n"
        "- **更新修复**：本版本 release 附带 release-manifest.json / .sig / SHA256SUMS.txt；v0.16.17 已补传 manifest，旧版更新检测恢复正常"
    ),
    "draft": False,
    "prerelease": False,
}


def post(url, data, headers=None):
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", "token " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rel = os.path.join(root, "release")

    # 1) v0.16.17 backfill assets (release already exists)
    for name in ("release-manifest.json", "release-manifest.sig"):
        local = os.path.join(rel, "release-manifest-0.16.17." + ("json" if name.endswith("json") else "sig"))
        with open(local, "rb") as f:
            data = f.read()
        up = REL17 + "?name=" + urllib.parse.quote(name)
        post(up, data, {"Content-Type": "application/octet-stream"})
        print("uploaded to 0.16.17:", name, len(data))

    # 2) create v0.16.18 release
    r = post(API + "/repos/mixiaobu/llamamonitor/releases",
             json.dumps(BODY, ensure_ascii=False).encode("utf-8"),
             {"Content-Type": "application/json; charset=utf-8"})
    rid = r["id"]
    print("release created:", rid, r["tag_name"])

    # 3) upload assets
    assets = [
        "LlamaMonitor-Setup-0.16.18-win-x64.exe",
        "LlamaMonitor-0.16.18-win-x64.zip",
        "release-manifest.json",
        "release-manifest.sig",
        "SHA256SUMS.txt",
    ]
    for name in assets:
        local = os.path.join(rel, name)
        with open(local, "rb") as f:
            data = f.read()
        up = ("https://uploads.github.com/repos/mixiaobu/llamamonitor/releases/%d/assets?name=" % rid) + urllib.parse.quote(name)
        post(up, data, {"Content-Type": "application/octet-stream"})
        print("uploaded:", name, len(data))
    print("DONE")


import urllib.parse  # noqa: E402
main()
