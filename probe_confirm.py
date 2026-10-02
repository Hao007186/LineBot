"""探測：刪除聊天室時的確認對話框結構。

流程：選一個聊天室 → 右鍵 → 點「刪除」 → 記錄確認對話框 → 送出 Esc 取消（不會按確認）。
注意：若 LINE 其實沒有確認對話框，點「刪除」那一步就會直接刪掉該聊天室，
所以請務必選一個刪了也無所謂的聊天室（例如官方帳號的廣告推播）。
用法：先開啟 LINE 並切到「聊天」分頁，然後執行
    .venv\\Scripts\\python.exe probe_confirm.py
結果寫入 probe_confirm.txt，截圖存於 probe_ocr/confirm_*.png。
"""
import os
import time

from src import ocr
from src.line_window import LineNotFound, LineWindow, _box, _process_path

from pywinauto import Desktop, keyboard, mouse

from probe import describe, dump, write_lines

MENU_CLASS = "LcContextMenu"
MENU_ITEM_CLASS = "LcContextMenuItem"
DELETE_TEXT = "刪除"
WAIT_TIMEOUT = 3.0
OUT_IMG_DIR = "probe_ocr"


def line_top_windows():
    return [w for w in Desktop(backend="uia").windows()
            if os.path.basename(_process_path(w.element_info.process_id)).lower() == "line.exe"]


def wait_for(fn, timeout=WAIT_TIMEOUT):
    end = time.time() + timeout
    while time.time() < end:
        result = fn()
        if result:
            return result
        time.sleep(0.2)
    return None


def choose_target(lw):
    lw.focus()
    visible = lw.visible_chats()
    print("目前畫面上的聊天室：")
    for i, chat in enumerate(visible):
        chat.name = ocr.read_text(chat.name_img)
        print(f"  [{i}] {chat.name or '(無法辨識)'}")
    choice = input("\n請輸入要拿來測試的編號（選刪了也無所謂的，例如官方帳號）；直接 Enter 取消：").strip()
    if not choice.isdigit() or int(choice) >= len(visible):
        return None
    target = visible[int(choice)]
    print(f"\n選定：{target.name or '(無法辨識)'}")
    print("若 LINE 沒有確認對話框，接下來點「刪除」就會直接刪除這個聊天室（電腦版，無法復原）。")
    if input("確定要繼續請輸入 y：").strip().lower() != "y":
        return None
    return target


def find_menu():
    menus = [w for w in line_top_windows() if w.element_info.class_name == MENU_CLASS]
    return menus[0] if menus else None


def find_delete_item(menu):
    found = []
    for item in menu.descendants(class_name=MENU_ITEM_CLASS):
        r = item.rectangle()
        if r.height() <= 0:
            continue
        text = ocr.read_text(ocr.grab(_box(r)))
        print(f"  選單項目: {text or '(無法辨識)'}")
        if DELETE_TEXT in text:
            found.append(item)
    return found[0] if len(found) == 1 else None


def record_dialog(new_windows, main_window, lines):
    os.makedirs(OUT_IMG_DIR, exist_ok=True)
    for i, w in enumerate(new_windows):
        info = w.element_info
        lines += [f"### 新視窗 {i}: {describe(info)}"] + dump(info)[0] + [""]
        ocr.grab(_box(w.rectangle())).save(os.path.join(OUT_IMG_DIR, f"confirm_{i}.png"))
        print(f"  新視窗 {i}: class={info.class_name!r}")
        for line in ocr.read_lines(ocr.grab(_box(w.rectangle()))):
            print(f"    文字: {line}")
            lines.append(f"# 文字: {line}")
        for el in w.descendants():
            r = el.rectangle()
            if r.width() > 0 and r.height() > 0 and "Button" in (el.element_info.class_name or ""):
                text = ocr.read_text(ocr.grab(_box(r)))
                print(f"    按鈕: class={el.element_info.class_name!r} 文字={text!r} rect={_box(r)}")
                lines.append(f"# 按鈕: class={el.element_info.class_name!r} 文字={text!r} rect={_box(r)}")
    # 對話框也可能畫在主視窗內，主視窗樹一併記錄
    ocr.grab(_box(main_window.rectangle())).save(os.path.join(OUT_IMG_DIR, "confirm_main.png"))
    lines += ["### 主視窗"] + dump(main_window.element_info)[0]


def main():
    try:
        lw = LineWindow()
    except LineNotFound as e:
        print(e)
        return 1
    target = choose_target(lw)
    if target is None:
        print("已取消，沒有做任何操作。")
        return 0

    # 重新定位（選擇期間列表可能有變動）
    lw.focus()
    row = next((c for c in lw.visible_chats() if c.same_as(target)), None)
    if row is None:
        print("畫面上找不到選定的聊天室（列表可能有變動），沒有做任何操作。")
        return 1

    before = {w.handle for w in line_top_windows()}
    l, t, r, b = row.rect
    mouse.right_click(coords=((l + r) // 2, (t + b) // 2))
    menu = wait_for(find_menu)
    if menu is None:
        print("右鍵選單沒有出現，沒有做任何操作。")
        return 1
    item = find_delete_item(menu)
    if item is None:
        keyboard.send_keys("{ESC}")
        print("選單中找不到唯一的「刪除」項目，已關閉選單，沒有做任何操作。")
        return 1

    ir = item.rectangle()
    mouse.click(coords=((ir.left + ir.right) // 2, (ir.top + ir.bottom) // 2))
    new_windows = wait_for(lambda: [w for w in line_top_windows()
                                    if w.handle not in before and w.element_info.class_name != MENU_CLASS])
    time.sleep(0.5)  # 等對話框畫完再截圖
    lines = []
    if new_windows:
        print(f"\n出現 {len(new_windows)} 個新視窗：")
    else:
        print("\n沒有出現新的頂層視窗（對話框可能畫在主視窗內，或已直接刪除）。")
    record_dialog(new_windows or [], lw.window, lines)
    write_lines("probe_confirm.txt", lines)

    keyboard.send_keys("{ESC}")
    time.sleep(1.0)
    still_open = [w for w in line_top_windows() if w.handle not in before]
    lw.focus()
    exists = any(c.same_as(target) for c in lw.visible_chats())

    print("\n== 結果 ==")
    print(f"  送出 Esc 後對話框{'仍開著，請手動按「取消」' if still_open else '已關閉'}")
    print(f"  選定的聊天室{'仍在列表中（未刪除）' if exists else '已不在畫面上，可能已被刪除'}")
    print(f"  詳細結構: probe_confirm.txt，截圖: {OUT_IMG_DIR}/confirm_*.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
