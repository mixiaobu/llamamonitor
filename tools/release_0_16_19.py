# -*- coding: utf-8 -*-
"""v0.16.19 release：创建 release + 上传 5 个资产
（installer / portable / manifest / sig / SHA256SUMS）。"""
import json
import os
import urllib.parse
import urllib.request

TOKEN = os.environ["GITHUB_TOKEN"]
API = "https://api.github.com"

BODY = {
    "tag_name": "v0.16.19",
    "target_commitish": "main",
    "name": "LlamaMonitor 0.16.19",
    "body": (
        "## 0.16.19 — 手机反馈修复收尾\n\n"
        "- **GPU 过滤 chip**：去掉外层灰色圆角背景容器（chip 自身已有边框/底色）；手机端 chip 全宽竖排\n"
        "- **图表时间轴**：窄屏抽稀到 20 分钟一档（HH:MM）；日期轴标签 `MM-DD` 缩短并按容器宽度自动抽稀——390px 实测 12 个标签均匀分布无重叠\n"
        "- **手机滚不到最底部**：`.app` 高度改用 `100dvh`，手机浏览器动态工具栏不再把底部内容推出可视区\n"
        "- **按钮点按直角**：`.seg button` 圆角统一到控件标准 8px（手机 sticky hover 高亮持续显示时不再像直角）\n"
        "- **浏览器缓存旧 JS**：静态资源 `Cache-Control: no-cache`（含 HTML，ETag 304 不增流量）——`F.formatClock is not a function` 不再出现；调用点另有 `formatClock || formatDateTime` 兜底\n"
        "- **概览状态条**：在线时不再显示「最后更新 X 秒前 / X 秒后刷新」\n\n"
        "更新方式：关于页点「检查更新」；手机用户直接刷新浏览器（Ctrl+F5 / 关闭重开）。"
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

    # 1) create v0.16.19 release
    r = post(API + "/repos/mixiaobu/llamamonitor/releases",
             json.dumps(BODY, ensure_ascii=False).encode("utf-8"),
             {"Content-Type": "application/json; charset=utf-8"})
    rid = r["id"]
    print("release created:", rid, r["tag_name"])

    # 2) upload assets
    assets = [
        "LlamaMonitor-Setup-0.16.19-win-x64.exe",
        "LlamaMonitor-0.16.19-win-x64.zip",
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
    print("DONE release id:", rid)


main()
