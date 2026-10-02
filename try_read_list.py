"""手動測試：讀取 LINE 完整聊天列表。

只會移動滑鼠與捲動列表，不會點擊或修改任何聊天室。
用法：先開啟 LINE 並切到「聊天」分頁，然後執行
    .venv\\Scripts\\python.exe try_read_list.py
結果寫入 out/chats.txt，每列截圖存於 out/rows/。
"""
import os
import time

from src.line_window import LineNotFound, LineWindow, StopRequested
from src.stop_key import watch_stop_key

OUT_DIR = "out"


def main():
    input("按 Enter 開始。執行期間請勿操作滑鼠；按住 Esc 可中止...")
    try:
        lw = LineWindow()
        start = time.time()
        with watch_stop_key():
            chats = lw.read_all(on_new=lambda c: print(f"  讀到: {c.name or '(無法辨識)'}"))
    except (LineNotFound, StopRequested) as e:
        print(e)
        return 1

    rows_dir = os.path.join(OUT_DIR, "rows")
    os.makedirs(rows_dir, exist_ok=True)
    for f in os.listdir(rows_dir):
        os.remove(os.path.join(rows_dir, f))
    with open(os.path.join(OUT_DIR, "chats.txt"), "w", encoding="utf-8") as f:
        for i, chat in enumerate(chats):
            chat.row_img.save(os.path.join(rows_dir, f"{i:03}.png"))
            f.write(f"{i:03}\t{chat.name or '(無法辨識)'}\n")

    unread = sum(1 for c in chats if not c.name)
    print(f"\n共 {len(chats)} 個聊天室（無法辨識名稱: {unread}），耗時 {time.time() - start:.1f} 秒")
    for w in lw.warnings:
        print("警告:", w)
    print(f"結果: {OUT_DIR}/chats.txt，截圖: {rows_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
