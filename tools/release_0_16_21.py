# -*- coding: utf-8 -*-
"""v0.16.21 release：创建 release + 上传 5 个资产
（installer / portable / manifest / sig / SHA256SUMS）。"""
import json
import os
import urllib.parse
import urllib.request

TOKEN = os.environ["GITHUB_TOKEN"]
API = "https://api.github.com"

BODY = {
    "tag_name": "v0.16.21",
    "target_commitish": "main",
    "name": "LlamaMonitor 0.16.21",
    "body": (
        "## 0.16.21 — 手机网络面板 403 噪音\n\n"
        "- **loopback-only 请求零 403**：`/api/config`、`/api/update/*`、`/api/app/integration` "
        "按安全模型仅允许本机（127.0.0.1）访问，手机走局域网 IP 必得 403——此前手机 DevTools "
        "网络面板里每 30 秒刷一条 `/api/update/status` 红色 403。现在前端识别远程客户端后"
        "直接不发这些请求，网络面板完全干净；本机访问行为不变\n"
        "- **设置页远程只读模式**：手机上打开设置页显示「远程只读模式」提示，表单控件与"
        "保存按钮禁用——设置只能在运行 LlamaMonitor 的电脑上改（避免改了没反应）\n\n"
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
        "LlamaMonitor-Setup-0.16.21-win-x64.exe",
        "LlamaMonitor-0.16.21-win-x64.zip",
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
