"""批次刪除 / 隱藏 LINE 聊天室的 GUI（tkinter）。

所有操作 LINE 的工作都在單一常駐背景執行緒（Worker）執行，GUI 執行緒只處理畫面，兩者以佇列溝通。
執行前本視窗會移到不擋住 LINE 的位置；螢幕上沒有空間時改為最小化，結束後再還原。
log 寫入 out/logs/（含聊天室名稱，不進版控）。
"""
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

import pythoncom
import win32api
import win32gui
from PIL import ImageTk

from .actions import DELETE, HIDE, ActionFailed, apply
from .line_window import LineNotFound, LineWindow
from .stop_key import StopRequested, check_stop, request_stop, watch_stop_key

# 打包成 exe 時 __file__ 在暫存資料夾（結束即刪除），log 改寫到 exe 旁邊
APP_DIR = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
           else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG_DIR = os.path.join(APP_DIR, "out", "logs")
TITLE = "LINE 批次刪除 / 隱藏聊天室"
ACTIONS = {"delete": DELETE, "hide": HIDE}
UNREAD = "(無法辨識)"
POLL_MS = 100
AUTO_READ_MS = 800
THUMB_SCALE = 0.5
GUI_WIDTH = 520
MIN_GUI_WIDTH = 380
MIN_GUI_HEIGHT = 400
LINE_MARGIN = 220          # LINE 右側保留給右鍵選單的寬度
EDGE_GAP = 10
FRAME_W, FRAME_H = 20, 60  # tk geometry 不含標題列與邊框，預留其大小
CONFIRM_LIST_MAX = 15

log = logging.getLogger("linebot")


class _EventHandler(logging.Handler):
    """把 log 轉成 GUI 事件，讓 GUI 執行緒顯示。"""

    def __init__(self, events):
        super().__init__()
        self.events = events

    def emit(self, record):
        self.events.put(("log", record.levelno, self.format(record)))


def _sleep(seconds):
    """可被中止的 sleep。"""
    end = time.time() + seconds
    while True:
        check_stop()
        remaining = end - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


def _free_area(line_rect):
    """找一塊不與 LINE 視窗（含右側選單空間）重疊、放得下本視窗的區域，回傳 (x, y, w, h) 或 None。"""
    left, top, right, bottom = line_rect
    right += LINE_MARGIN
    best = None
    for hmon, _, _ in win32api.EnumDisplayMonitors():
        ml, mt, mr, mb = win32api.GetMonitorInfo(hmon)["Work"]
        if right <= ml or left >= mr or bottom <= mt or top >= mb:
            regions = [(ml, mt, mr, mb, "left")]       # 整個螢幕都沒和 LINE 重疊
        else:
            regions = [(ml, mt, min(left, mr), mb, "right"),   # LINE 左側，貼齊 LINE
                       (max(right, ml), mt, mr, mb, "left")]   # LINE 右側
        for rl, rt, rr, rb, align in regions:
            w = rr - rl - 2 * EDGE_GAP - FRAME_W
            h = rb - rt - 2 * EDGE_GAP - FRAME_H
            if w < MIN_GUI_WIDTH or h < MIN_GUI_HEIGHT or (best and w <= best[2]):
                continue
            w = min(w, GUI_WIDTH)
            x = rl + EDGE_GAP if align == "left" else rr - EDGE_GAP - FRAME_W - w
            best = (x, rt + EDGE_GAP, w, h)
    return best


class Worker(threading.Thread):
    """依序執行 GUI 交付的工作；所有 UIA / 滑鼠 / OCR 操作都在這個執行緒。"""

    def __init__(self, events):
        super().__init__(daemon=True)
        self.tasks = queue.Queue()
        self.events = events
        self.lw = None

    def emit(self, *event):
        self.events.put(event)

    def submit(self, fn, *args):
        self.tasks.put((fn, args))

    def run(self):
        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
        while True:
            fn, args = self.tasks.get()
            try:
                fn(*args)
            except LineNotFound as e:
                log.error("%s", e)
                self.emit("alert", str(e))
            except StopRequested as e:
                log.warning("%s", e)
            except Exception:
                log.exception("未預期的錯誤")
            finally:
                self.emit("finished")

    def _prepare(self):
        """找到並聚焦 LINE，等 GUI 讓開 LINE 視窗後再聚焦一次。"""
        self.lw = LineWindow()
        self.lw.focus()
        moved = threading.Event()
        self.emit("avoid", win32gui.GetWindowRect(self.lw.window.handle), moved)
        moved.wait()
        self.lw.focus()

    def read_list(self):
        self._prepare()
        log.info("開始讀取聊天列表")
        with watch_stop_key():
            chats = self.lw.read_all(on_new=lambda c: self.emit("chat", c))
        for w in self.lw.warnings:
            log.warning("%s", w)
        unread = sum(1 for c in chats if not c.name)
        log.info("讀取完成，共 %d 個聊天室（無法辨識名稱：%d）", len(chats), unread)

    def process(self, targets, action, delay):
        self._prepare()
        total, done, failed = len(targets), 0, 0
        log.info("開始%s %d 個聊天室，每筆間隔 %.1f 秒", action.name, total, delay)
        try:
            with watch_stop_key():
                for n, chat in enumerate(targets, 1):
                    label = chat.name or UNREAD
                    try:
                        apply(self.lw, chat, action)
                        done += 1
                        log.info("(%d/%d) 已%s：%s", n, total, action.name, label)
                        self.emit("done", chat)
                    except ActionFailed as e:
                        failed += 1
                        log.warning("(%d/%d) 跳過：%s（%s）", n, total, label, e)
                        self.emit("failed", chat, str(e))
                    self.emit("progress", n, total)
                    if n < total:
                        _sleep(delay)
        finally:
            log.info("完成 %d 筆，失敗 %d 筆，未處理 %d 筆", done, failed, total - done - failed)


@dataclass
class Row:
    chat: object
    var: tk.BooleanVar
    frame: ttk.Frame
    check: ttk.Checkbutton
    status: ttk.Label
    photo: ImageTk.PhotoImage   # 需保留參照，否則圖片會被回收
    shown: bool = False


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.rows = []
        self.row_of = {}
        self.running = False
        self.iconified = False
        self.close_pending = False
        self.closed = False
        self.log_path = self._setup_logging()
        self._build()
        self.worker = Worker(self.events)
        self.worker.start()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(POLL_MS, self._poll)
        log.info("log 檔：%s", self.log_path)
        root.after(AUTO_READ_MS, self._start_read)  # 啟動後自動讀取；讀取只會捲動，不會修改聊天室

    # ---- 建立畫面 ----

    def _setup_logging(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, time.strftime("%Y%m%d-%H%M%S") + ".log")
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        gui_handler = _EventHandler(self.events)
        gui_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        log.setLevel(logging.INFO)
        log.addHandler(file_handler)
        log.addHandler(gui_handler)
        return path

    def _build(self):
        root = self.root
        root.title(TITLE)
        root.geometry(f"{GUI_WIDTH}x760")
        root.minsize(MIN_GUI_WIDTH, MIN_GUI_HEIGHT)
        self.controls = []

        top = ttk.Frame(root, padding=(8, 8, 8, 0))
        top.pack(fill="x")
        self.read_btn = ttk.Button(top, text="重新讀取", command=self._start_read)
        self.read_btn.pack(side="left")
        ttk.Label(top, text="篩選：").pack(side="left", padx=(12, 0))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._apply_filter())
        filter_entry = ttk.Entry(top, textvariable=self.filter_var)
        filter_entry.pack(side="left", fill="x", expand=True)
        self.controls += [self.read_btn, filter_entry]

        sel = ttk.Frame(root, padding=(8, 4, 8, 0))
        sel.pack(fill="x")
        for text, fn in [("全選", lambda: self._select(lambda r: True)),
                         ("全不選", lambda: self._select(lambda r: False)),
                         ("反選", lambda: self._select(lambda r: not r.var.get()))]:
            btn = ttk.Button(sel, text=text, command=fn, width=6)
            btn.pack(side="left", padx=(0, 4))
            self.controls.append(btn)
        ttk.Label(sel, text="（只作用於篩選後顯示的項目）", foreground="gray").pack(side="left")

        self._build_list(root)

        self.count_label = ttk.Label(root, padding=(8, 2))
        self.count_label.pack(fill="x")

        opts = ttk.LabelFrame(root, text="執行方式", padding=(8, 4))
        opts.pack(fill="x", padx=8)
        action_row = ttk.Frame(opts)
        action_row.pack(anchor="w", pady=(0, 4))
        ttk.Label(action_row, text="動作：").pack(side="left")
        self.action = tk.StringVar(value="delete")
        self.action.trace_add("write", lambda *_: self._update_count())
        for value, text in [("delete", "刪除（手機也會一起刪除）"), ("hide", "隱藏（僅電腦版）")]:
            rb = ttk.Radiobutton(action_row, text=text, value=value, variable=self.action)
            rb.pack(side="left", padx=(0, 8))
            self.controls.append(rb)
        self.mode = tk.StringVar(value="selected")
        self.mode.trace_add("write", lambda *_: self._update_count())
        for value, text in [("selected", "處理勾選的聊天室"),
                            ("keep", "保留勾選的聊天室，處理其餘全部（白名單）")]:
            rb = ttk.Radiobutton(opts, text=text, value=value, variable=self.mode)
            rb.pack(anchor="w")
            self.controls.append(rb)
        delay_row = ttk.Frame(opts)
        delay_row.pack(anchor="w", pady=(4, 0))
        ttk.Label(delay_row, text="每筆間隔（秒）：").pack(side="left")
        self.delay_var = tk.StringVar(value="1.0")
        delay_box = ttk.Spinbox(delay_row, from_=0, to=30, increment=0.5,
                                textvariable=self.delay_var, width=6)
        delay_box.pack(side="left")
        self.controls.append(delay_box)

        run = ttk.Frame(root, padding=(8, 6, 8, 0))
        run.pack(fill="x")
        self.start_btn = ttk.Button(run, text="開始執行", command=self._start_action)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(run, text="停止（Esc）", command=request_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.progress = ttk.Progressbar(run, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.controls.append(self.start_btn)
        self.status = ttk.Label(root, padding=(8, 2), text="即將自動讀取 LINE 聊天列表…")
        self.status.pack(fill="x")

        log_frame = ttk.Frame(root, padding=(8, 0, 8, 8))
        log_frame.pack(fill="x")
        self.log_text = tk.Text(log_frame, height=8, state="disabled", wrap="word")
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="x", expand=True)
        self.log_text.tag_configure("warn", foreground="#b00020")

        self._update_count()

    def _build_list(self, root):
        outer = ttk.Frame(root, padding=(8, 4, 8, 0))
        outer.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(outer, highlightthickness=1, highlightbackground="#ccc")
        scroll = ttk.Scrollbar(outer, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.list_frame = ttk.Frame(self.canvas)
        window = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.list_frame.bind("<Configure>",
                             lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _on_wheel(self, event):
        """游標在清單（含其中的列）上時捲動清單；其他元件照自己的預設行為。"""
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        if widget is not None and str(widget).startswith(str(self.canvas)):
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

    # ---- 清單 ----

    def _add_row(self, chat):
        img = chat.row_img
        thumb = img.resize((max(1, round(img.width * THUMB_SCALE)), max(1, round(img.height * THUMB_SCALE))))
        photo = ImageTk.PhotoImage(thumb)
        var = tk.BooleanVar(value=False)
        var.trace_add("write", lambda *_: self._update_count())
        frame = ttk.Frame(self.list_frame, padding=(4, 2))
        check = ttk.Checkbutton(frame, variable=var)
        check.pack(side="left")
        if self.running:
            check.state(["disabled"])
        pic = ttk.Label(frame, image=photo)
        pic.pack(side="left")
        text = ttk.Frame(frame)
        text.pack(side="left", fill="x", expand=True, padx=6)
        name = ttk.Label(text, text=chat.name or UNREAD, foreground="" if chat.name else "gray")
        name.pack(anchor="w")
        status = ttk.Label(text, foreground="#b00020")
        status.pack(anchor="w")
        row = Row(chat=chat, var=var, frame=frame, check=check, status=status, photo=photo)
        for widget in (frame, pic, text, name, status):
            widget.bind("<Button-1>", lambda e, r=row: self._toggle(r))
        self.rows.append(row)
        self.row_of[id(chat)] = row
        if self._matches(row):
            frame.pack(fill="x")
            row.shown = True
        self._update_count()

    def _clear_rows(self):
        for row in self.rows:
            row.frame.destroy()
        self.rows.clear()
        self.row_of.clear()
        self._update_count()

    def _remove_row(self, row):
        row.frame.destroy()
        self.rows.remove(row)
        del self.row_of[id(row.chat)]
        self._update_count()

    def _toggle(self, row):
        if not self.running:
            row.var.set(not row.var.get())

    def _matches(self, row):
        keyword = self.filter_var.get().strip().lower()
        return not keyword or keyword in (row.chat.name or "").lower()

    def _apply_filter(self):
        for row in self.rows:
            row.frame.pack_forget()
            row.shown = self._matches(row)
            if row.shown:
                row.frame.pack(fill="x")
        self.canvas.yview_moveto(0)

    def _select(self, rule):
        for row in self.rows:
            if row.shown:
                row.var.set(rule(row))

    def _targets(self):
        keep = self.mode.get() == "keep"
        if keep:
            return [r.chat for r in self.rows if not r.var.get()]
        return [r.chat for r in self.rows if r.var.get()]

    def _update_count(self):
        checked = sum(r.var.get() for r in self.rows)
        name = ACTIONS[self.action.get()].name
        self.count_label.configure(
            text=f"共 {len(self.rows)} 個，已勾選 {checked} 個 → 將{name} {len(self._targets())} 個")

    # ---- 執行 ----

    def _set_running(self, running):
        self.running = running
        state = ["disabled"] if running else ["!disabled"]
        for widget in self.controls:
            widget.state(state)
        for row in self.rows:
            row.check.state(state)
        self.stop_btn.state(["!disabled"] if running else ["disabled"])

    def _start_read(self):
        if self.rows and not messagebox.askyesno(TITLE, "重新讀取會清除目前的清單與勾選，確定嗎？"):
            return
        self._clear_rows()
        self.progress.configure(value=0)
        self.status.configure(text="讀取中…執行期間請勿操作滑鼠鍵盤；按 Esc 可中止")
        self._set_running(True)
        self.worker.submit(self.worker.read_list)

    def _start_action(self):
        action = ACTIONS[self.action.get()]
        if not self.rows:
            messagebox.showinfo(TITLE, "請先讀取聊天列表")
            return
        try:
            delay = float(self.delay_var.get())
            if not 0 <= delay <= 60:
                raise ValueError
        except ValueError:
            messagebox.showerror(TITLE, "每筆間隔請輸入 0–60 的秒數")
            return
        targets = self._targets()
        if not targets:
            messagebox.showinfo(TITLE, f"沒有要{action.name}的聊天室")
            return
        if not self._confirm(targets, action):
            return
        self.progress.configure(value=0, maximum=len(targets))
        self.status.configure(text=f"{action.name}中 0/{len(targets)}…執行期間請勿操作滑鼠鍵盤；按 Esc 可中止")
        self._set_running(True)
        self.worker.submit(self.worker.process, targets, action, delay)

    def _confirm(self, targets, action):
        lines = []
        if self.mode.get() == "keep":
            kept = len(self.rows) - len(targets)
            lines.append(f"白名單模式：保留勾選的 {kept} 個，{action.name}其餘全部。")
            if kept == 0:
                lines.append(f"⚠ 沒有勾選任何要保留的聊天室，將{action.name}清單中的全部聊天室！")
        lines.append(f"\n即將{action.name} {len(targets)} 個聊天室：")
        lines += [f"  • {c.name or UNREAD}" for c in targets[:CONFIRM_LIST_MAX]]
        if len(targets) > CONFIRM_LIST_MAX:
            lines.append(f"  …以及其他 {len(targets) - CONFIRM_LIST_MAX} 個")
        if action is DELETE:
            lines += ["",
                      "⚠ 電腦版刪除的聊天室，手機上也會一起被刪除，且無法復原。",
                      "　建議先在手機備份聊天記錄。"]
        else:
            lines += ["", "隱藏只會從電腦版聊天列表移除，聊天記錄仍會保留，手機上不會跟著隱藏。"]
        lines += ["",
                  "執行期間請勿操作滑鼠鍵盤；按 Esc 可隨時中止。",
                  f"確定要開始{action.name}嗎？"]
        return messagebox.askyesno(TITLE, "\n".join(lines), icon="warning", default="no")

    def _on_close(self):
        if not self.running:
            self._close()
            return
        self.close_pending = True
        request_stop()
        self.status.configure(text="正在中止，完成後會自動關閉…")

    # ---- 背景執行緒事件 ----

    def _poll(self):
        try:
            while not self.closed:
                event, *args = self.events.get_nowait()
                getattr(self, "_on_" + event)(*args)
        except queue.Empty:
            pass
        if not self.closed:
            self.root.after(POLL_MS, self._poll)

    def _on_log(self, level, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n", "warn" if level >= logging.WARNING else ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_alert(self, text):
        messagebox.showerror(TITLE, text)

    def _on_avoid(self, line_rect, moved):
        """把本視窗移到不擋住 LINE 的位置；沒有空間就最小化。"""
        area = _free_area(line_rect)
        if area:
            self.root.state("normal")  # 最大化時 geometry 不會生效
            x, y, w, h = area
            self.root.geometry(f"{w}x{h}+{x}+{y}")
        else:
            log.info("螢幕上沒有不擋住 LINE 的空間，執行期間先將本視窗最小化")
            self.root.iconify()
            self.iconified = True
        self.root.update()
        moved.set()

    def _on_chat(self, chat):
        self._add_row(chat)
        self.status.configure(text=f"讀取中…已讀到 {len(self.rows)} 個；按 Esc 可中止")

    def _on_done(self, chat):
        row = self.row_of.get(id(chat))
        if row:
            self._remove_row(row)

    def _on_failed(self, chat, reason):
        row = self.row_of.get(id(chat))
        if row:
            row.status.configure(text=f"失敗：{reason}")

    def _on_progress(self, n, total):
        self.progress.configure(value=n, maximum=total)
        self.status.configure(text=f"執行中 {n}/{total}…按 Esc 可中止")

    def _on_finished(self):
        self._set_running(False)
        if self.iconified:
            self.root.deiconify()
            self.iconified = False
        self.root.lift()
        self.status.configure(text=f"已結束，詳見下方 log（{os.path.basename(self.log_path)}）")
        if self.close_pending:
            self._close()

    def _close(self):
        self.closed = True
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0
