"""Real burn-in ops orchestrator (Phase 16 spec H/I).

按修订后规格 H 在真实环境（真实 llama-server + 真实 LlamaMonitor 0.16.3）
执行运维操作序列，并在 T=1h/2h/4h 打 checkpoint（spec I）：

  T+20s   monitor restart #1        T+1h40m llama restart #3
  T+40m   monitor restart #2        T+2h05m backup (POST /api/data/backup)
  T+1h00  llama restart #1          T+2h10m CSV export (/api/data/export/daily.csv)
  T+1h20m llama restart #2          T+2h30m GPU load change (completion via llama)
                                    T+2h45m Settings view (config API)
  T+1h40m tray hide/show x20
  T+2h20m sleep #1 (~120s)  T+2h25m sleep #2 (~120s)
  T+4h    final checkpoint, 结束（满足 H 最小 4h）

每步记录到 %TEMP%\lm_ops_log.txt（时间戳 + 结果），便于报告引用。
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.error

TEMP = os.environ["TEMP"]
LOG = os.path.join(TEMP, "lm_ops_log.txt")
DB = os.path.join(os.environ["LOCALAPPDATA"], "LlamaMonitor", "monitor.db")
EXE = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "LlamaMonitor", "LlamaMonitor.exe")
MON = "http://127.0.0.1:8765"

LLAMA_CMD = (
    '"C:\\Users\\mixiaobu\\Desktop\\ai\\llamacpp\\llama.cpp\\build\\bin\\Release\\llama-server.exe" '
    '-m "C:\\Users\\mixiaobu\\Desktop\\ai\\qwen3.8-27b\\Huihui-Qwen3.8-27B-abliterated-UD-Q4_K_XL.gguf" '
    '--mmproj "C:\\Users\\mixiaobu\\Desktop\\ai\\qwen3.8-27b\\mmproj-model-bf16.gguf" '
    '--no-mmproj-offload --image-min-tokens 1024 --image-max-tokens 1024 '
    '--mtmd-batch-max-tokens 1024 --device CUDA0 -ngl all --fit off --load-mode mmap -fa on '
    '-c 262144 -np 1 -ctk q8_0 -ctv q8_0 -b 8192 -ub 1024 -t 96 -tb 96 --jinja '
    '--reasoning on --reasoning-effort medium --reasoning-budget -1 --reasoning-preserve '
    '--cache-prompt --cache-ram 131072 --ctx-checkpoints 32 --checkpoint-min-step 8192 '
    '--spec-type draft-mtp --spec-draft-n-max 4 --spec-draft-n-min 0 --spec-draft-p-min 0.2 '
    '-ctkd q8_0 -ctvd q8_0 --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0 '
    '--presence-penalty 0.0 --repeat-penalty 1.0 --perf --metrics --log-timestamps '
    '--host 0.0.0.0 --port 9091 --alias qwen3.8-27b-medium'
)


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def api_get(path: str, timeout: float = 5.0):
    with urllib.request.urlopen(MON + path, timeout=timeout) as r:
        return json.loads(r.read())


def api_post(path: str, timeout: float = 60.0):
    req = urllib.request.Request(MON + path, method="POST", data=b"")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def wait_up(pred, what: str, deadline: float) -> bool:
    while time.time() < deadline:
        try:
            if pred():
                log(f"OK {what} is up")
                return True
        except Exception:
            pass
        time.sleep(5)
    log(f"TIMEOUT waiting for {what}")
    return False


def checkpoint() -> None:
    t = subprocess.run([sys.executable, os.path.join(TEMP, "lm_burnin_checkpoint.py")],
                       capture_output=True, text=True, timeout=120)
    log("checkpoint: " + t.stdout.strip().replace("\n", " | "))


def db_q(sql: str):
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def monitor_restart(n: int) -> None:
    log(f"=== monitor restart #{n}: POST /api/app/exit ===")
    try:
        api_post("/api/app/exit", timeout=30)
    except Exception as e:
        log(f"exit call err (可能已退出): {e}")
    d = time.time() + 60
    while time.time() < d:
        try:
            api_get("/api/status")
            time.sleep(2)
        except Exception:
            break
    log("monitor process gone")
    subprocess.Popen([EXE, "--background"], creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    log("monitor relaunch issued")

    def _monitor_alive():
        st = api_get("/api/status")
        lu = st.get("last_update")
        return bool(lu) and (time.time() - lu) < 20  # collector 已开始轮询

    ok = wait_up(_monitor_alive, "monitor+collector", time.time() + 300)
    if ok:
        st = api_get("/api/status")
        log(f"monitor back: v{st.get('version')} uptime={st['application']['uptime_seconds']} online={st['server_online']}")


def llama_restart(n: int) -> None:
    log(f"=== llama-server restart #{n}: kill + relaunch ===")
    # 先记录 restart 前 monitor 状态（确认在线，缺口判定才有意义）
    try:
        st = api_get("/api/status")
        log(f"pre-restart: online={st['server_online']}")
    except Exception as e:
        log(f"pre-restart status err: {e}")
    subprocess.run('taskkill /F /IM llama-server.exe', capture_output=True, text=True)
    # 等 monitor 检测到离线（server_online=False 或 API 短暂不可用）
    time.sleep(10)
    p = subprocess.Popen(
        f'cd /d C:\\Users\\mixiaobu\\Desktop\\ai\\llamacpp\\llama.cpp\\build\\bin\\Release && '
        f'{LLAMA_CMD} > "{TEMP}\\lm_llama_stdout.log" 2>&1',
        shell=True, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    log("llama relaunch issued (model load ~1-3 min)")
    ok = wait_up(lambda: bool(api_get("/api/status")["server_online"]),
                 "llama-server", time.time() + 420)
    if ok:
        st = api_get("/api/status")
        log(f"llama back: online, last_update age {time.time()-st['last_update']:.0f}s")


def sleep_wake(n: int, secs: int) -> None:
    log(f"=== sleep #{n} (~{secs}s, SetSuspendState) ===")
    before = len(db_q("SELECT id FROM data_gaps WHERE reason='system_pause_or_sleep'"))
    import ctypes
    r = ctypes.windll.kernel32.SetSuspendState(ctypes.c_int(0), ctypes.c_int(0), ctypes.c_int(1))
    log(f"SetSuspendState ret={r} (0/False=可能失败)")
    t0 = time.time()
    while time.time() - t0 < secs + 900:
        try:
            st = api_get("/api/status")
            if st.get("last_update") and (time.time() - st["last_update"]) < 15:
                break
        except Exception:
            pass
        time.sleep(10)
    after = len(db_q("SELECT id FROM data_gaps WHERE reason='system_pause_or_sleep'"))
    log(f"woke up: system_pause_or_sleep gaps {before} -> {after}")


def tray_hide_show(n: int, rounds: int) -> None:
    """真实窗口 hide/show：WM_CLOSE（-> _on_closing 隐藏到托盘）+
    Local\\LlamaMonitor.ShowWindow named event（-> tray open 显示）。
    与托盘"Open"项和窗口 X 按钮完全同路径。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    WM_CLOSE = 0x0010
    ok = 0
    log(f"=== tray hide/show x{rounds} (WM_CLOSE hide + named-event show) ===")
    for i in range(rounds):
        # hide：向主窗口发 WM_CLOSE（被 _on_closing 拦截 -> 隐藏，监控继续）
        hwnd = user32.FindWindowW(None, "LlamaMonitor")
        if hwnd:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            time.sleep(2.5)
        # show：发 named event（tray open 同路径）
        eh = ctypes.windll.kernel32.CreateEventW(None, 1, 0, "Local\\LlamaMonitor.ShowWindow")
        if eh:
            ctypes.windll.kernel32.SetEvent(eh)
            ctypes.windll.kernel32.CloseHandle(eh)
        time.sleep(2.5)
        ok += 1
    log(f"tray hide/show done ({ok}/{rounds})")


def do_backup() -> None:
    try:
        before = len(api_get("/api/data/backups").get("backups", []))
    except Exception:
        before = -1
    r = api_post("/api/data/backup", timeout=120)
    try:
        after = len(api_get("/api/data/backups").get("backups", []))
    except Exception:
        after = -1
    log(f"backup: {before} -> {after} backups (resp={str(r)[:120]})")


def do_csv() -> None:
    with urllib.request.urlopen(MON + "/api/data/export/daily.csv", timeout=60) as r:
        data = r.read()
    rows = data.count(b"\n")
    log(f"CSV export: {len(data)} bytes, {rows} rows")


def do_gpu_load() -> None:
    t0 = time.time()
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:9091/v1/completions",
            data=json.dumps({
                "prompt": "Count from 1 to 30, one number per line.",
                "max_tokens": 120,
                "temperature": 0.1,
                "stream": False,
            }).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.loads(r.read())
        n = len(out.get("choices", [{}])[0].get("text", ""))
        log(f"GPU load: completion {time.time()-t0:.1f}s, {n} chars output")
    except Exception as e:
        log(f"GPU load err: {e}")


def do_settings_view() -> None:
    st = api_get("/api/config")
    log(f"settings view: config loaded={st.get('loaded')} has_errors={st.get('has_errors')}")


def main() -> int:
    t0 = time.time()
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"\n===== BURN-IN OPS START {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    log("ops start (spec H: real env, monitor 0.16.3, llama Qwen3.8-27B)")

    def at(sec: float, fn, *a):
        return (sec, fn, a)

    steps = [
        at(20, monitor_restart, 1),
        at(2400, monitor_restart, 2),          # 40m
        at(3600, llama_restart, 1),            # 1h
        at(4800, llama_restart, 2),            # 1h20m
        at(6000, llama_restart, 3),            # 1h40m
        at(6200, tray_hide_show, 1, 20),
        at(7200, do_backup),                   # 2h
        at(7500, do_csv),                      # 2h05m
        at(7900, sleep_wake, 1, 120),          # 2h12m
        at(8500, sleep_wake, 2, 120),          # 2h22m
        at(8700, do_gpu_load),                 # 2h25m
        at(9000, do_settings_view),            # 2h30m
    ]
    checkpoints = {3600: "T=1h", 7200: "T=2h", 14400: "T=4h"}
    steps.sort(key=lambda s: s[0])
    next_i = 0
    done = set()
    while time.time() - t0 < 14400 + 180:
        now = time.time() - t0
        for ck, label in checkpoints.items():
            if label not in done and now >= ck:
                done.add(label)
                log(f"=== {label} checkpoint ===")
                checkpoint()
        if next_i < len(steps) and now >= steps[next_i][0]:
            _, fn, a = steps[next_i]
            next_i += 1
            try:
                fn(*a)
            except Exception as e:
                log(f"STEP FAIL {fn.__name__}: {e}")
    log("=== ops complete, final checkpoint ===")
    checkpoint()
    log(f"ops end (total {time.strftime('%H:%M:%S')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
