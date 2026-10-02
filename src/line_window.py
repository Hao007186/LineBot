"""找 LINE 主視窗、讀取聊天列表、捲動。

只會移動滑鼠與捲動列表，不會點擊或修改任何聊天室。
"""
import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass, field

import win32con
import win32gui
from PIL import Image, ImageChops
from pywinauto import Desktop, mouse

from . import ocr

LINE_EXE = "line.exe"
MAIN_CLASS = "AllInOneWindow"
LIST_CLASS = "LcListView"
BASE_ROW_HEIGHT = 90          # 100% 縮放時的列高，用來換算下列區域
NAME_BOX = (95, 15, 90, 45)   # 名稱區：左, 上, 距右邊界, 下
AVATAR_BOX = (18, 15, 78, 75)
SCROLL_NOTCHES = 2            # 每次往下捲的滾輪格數，需小於一個畫面高度
SCROLL_WAIT = 0.4
MAX_SCROLLS = 1000
VK_ESCAPE = 0x1B

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class LineNotFound(Exception):
    pass


class StopRequested(Exception):
    pass


def check_stop():
    """使用者按下 Esc 就中止。"""
    if _user32.GetAsyncKeyState(VK_ESCAPE) & 0x8001:
        raise StopRequested("使用者按下 Esc 中止")


def _process_path(pid):
    handle = _kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        ok = _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        return buf.value if ok else ""
    finally:
        _kernel32.CloseHandle(handle)


def _box(rect):
    return (rect.left, rect.top, rect.right, rect.bottom)


@dataclass
class Chat:
    row_img: Image.Image      # 整列截圖，供 GUI 顯示
    name_img: Image.Image
    name_mask: Image.Image
    avatar: Image.Image
    rect: tuple               # 最後一次看到的螢幕座標
    name: str = ""            # OCR 結果，僅供顯示與搜尋

    def same_as(self, other):
        """以頭像 + 名稱文字遮罩判斷是否為同一聊天室（不依賴 OCR 文字）。頭像比對較快，先做。"""
        return ocr.same_avatar(self.avatar, other.avatar) and ocr.same_mask(self.name_mask, other.name_mask)


@dataclass
class LineWindow:
    window: object = None
    warnings: list = field(default_factory=list)

    def __post_init__(self):
        if self.window is None:
            self.window = self._find()

    @staticmethod
    def _find():
        for w in Desktop(backend="uia").windows(class_name=MAIN_CLASS):
            path = _process_path(w.element_info.process_id)
            if os.path.basename(path).lower() == LINE_EXE:
                return w
        raise LineNotFound("找不到 LINE 主視窗，請確認 LINE 已開啟並登入")

    def focus(self):
        hwnd = self.window.handle
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        self.window.set_focus()
        time.sleep(0.3)

    def chat_list(self):
        lists = [l for l in self.window.descendants(control_type="List", class_name=LIST_CLASS)
                 if l.rectangle().height() > 0]
        if not lists:
            raise LineNotFound("找不到聊天列表，請在 LINE 切換到「聊天」分頁")
        return lists[0]

    def _move_mouse_away(self):
        """把游標移出 LINE 視窗，避免滑鼠移過的反白影響截圖。"""
        r = self.window.rectangle()
        screen_right = _user32.GetSystemMetrics(76) + _user32.GetSystemMetrics(78)  # 虛擬螢幕右緣
        x = r.right + 40 if r.right + 40 < screen_right else r.left - 40
        mouse.move(coords=(x, r.top + 40))

    def _capture_row(self, rect):
        img = ocr.grab(rect)
        s = img.height / BASE_ROW_HEIGHT
        left, top, right_margin, bottom = NAME_BOX
        name_img = img.crop((round(left * s), round(top * s),
                             img.width - round(right_margin * s), round(bottom * s)))
        avatar_img = img.crop(tuple(round(v * s) for v in AVATAR_BOX))
        return Chat(row_img=img, name_img=name_img, name_mask=ocr.text_mask(name_img),
                    avatar=ocr.avatar_thumb(avatar_img), rect=rect)

    def visible_chats(self):
        """回傳完整顯示在列表範圍內的聊天室（尚未 OCR）。"""
        lst = self.chat_list()
        lr = lst.rectangle()
        self._move_mouse_away()
        chats = []
        for item in lst.children(control_type="ListItem"):
            r = item.rectangle()
            if r.height() > 0 and r.top >= lr.top and r.bottom <= lr.bottom:
                chats.append(self._capture_row(_box(r)))
        return chats

    def _list_snapshot(self):
        self._move_mouse_away()
        return ocr.grab(_box(self.chat_list().rectangle())).convert("RGB")

    def scroll(self, notches):
        """捲動列表（正數往上、負數往下），回傳列表是否有移動。"""
        before = self._list_snapshot()
        r = self.chat_list().rectangle()
        mouse.scroll(coords=((r.left + r.right) // 2, (r.top + r.bottom) // 2), wheel_dist=notches)
        time.sleep(SCROLL_WAIT)
        after = self._list_snapshot()
        return before.size != after.size or ImageChops.difference(before, after).getbbox() is not None

    def scroll_to_top(self):
        for _ in range(MAX_SCROLLS):
            check_stop()
            if not self.scroll(10):
                return

    def read_all(self, on_new=None):
        """從頂端捲到底，回傳所有聊天室（依列表順序、已去重、已 OCR）。"""
        check_stop()  # 清掉先前殘留的 Esc 狀態
        self.warnings.clear()
        self.focus()
        self.scroll_to_top()
        chats, prev_visible = [], []
        for _ in range(MAX_SCROLLS):
            check_stop()
            visible = self.visible_chats()
            if prev_visible and not any(v.same_as(p) for v in visible for p in prev_visible):
                self.warnings.append(f"第 {len(chats)} 筆附近捲動後與上一畫面沒有重疊，可能漏讀")
            for chat in visible:
                # 重複項目通常在上一畫面，從最近讀到的開始比對
                if not any(chat.same_as(known) for known in reversed(chats)):
                    chat.name = ocr.read_text(chat.name_img)
                    chats.append(chat)
                    if on_new:
                        on_new(chat)
            prev_visible = visible
            if not self.scroll(-SCROLL_NOTCHES):
                break
        return chats
