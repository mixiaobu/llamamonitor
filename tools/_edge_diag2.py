# -*- coding: utf-8 -*-
"""诊断 v2：强制 visible + 原始事件 dump。"""
import json
import subprocess
import tempfile
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _edge_verify import CDP, wait_for_page, APP_URL, DEBUG_PORT, EDGE


def launch_edge():
    user_data = tempfile.mkdtemp(prefix="lm_edge_diag2_")
    args = [
        EDGE,
        "--remote-debugging-port=%d" % DEBUG_PORT,
        "--remote-allow-origins=*",
        "--user-data-dir=" + user_data,
        "--no-first-run", "--no-default-browser-check",
        "--headless=new",
        "--window-size=1280,900",
        APP_URL,
    ]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


edge = launch_edge()
try:
    ws = wait_for_page(DEBUG_PORT)
    cdp = CDP(ws)
    cdp.send("Page.enable"); cdp.send("Runtime.enable"); cdp.send("Network.enable")
    cdp.drain(4.0)
    cdp.evaluate("document.querySelector('[data-page=\"performance\"]').click()")
    cdp.drain(1.0)

    # 强制 visible（模拟用户聚焦该 tab）
    cdp.evaluate("Object.defineProperty(document,'visibilityState',{get:function(){return 'visible'},configurable:true});"
                 "document.dispatchEvent(new Event('visibilitychange'));true")
    print("visibilityState(now):", cdp.evaluate("document.visibilityState"))
    print("isAppVisible:", cdp.evaluate("LM.poll.isAppVisible()"))

    cdp.drain(6.0)
    allreq = [e for e in cdp.events if e.get("method") == "Network.requestWillBeSent"]
    rt = [e for e in allreq if "/api/realtime" in e.get("params", {}).get("request", {}).get("url", "")]
    print("total requestWillBeSent events:", len(allreq))
    print("of which /api/realtime:", len(rt))
    urls = [e.get("params", {}).get("request", {}).get("url", "(none)") for e in allreq]
    from collections import Counter
    print("url histogram:", dict(Counter(u.split("?")[0] for u in urls)))
    # 时间戳间隔
    if len(rt) >= 2:
        ts = [e.get("params", {}).get("timestamp", 0) for e in rt]
        gaps = [round((ts[i+1]-ts[i])*1000) for i in range(len(ts)-1)]
        print("realtime count:", len(rt), "gaps(ms):", gaps)
    cdp.close()
finally:
    try:
        edge.terminate(); edge.wait(timeout=10)
    except Exception:
        try: edge.kill()
        except Exception: pass
