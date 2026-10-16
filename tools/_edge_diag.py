# -*- coding: utf-8 -*-
"""诊断：Edge 里 /api/realtime 为何不轮询。"""
import json
import subprocess
import tempfile
import time
import urllib.request
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _edge_verify import CDP, wait_for_page, APP_URL, DEBUG_PORT, EDGE


def launch_edge():
    user_data = tempfile.mkdtemp(prefix="lm_edge_diag_")
    args = [
        EDGE,
        "--remote-debugging-port=%d" % DEBUG_PORT,
        "--remote-allow-origins=*",
        "--user-data-dir=" + user_data,
        "--no-first-run", "--no-default-browser-check",
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
    cdp.drain(2.0)

    print("visibilityState:", cdp.evaluate("document.visibilityState"))
    print("currentPage:", cdp.evaluate("LM.nav.currentPage()"))
    print("appVisible(LM.poll.isAppVisible):", cdp.evaluate("LM.poll.isAppVisible()"))
    print("provider:", cdp.evaluate("state ? state.provider : '(state not global)'"))
    audit = cdp.evaluate("JSON.stringify(LM.poll.audit())")
    print("poll.audit:", audit)
    rt_task = cdp.evaluate("(function(){var t=null;try{t=LM.poll.audit()['realtime']}catch(e){}return JSON.stringify(t)})()")
    print("realtime task:", rt_task)

    # 直接手动调一次 api.realtime，看 Network 是否捕获
    hits = []
    cdp.on("Network.requestWillBeSent", lambda m: (
        m.get("request", {}).get("url", "").find("/api/realtime") >= 0
        and hits.append(m["request"]["url"])))
    r = cdp.evaluate("LM.api.realtime('/api/realtime').then(function(d){return d? (d.provider+' seq='+d.sequence):'null'}).catch(function(e){return 'ERR '+e.message})", await_promise=True)
    cdp.drain(1.0)
    print("manual api.realtime result:", r)
    print("manual request captured:", hits)

    # 检查 #rtSlots 与 badge
    print("rtSlots:", cdp.evaluate("document.getElementById('rtSlots').textContent"))
    print("perfFreshness:", cdp.evaluate("(function(){var b=document.getElementById('perfFreshness');return b? (b.hidden?'hidden':b.className+' | '+b.textContent):'(missing)'})()"))

    # 等 5s，看自然轮询
    nat = []
    cdp.on("Network.requestWillBeSent", lambda m: (
        m.get("request", {}).get("url", "").find("/api/realtime") >= 0
        and nat.append(time.time())))
    cdp.drain(5.0)
    print("natural polls in 5s:", len(nat))
    cdp.close()
finally:
    try:
        edge.terminate(); edge.wait(timeout=10)
    except Exception:
        try: edge.kill()
        except Exception: pass
