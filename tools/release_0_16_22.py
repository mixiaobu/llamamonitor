# -*- coding: utf-8 -*-
"""v0.16.22 release：创建 release + 上传 5 个资产
（installer / portable / manifest / sig / SHA256SUMS）。"""
import json
import os
import urllib.parse
import urllib.request

TOKEN = os.environ["GITHUB_TOKEN"]
API = "https://api.github.com"

BODY = {
    "tag_name": "v0.16.22",
    "target_commitish": "main",
    "name": "LlamaMonitor 0.16.22",
    "body": (
        "## 0.16.22 — 远程隐藏设置入口\n\n"
        "- **手机上不再显示「设置」导航**：远程（局域网）客户端的配置接口是"
        "本机专用（loopback-only），进去只能看到只读表单——现在直接不给入口，"
        "更干净。本机访问行为完全不变，设置页照常可用\n\n"
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
        "LlamaMonitor-Setup-0.16.22-win-x64.exe",
        "LlamaMonitor-0.16.22-win-x64.zip",
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
