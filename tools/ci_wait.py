#!/usr/bin/env python3
"""ci_wait.py — 等待 tag 触发的 release workflow 完成并报告结论（1.1.2/1.1.3 发布链复用）。

用法：
  python tools/ci_wait.py [--token ghp_xxx] [--max-min 25] [--sha <commit>]

- 找到 head_sha 匹配（或最新）的 release.yml run，轮询到 completed；
- 输出最终 status/conclusion/job 列表；
- 退出码 0 = success，1 = failure，2 = 超时/未找到。
"""
import argparse
import base64
import json
import sys
import time
import urllib.request

REPO = "mixiaobu/llamamonitor"
PROXY = "http://127.0.0.1:10808"
API = f"https://api.github.com/repos/{REPO}/actions"


def call(path, token):
    req = urllib.request.Request(
        API + path,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "lm-ci-wait",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}))
    with opener.open(req, timeout=30) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True)
    ap.add_argument("--sha", default=None, help="只匹配该 head_sha 的 run（缺省取最新）")
    ap.add_argument("--max-min", type=int, default=25)
    a = ap.parse_args()

    runs = call("/workflows/release.yml/runs?per_page=5", a.token).get("workflow_runs", [])
    run = None
    for r in runs:
        if a.sha and r.get("head_sha", "").startswith(a.sha[:7]):
            run = r
            break
    if run is None and runs:
        run = runs[0]
    if run is None:
        print("NO RUN FOUND for release.yml")
        sys.exit(2)

    print(f"run {run['id']} {run['head_sha'][:7]} {run['event']} {run['status']} {run.get('conclusion') or '-'}")
    deadline = time.time() + a.max_min * 60
    while run.get("status") not in ("completed",):
        if time.time() > deadline:
            print(f"TIMEOUT status={run.get('status')}")
            sys.exit(2)
        time.sleep(30)
        run = call(f"/runs/{run['id']}", a.token)
        print(f"  ... {run.get('status')} {run.get('conclusion') or '-'}")

    jobs = call(f"/runs/{run['id']}/jobs", a.token).get("jobs", [])
    for j in jobs:
        print(f"job: {j['name']} {j['status']} {j.get('conclusion')}")
    print(f"RESULT status={run['status']} conclusion={run.get('conclusion')}")
    sys.exit(0 if run.get("conclusion") == "success" else 1)


if __name__ == "__main__":
    main()
