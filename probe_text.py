"""第二次探測：確認 LINE 介面文字能否透過 UIA 其他屬性取得，以及 OCR 辨識效果。

全程只讀取與截圖，不會點擊或修改 LINE 的任何東西。
用法：先開啟 LINE 並停在聊天列表，然後執行
    .venv\\Scripts\\python.exe probe_text.py
"""
import asyncio
import ctypes
import os
import time

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 讓截圖座標與 UIA 座標一致
except Exception:
    pass

from PIL import ImageGrab
from pywinauto.controls.uiawrapper import UIAWrapper
from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import DataWriter

from probe import MENU_COUNTDOWN, area, line_windows

TEXT_CLASSES = {"LcText", "LcTextField", "LcContextMenuItem"}
OCR_DIR = "probe_ocr"
OCR_SCALE = 2
out_lines = []


def log(text=""):
    print(text)
    out_lines.append(text)


def collect(info, found):
    """收集可能帶文字的元素。"""
    try:
        if info.class_name in TEXT_CLASSES or info.control_type == "ListItem":
            found.append(info)
        for child in info.children():
            collect(child, found)
    except Exception:
        pass


def text_sources(info):
    """嘗試各種 UIA 文字來源，回傳 {來源: 值}（只留非空值）。"""
    w = UIAWrapper(info)
    sources = {"name": lambda: info.name,
               "window_text": w.window_text,
               "legacy": lambda: {k: v for k, v in w.legacy_properties().items()
                                  if k in ("Name", "Value", "Description", "Help") and v},
               "value": lambda: w.iface_value.CurrentValue,
               "text_pattern": lambda: w.iface_text.DocumentRange.GetText(-1)}
    result = {}
    for key, getter in sources.items():
        try:
            value = getter()
        except Exception:
            continue
        if value:
            result[key] = value
    return result


def probe_properties(infos, label):
    log(f"-- {label}：UIA 文字屬性 --")
    hits = 0
    for info in infos:
        found = text_sources(info)
        if found:
            hits += 1
            log(f"  [{info.class_name or info.control_type}] {found}")
    log(f"  {len(infos)} 個元素中，有 {hits} 個取得文字")
    return hits


async def ocr_image(engine, img):
    img = img.resize((img.width * OCR_SCALE, img.height * OCR_SCALE)).convert("RGBA")
    writer = DataWriter()
    writer.write_bytes(img.tobytes("raw", "BGRA"))
    bmp = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.BGRA8, img.width, img.height)
    result = await engine.recognize_async(bmp)
    return ["".join(w.text for w in line.words) for line in result.lines]


def probe_ocr(infos, label, prefix):
    log(f"-- {label}：OCR --")
    engine = OcrEngine.try_create_from_user_profile_languages()
    os.makedirs(OCR_DIR, exist_ok=True)
    for i, info in enumerate(infos):
        r = info.rectangle
        img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True)
        img.save(os.path.join(OCR_DIR, f"{prefix}_{i}.png"))
        lines = asyncio.run(ocr_image(engine, img))
        log(f"  #{i} rect=({r.left},{r.top},{r.right},{r.bottom}) -> {lines}")


def main():
    log("== 第 1 部分：聊天列表 ==")
    wins = line_windows()
    if not wins:
        log("找不到 LINE 視窗。請確認 LINE 已開啟、已登入、且視窗沒有最小化。")
        return 1
    main_win = max(wins, key=lambda w: area(w.element_info))
    main_win.set_focus()  # 只把視窗帶到前景，確保截圖不被遮住
    time.sleep(0.5)
    found = []
    collect(main_win.element_info, found)
    list_hits = probe_properties(found, "主視窗")
    items = [i for i in found if i.control_type == "ListItem"]
    probe_ocr(items, "聊天列表項目", "chat")

    log("\n== 第 2 部分：右鍵選單 ==")
    input(f"按 Enter 後，請在 {MENU_COUNTDOWN} 秒內到 LINE 對任一聊天室按右鍵，"
          "並讓選單保持開啟（不要點任何選項）...")
    for i in range(MENU_COUNTDOWN, 0, -1):
        print(f"  {i}...", flush=True)
        time.sleep(1)
    menus = [w for w in line_windows() if w.element_info.class_name == "LcContextMenu"]
    menu_hits = 0
    if menus:
        found = []
        collect(menus[0].element_info, found)
        menu_hits = probe_properties(found, "右鍵選單")
        probe_ocr([i for i in found if i.class_name == "LcContextMenuItem"], "選單項目", "menu")
    else:
        log("  沒有偵測到右鍵選單（LcContextMenu）。")
    print("請回到 LINE 按 Esc 關閉右鍵選單。")

    log("\n== 判讀 ==")
    log(f"  UIA 可取得聊天列表文字: {'是' if list_hits else '否'}")
    log(f"  UIA 可取得選單文字: {'是' if menu_hits else '否'}")
    log(f"  OCR 結果請對照 {OCR_DIR}/ 內的截圖確認準確度")
    with open("probe_text.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
