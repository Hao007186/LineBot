"""Esc 中止熱鍵：用低階鍵盤鉤子偵測使用者實際按下的 Esc。

執行期間會攔下使用者的 Esc，不讓它傳到 LINE（否則會關掉選單或對話框，讓接下來的點擊落到別處）；
程式自己送出的 Esc 帶有 injected 旗標，照常放行。
用法：
    with watch_stop_key():
        ...  # 期間定期呼叫 check_stop()
"""
import ctypes
import threading
from contextlib import contextmanager
from ctypes import wintypes

WH_KEYBOARD_LL = 13
WM_QUIT = 0x0012
VK_ESCAPE = 0x1B
LLKHF_INJECTED = 0x10

LRESULT = wintypes.LPARAM
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_user32.SetWindowsHookExW.restype = wintypes.HHOOK
_user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
_user32.CallNextHookEx.restype = LRESULT
_user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
_user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
_user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

_stop = threading.Event()


class StopRequested(Exception):
    pass


def check_stop():
    if _stop.is_set():
        raise StopRequested("使用者要求中止")


def request_stop():
    """不經熱鍵直接要求中止（例如 GUI 的「停止」按鈕）。"""
    _stop.set()


def _run_hook(ready, state):
    @HOOKPROC
    def proc(code, wparam, lparam):
        if code >= 0:
            key = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if key.vkCode == VK_ESCAPE and not key.flags & LLKHF_INJECTED:
                _stop.set()
                return 1  # 攔下，不傳給 LINE
        return _user32.CallNextHookEx(None, code, wparam, lparam)

    state["thread_id"] = _kernel32.GetCurrentThreadId()
    state["hook"] = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc, _kernel32.GetModuleHandleW(None), 0)
    ready.set()
    if not state["hook"]:
        return
    msg = wintypes.MSG()
    while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        pass
    _user32.UnhookWindowsHookEx(state["hook"])


@contextmanager
def watch_stop_key():
    """期間內使用者按 Esc 會讓 check_stop() 丟出 StopRequested。"""
    _stop.clear()
    ready, state = threading.Event(), {}
    thread = threading.Thread(target=_run_hook, args=(ready, state), daemon=True)
    thread.start()
    ready.wait()
    if not state["hook"]:
        raise RuntimeError("無法安裝 Esc 熱鍵鉤子")
    try:
        yield
    finally:
        _user32.PostThreadMessageW(state["thread_id"], WM_QUIT, 0, 0)
        thread.join(timeout=2)
