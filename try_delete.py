"""手動測試：刪除指定的聊天室（電腦版，無法復原）。

先讀取完整聊天列表，由使用者輸入編號並輸入 y 確認後才刪除；未選的聊天室不會被點擊。
用法：先開啟 LINE 並切到「聊天」分頁，然後執行
    .venv\\Scripts\\python.exe try_delete.py
"""
import time

from src.actions import DeleteFailed, delete_chat
from src.line_window import LineNotFound, LineWindow, StopRequested
from src.stop_key import watch_stop_key

DELAY = 1.0  # 每筆之間的間隔秒數


def parse_indices(text, count):
    indices = []
    for part in text.replace("，", ",").split(","):
        part = part.strip()
        if not part.isdigit() or int(part) >= count:
            return None
        indices.append(int(part))
    return sorted(set(indices))


def main():
    input("按 Enter 開始讀取聊天列表。執行期間請勿操作滑鼠；按住 Esc 可中止...")
    try:
        lw = LineWindow()
        with watch_stop_key():
            chats = lw.read_all()
    except (LineNotFound, StopRequested) as e:
        print(e)
        return 1
    for i, chat in enumerate(chats):
        print(f"  [{i:03}] {chat.name or '(無法辨識)'}")

    indices = parse_indices(input("\n輸入要刪除的編號（多筆用逗號分隔）；直接 Enter 取消：").strip(), len(chats))
    if not indices:
        print("已取消，沒有做任何操作。")
        return 0
    targets = [chats[i] for i in indices]
    print("\n即將刪除（電腦版，無法復原）：")
    for i, chat in zip(indices, targets):
        print(f"  [{i:03}] {chat.name or '(無法辨識)'}")
    if input("確定要刪除請輸入 y：").strip().lower() != "y":
        print("已取消，沒有做任何操作。")
        return 0

    done, failed = 0, 0
    try:
        with watch_stop_key():
            for n, chat in enumerate(targets, 1):
                label = chat.name or "(無法辨識)"
                try:
                    delete_chat(lw, chat)
                    done += 1
                    print(f"  ({n}/{len(targets)}) 已刪除: {label}")
                except DeleteFailed as e:
                    failed += 1
                    print(f"  ({n}/{len(targets)}) 跳過: {label}（{e}）")
                time.sleep(DELAY)
    except StopRequested as e:
        print(e)
    print(f"\n完成 {done} 筆，失敗 {failed} 筆，未處理 {len(targets) - done - failed} 筆")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
