# -*- coding: utf-8 -*-
"""v0.16.20 release：创建 release + 上传 5 个资产
（installer / portable / manifest / sig / SHA256SUMS）。"""
import json
import os
import urllib.parse
import urllib.request

TOKEN = os.environ["GITHUB_TOKEN"]
API = "https://api.github.com"

BODY = {
    "tag_name": "v0.16.20",
    "target_commitish": "main",
    "name": "LlamaMonitor 0.16.20",
    "body": (
        "## 0.16.20 — 手机端反馈修复\n\n"
        "- **手机时间范围筛选不显示（核心）**：`/api/config` 是 loopback-only，手机走局域网 IP 访问得 403，"
        "而 GPU/用量页的时间筛选被放在 config 请求成功之后才创建，403 导致筛选永远不出现。"
        "现在筛选先用默认值创建，config 到达后再同步服务器默认范围；窄屏页头改列式，筛选整行铺满不再被裁切\n"
        "- **手机访问 local-only 端点 403**：更新状态轮询对「local-only endpoint」403 静默跳过（本机行为不变）\n"
        "- **设置页文件夹三按钮溢出**：「打开数据/日志/备份」窄屏改纵向全宽堆叠\n"
        "- **手机点按高亮圆角不一致**：原生 tap highlight 与元素圆角不符——交互元素改用自绘 :active 反馈，"
        "高亮圆角与按钮/胶囊完全一致\n"
        "- **概览 GPU 卡显存折行**：窄屏显存值字号降一档，「29.7 / 32.0 GiB」单行显示不再从数字中间折断\n"
        "- **能耗估算卡挤压**：GPU 名称过长时左侧完整换行，右侧估算值锁定单行不被压缩\n\n"
        "更新方式：关于页点「检查更新」；手机用户直接刷新浏览器。"
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

    r = post(API + "/repos/mixiaobu/llamamonitor/releases",
             json.dumps(BODY, ensure_ascii=False).encode("utf-8"),
             {"Content-Type": "application/json; charset=utf-8"})
    rid = r["id"]
    print("release created:", rid, r["tag_name"])

    assets = [
        "LlamaMonitor-Setup-0.16.20-win-x64.exe",
        "LlamaMonitor-0.16.20-win-x64.zip",
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
