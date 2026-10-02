"""可行性探測：檢查 pywinauto (UIA) 能否讀到 LINE 桌面版的介面元素。

全程只讀取，不會點擊或修改 LINE 的任何東西。
用法：先開啟 LINE 並停在聊天列表，然後執行
    .venv\\Scripts\\python.exe probe.py
"""
import ctypes
import os
import time
from ctypes import wintypes

from pywinauto import Desktop

LINE_EXE = "line.exe"
MAX_DEPTH = 30
MAX_NODES = 5000
LIST_TYPES = {"List", "ListItem", "DataItem", "Tree", "TreeItem", "Table"}
MENU_TYPES = {"Menu", "MenuItem", "MenuBar"}
MENU_COUNTDOWN = 5

_kernel32 = ctypes.windll.kernel32
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_path_cache = {}


def process_path(pid):
    if pid in _path_cache:
        return _path_cache[pid]
    path = ""
    handle = _kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                path = buf.value
        finally:
            _kernel32.CloseHandle(handle)
    _path_cache[pid] = path
    return path


def line_windows():
    """回傳所有屬於 LINE.exe 的可見頂層視窗。"""
    return [w for w in Desktop(backend="uia").windows()
            if os.path.basename(process_path(w.element_info.process_id)).lower() == LINE_EXE]


def area(info):
    r = info.rectangle
    return max(0, r.right - r.left) * max(0, r.bottom - r.top)


def describe(info):
    r = info.rectangle
    return (f"[{info.control_type}] name={info.name!r} class={info.class_name!r} "
            f"auto_id={info.automation_id!r} rect=({r.left},{r.top},{r.right},{r.bottom})")


def dump(root):
    """遞迴走訪元素樹，回傳 (文字行, 統計)。"""
    lines = []
    stats = {"nodes": 0, "named": 0, "types": {}, "lists": [], "menus": []}

    def walk(info, depth):
        if depth > MAX_DEPTH or stats["nodes"] >= MAX_NODES:
            return
        stats["nodes"] += 1
        try:
            text = describe(info)
            ctype, name = info.control_type, info.name
        except Exception as e:
            lines.append("  " * depth + f"<讀取失敗: {e}>")
            return
        lines.append("  " * depth + text)
        stats["types"][ctype] = stats["types"].get(ctype, 0) + 1
        if name:
            stats["named"] += 1
        if ctype in LIST_TYPES:
            stats["lists"].append(text)
        if ctype in MENU_TYPES:
            stats["menus"].append(text)
        try:
            children = info.children()
        except Exception:
            return
        for child in children:
            walk(child, depth + 1)

    walk(root, 0)
    return lines, stats


def write_lines(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def print_stats(stats):
    print(f"  元素總數: {stats['nodes']}（有名稱的: {stats['named']}）")
    types = ", ".join(f"{k}={v}" for k, v in sorted(stats["types"].items(), key=lambda kv: -kv[1]))
    print(f"  類型分布: {types}")


def stage_a():
    print("== 階段 A：主視窗 ==")
    wins = line_windows()
    if not wins:
        print("  找不到 LINE 視窗。請確認 LINE 已開啟、已登入、且視窗沒有最小化。")
        return None
    for w in wins:
        info = w.element_info
        print(f"  候選視窗: title={info.name!r} class={info.class_name!r} "
              f"framework={info.framework_id!r} 面積={area(info)}")
    print(f"  執行檔: {process_path(wins[0].element_info.process_id)}")
    main_info = max(wins, key=lambda w: area(w.element_info)).element_info
    print(f"  選定主視窗: {main_info.name!r}")
    lines, stats = dump(main_info)
    write_lines("probe_main.txt", lines)
    print_stats(stats)
    print("  完整元素樹已寫入 probe_main.txt")
    return stats


def stage_b(stats):
    print("\n== 階段 B：聊天列表 ==")
    if not stats["lists"]:
        print("  沒有找到任何清單類元素（List/ListItem/DataItem/Tree/Table）。")
        return
    print(f"  找到 {len(stats['lists'])} 個清單類元素，前 30 個：")
    for text in stats["lists"][:30]:
        print("   ", text)


def stage_c():
    print("\n== 階段 C：右鍵選單 ==")
    input(f"  按 Enter 後，請在 {MENU_COUNTDOWN} 秒內到 LINE 對任一聊天室按右鍵，"
          "並讓選單保持開啟（不要點任何選項）...")
    for i in range(MENU_COUNTDOWN, 0, -1):
        print(f"  {i}...", flush=True)
        time.sleep(1)
    all_lines, menus = [], []
    for w in line_windows():
        lines, stats = dump(w.element_info)
        all_lines += [f"### 頂層視窗 {w.element_info.name!r}"] + lines + [""]
        menus += stats["menus"]
    write_lines("probe_menu.txt", all_lines)
    if menus:
        print(f"  找到 {len(menus)} 個選單類元素：")
        for text in menus:
            print("   ", text)
    else:
        print("  沒有找到 Menu/MenuItem 元素（選單可能是自繪的，詳見 probe_menu.txt）。")
    print("  完整內容已寫入 probe_menu.txt。請回到 LINE 按 Esc 關閉右鍵選單。")
    return bool(menus)


def main():
    stats = stage_a()
    if stats is None:
        return 1
    stage_b(stats)
    has_menu = stage_c()

    print("\n== 初步判讀 ==")
    print(f"  主視窗內部元素可讀: {'是' if stats['nodes'] > 5 and stats['named'] > 0 else '否（可能是自繪介面）'}")
    print(f"  清單類元素: {'有' if stats['lists'] else '無'}")
    print(f"  右鍵選單可讀: {'是' if has_menu else '否'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
