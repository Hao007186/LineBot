"""找 LINE 主視窗（找不到時自動開啟 LINE）、讀取聊天列表、捲動。

只會移動滑鼠與捲動列表，不會點擊或修改任何聊天室。
"""
import ctypes
import logging
import os
import time
from ctypes import wintypes
from dataclasses import dataclass, field

import win32con
import win32gui
import win32process
from PIL import Image, ImageChops
from pywinauto import Desktop, mouse

from . import ocr
from .stop_key import StopRequested, check_stop  # noqa: F401（StopRequested 供呼叫端使用）

LINE_EXE = "line.exe"
LAUNCHER_EXE = "LineLauncher.exe"
LAUNCH_TIMEOUT = 30           # 自動開啟 LINE 後等待主視窗出現的秒數
LAUNCH_SETTLE = 2.0
MAIN_CLASS = "AllInOneWindow"
LIST_CLASS = "LcListView"
BASE_ROW_HEIGHT = 90          # 100% 縮放時的列高，用來換算下列區域
NAME_BOX = (95, 15, 90, 45)   # 名稱區：左, 上, 距右邊界, 下
NAME_TAIL = 25                # 比對名稱時忽略名稱區右端的寬度（截斷成「…」的位置）
AVATAR_BOX = (18, 15, 78, 75)
SCROLL_NOTCHES = 2            # 每次往下捲的滾輪格數；捲過頭（與上一畫面沒有重疊）時改為 1
MIN_VISIBLE_ROWS = 3
SCROLL_WAIT = 0.4
MAX_SCROLLS = 1000

log = logging.getLogger("linebot")

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class LineNotFound(Exception):
    pass


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


def _line_launcher():
    """回傳 LINE 啟動程式路徑：優先用執行中 LINE 旁的 LineLauncher.exe，其次預設安裝位置。"""
    candidates = []
    for pid in win32process.EnumProcesses():
        path = _process_path(pid)
        if os.path.basename(path).lower() == LINE_EXE:   # ...\LINE\bin\current\LINE.exe
            candidates += [os.path.join(os.path.dirname(os.path.dirname(path)), LAUNCHER_EXE), path]
            break
    candidates.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "LINE", "bin", LAUNCHER_EXE))
    return next((p for p in candidates if os.path.isfile(p)), None)


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
        tail = round(NAME_TAIL * self.row_img.height / BASE_ROW_HEIGHT)
        return (ocr.same_avatar(self.avatar, other.avatar)
                and ocr.same_mask(self.name_mask, other.name_mask, tail))


@dataclass
class LineWindow:
    window: object = None
    warnings: list = field(default_factory=list)

    def __post_init__(self):
        if self.window is None:
            try:
                self.window = self._find()
            except LineNotFound:
                self.window = self._launch()

    @staticmethod
    def _find():
        for w in Desktop(backend="uia").windows(class_name=MAIN_CLASS):
            path = _process_path(w.element_info.process_id)
            if os.path.basename(path).lower() == LINE_EXE:
                return w
        raise LineNotFound("找不到 LINE 主視窗，請確認 LINE 已開啟並登入")

    @classmethod
    def _launch(cls):
        """LINE 縮到系統匣時主視窗會被銷毀，沒開時則根本不存在：執行 LINE 啟動程式把它叫出來。

        LINE 只允許單一執行個體，已在執行時再啟動一次會叫出原本的視窗。
        """
        launcher = _line_launcher()
        if launcher is None:
            raise LineNotFound("找不到 LINE 主視窗，也找不到 LINE 的安裝位置，請手動開啟 LINE")
        log.info("找不到 LINE 主視窗（可能縮在系統匣或未開啟），自動開啟 LINE…")
        os.startfile(launcher)
        end = time.time() + LAUNCH_TIMEOUT
        while time.time() < end:
            time.sleep(0.5)
            try:
                window = cls._find()
            except LineNotFound:
                continue
            time.sleep(LAUNCH_SETTLE)  # 等聊天列表載入
            return window
        raise LineNotFound(f"已嘗試開啟 LINE，但 {LAUNCH_TIMEOUT} 秒內沒有出現主視窗，請確認 LINE 已登入")

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
        self.warnings.clear()
        self.focus()
        self.scroll_to_top()
        chats, prev_visible = [], []
        notches, max_visible = SCROLL_NOTCHES, 0
        for _ in range(MAX_SCROLLS):
            check_stop()
            visible = self.visible_chats()
            max_visible = max(max_visible, len(visible))
            if prev_visible and not any(v.same_as(p) for v in visible for p in prev_visible):
                if notches > 1:
                    # 捲過頭：退回上一畫面，改用較小的捲動量重讀
                    self.scroll(notches)
                    notches = 1
                    self.scroll(-notches)
                    continue
                self.warnings.append(f"第 {len(chats)} 筆附近捲動後與上一畫面沒有重疊，可能漏讀")
            for chat in visible:
                # 重複項目通常在上一畫面，從最近讀到的開始比對
                if not any(chat.same_as(known) for known in reversed(chats)):
                    chat.name = ocr.read_text(chat.name_img)
                    chats.append(chat)
                    if on_new:
                        on_new(chat)
            prev_visible = visible
            if not self.scroll(-notches):
                break
        if max_visible < MIN_VISIBLE_ROWS < len(chats):
            self.warnings.insert(0, f"LINE 視窗太矮，聊天列表一次只顯示 {max_visible} 列，建議把 LINE 視窗拉高")
        return chats
