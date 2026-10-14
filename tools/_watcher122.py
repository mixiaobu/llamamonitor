# 1.2.2 watcher 管线集成验证：真实 Win32 窗口 + 伪造工作区（不改用户分辨率）
import ctypes, threading, time, sys, logging
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
sys.path.insert(0, r"C:\Users\mixiaobu\Desktop\ai\LlamaMonitor")
from ctypes import wintypes
import desktop

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

# ---- 1. 真实顶层窗口（屏幕外 3000,3000，1400x900） ----
ProcType = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)

class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", ProcType),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

_cb = ProcType(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
wc = WNDCLASSW()
wc.lpfnWndProc = _cb
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.DefWindowProcW.restype = wintypes.LPARAM
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
wc.hInstance = kernel32.GetModuleHandleW(None)
wc.lpszClassName = "LMWatcherTest122"
user32.RegisterClassW(ctypes.byref(wc))
hwnd = user32.CreateWindowExW(0, "LMWatcherTest122", "LMWatcherTest122", 0x00CF0000,
                              3000, 3000, 1400, 900, 0, 0, wc.hInstance, None)
assert hwnd, "CreateWindowExW failed"
user32.ShowWindow(hwnd, 5)
user32.UpdateWindow(hwnd)

# ---- 2. 伪造工作区：GetMonitorInfoW 返回 (0,0,1000,700) ----
# 注意：不声明 orig.argtypes（loop 内部用自己的 MONITORINFO 类，类型不同）
orig = user32.GetMonitorInfoW
phase = {"fake": True}

def fake_gmi(hmon, lpmi):
    r = orig(hmon, lpmi)
    if phase["fake"] and r:
        # lpmi 是 loop 内部 byref(其 MONITORINFO)：cast 成本脚本同布局类改写 rcWork
        mi = ctypes.cast(lpmi, ctypes.POINTER(MONITORINFO)).contents
        mi.rcWork.left = 0; mi.rcWork.top = 0
        mi.rcWork.right = 1000; mi.rcWork.bottom = 700
    return r

user32.GetMonitorInfoW = fake_gmi

# ---- 3. 跑 watcher ----
stop = threading.Event()
t = threading.Thread(target=desktop._display_watcher_loop, args=(stop, lambda: hwnd, 0.3))
t.start()
# 主线程 pump 消息（跨线程 SetWindowPos 的 sent message 需要窗口属主线程处理；
# 生产中 pywebview 主线程本就在持续 pump）
u32p = ctypes.CDLL("user32")
t0 = time.time()
while time.time() - t0 < 1.2:
    msg = wintypes.MSG()
    while u32p.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):
        u32p.TranslateMessage(ctypes.byref(msg))
        u32p.DispatchMessageW(ctypes.byref(msg))
    time.sleep(0.02)
stop.set()
t.join(3)
phase["fake"] = False

# loop 在共享的 windll.user32 实例上设过 argtypes（它自己的 RECT 类），
# 本脚本用独立 CDLL 实例读回窗口位置
u32 = ctypes.CDLL("user32", use_last_error=True)
u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
wr = RECT()
u32.GetWindowRect(hwnd, ctypes.byref(wr))
got = (wr.left, wr.top, wr.right - wr.left, wr.bottom - wr.top)
# 期望：fit((3000,3000,1400,900),(0,0,1000,700),min(1000,650)) = (0,0,1000,700)
ok = got == (0, 0, 1000, 700)
print("final rect:", got, "expected (0, 0, 1000, 700)")
print("RESULT:", "PASS" if ok else "FAIL")
user32.DestroyWindow(hwnd)
sys.exit(0 if ok else 1)
