"""對單一聊天室執行刪除或隱藏：定位 → 右鍵 → 選單項目 → （確認對話框） → 驗證已從列表消失。

每一步都以 OCR 確認文字後才點擊；任何一步對不上就按 Esc 取消並丟出 ActionFailed，不會盲點。
"""
import os
import time
from dataclasses import dataclass

from PIL import ImageOps
from pywinauto import Desktop, keyboard, mouse

from . import ocr
from .line_window import LINE_EXE, MAX_SCROLLS, StopRequested, _box, _process_path, check_stop

MENU_CLASS = "LcContextMenu"
MENU_ITEM_CLASS = "LcContextMenuItem"
ALERT_CLASS = "AlertWindow"
WAIT_TIMEOUT = 3.0
OPTIONAL_ALERT_WAIT = 1.5     # 不一定會出現的確認對話框，等這麼久沒出現就當作沒有
AFTER_ACTION_WAIT = 1.0
# OCR 常見的異體字 / 簡體誤讀，比對前換回繁體（實測：按鈕「隱藏」被讀成「隠藏」）
OCR_FIXES = str.maketrans({"隠": "隱", "删": "刪"})


@dataclass(frozen=True)
class Action:
    name: str             # 顯示用動詞
    text: str             # 選單項目 / 確認按鈕要含的文字
    confirm_required: bool


DELETE = Action("刪除", "刪除", confirm_required=True)
HIDE = Action("隱藏", "隱藏", confirm_required=True)    # 實測：會跳出確認對話框（按鈕「隱藏」／「取消」）


class ActionFailed(Exception):
    pass


class NotAvailable(ActionFailed):
    """右鍵選單本來就沒有這個項目（例如 Keep 筆記沒有「刪除」），重試也沒用。"""


def _line_top_windows():
    return [w for w in Desktop(backend="uia").windows()
            if os.path.basename(_process_path(w.element_info.process_id)).lower() == LINE_EXE]


def _wait_for(fn, timeout=WAIT_TIMEOUT):
    end = time.time() + timeout
    while time.time() < end:
        result = fn()
        if result:
            return result
        time.sleep(0.2)
    return None


def _center(box):
    left, top, right, bottom = box
    return ((left + right) // 2, (top + bottom) // 2)


def _press_esc():
    keyboard.send_keys("{ESC}")


def _click(el, still_open):
    """點擊元素中心；點之前確認沒有被要求中止、且選單或對話框仍開著，避免點到底下的東西。"""
    check_stop()
    if not still_open():
        raise ActionFailed("選單或對話框已被關閉，取消這次點擊")
    mouse.click(coords=_center(_box(el.rectangle())))


def _read(el):
    """辨識元素文字；綠底白字的按鈕讀不到時改用反相影像再試。"""
    img = ocr.grab(_box(el.rectangle()))
    text = ocr.read_text(img) or ocr.read_text(ImageOps.invert(img.convert("RGB")))
    return text.translate(OCR_FIXES)


def _visible_match(lw, chat):
    return next((c for c in lw.visible_chats() if c.same_as(chat)), None)


def locate(lw, chat):
    """找到聊天室目前在畫面上的那一列；先看目前畫面，找不到再從頂端往下捲。"""
    row = _visible_match(lw, chat)
    if row:
        return row
    lw.scroll_to_top()
    for _ in range(MAX_SCROLLS):
        check_stop()
        row = _visible_match(lw, chat)
        if row or not lw.scroll(-1):  # 每次只捲 1 格，避免視窗矮時跳過目標
            return row
    return None


def _find_menu():
    menus = [w for w in _line_top_windows() if w.element_info.class_name == MENU_CLASS]
    return menus[0] if menus else None


def _find_alert(lw):
    alerts = [w for w in lw.window.children(control_type="Window", class_name=ALERT_CLASS)
              if w.rectangle().height() > 0]
    return alerts[0] if alerts else None


def _visible(elements):
    return [el for el in elements if el.rectangle().height() > 0]


def _only(elements, what, text, is_menu=False):
    """從元素中找出唯一一個含 text 者；找不到或不唯一就按 Esc 取消。"""
    texts = [(el, _read(el)) for el in elements]
    found = [el for el, t in texts if text in t]
    if len(found) != 1:
        _press_esc()
        seen = "、".join(t or "(無法辨識)" for _, t in texts)
        if is_menu and not found and all(t for _, t in texts):
            # 每一項都讀得出來卻沒有目標文字：LINE 不提供這個動作，不是辨識失敗
            raise NotAvailable(f"{what}沒有「{text}」選項（只有：{seen}），此聊天室無法{text}"
                               "（例如 Keep 筆記無法刪除，可改用隱藏）")
        raise ActionFailed(f"{what}中找不到唯一的「{text}」（讀到：{seen}），已取消")
    return found[0]


def _click_menu_item(row, action):
    check_stop()
    mouse.right_click(coords=_center(row.rect))
    menu = _wait_for(_find_menu)
    if menu is None:
        raise ActionFailed("右鍵選單沒有出現")
    item = _only(_visible(menu.descendants(class_name=MENU_ITEM_CLASS)), "右鍵選單", action.text, is_menu=True)
    _click(item, lambda: _find_menu() is not None)


def _click_confirm(lw, action):
    timeout = WAIT_TIMEOUT if action.confirm_required else OPTIONAL_ALERT_WAIT
    alert = _wait_for(lambda: _find_alert(lw), timeout)
    if alert is None:
        if action.confirm_required:
            raise ActionFailed("確認對話框沒有出現")
        return
    message = "".join(_read(el) for el in _visible(alert.descendants(class_name="LcText")))
    if action.text not in message:
        _press_esc()
        raise ActionFailed(f"對話框內容不是{action.name}確認（讀到：{message or '(無法辨識)'}），已取消")
    button = _only(_visible(alert.descendants(class_name="LcButton")), "確認對話框", action.text)
    _click(button, lambda: _find_alert(lw) is not None)
    if not _wait_for(lambda: _find_alert(lw) is None):
        raise ActionFailed(f"按下{action.name}後對話框沒有關閉")


def apply(lw, chat, action, retries=1):
    """對一個聊天室執行動作並驗證已從列表消失；失敗時重試，仍失敗則丟出 ActionFailed。"""
    error = None
    for attempt in range(retries + 1):
        check_stop()
        lw.focus()
        row = locate(lw, chat)
        if row is None:
            if attempt > 0:
                return  # 上一輪已按下，只是驗證或對話框關閉逾時
            raise ActionFailed("列表中找不到這個聊天室")
        try:
            _click_menu_item(row, action)
            _click_confirm(lw, action)
        except NotAvailable:
            raise
        except ActionFailed as e:
            error = e
            continue
        except StopRequested:
            if _find_menu() or _find_alert(lw):
                _press_esc()  # 中止時把開著的選單或對話框關掉，不按確認
            raise
        time.sleep(AFTER_ACTION_WAIT)
        if _visible_match(lw, chat) is None:
            return
        error = ActionFailed(f"{action.name}後聊天室仍在列表中")
    raise error


def delete_chat(lw, chat, retries=1):
    apply(lw, chat, DELETE, retries)
