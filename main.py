"""LINE 電腦版批次刪除 / 隱藏聊天室工具：進入點。

用法：先開啟 LINE 並切到「聊天」分頁，然後執行
    .venv\\Scripts\\python.exe main.py
打包後的 exe 加上 --check 只做自我檢查（不操作 LINE），結果寫入 exe 旁的 check.txt。
"""
import sys


def self_check():
    """確認打包後各元件（tk、UIA、Windows OCR）都能載入，不操作 LINE。"""
    import os
    import tkinter as tk
    import traceback

    from src import gui
    lines = []
    try:
        from PIL import Image
        from pywinauto import Desktop

        from src import ocr
        lines.append(f"UIA 頂層視窗數：{len(Desktop(backend='uia').windows())}")
        ocr.read_lines(Image.new("RGB", (60, 20), "white"))
        lines.append("Windows OCR：OK")
        root = tk.Tk()
        root.destroy()
        lines.append("tkinter：OK")
        lines.append("RESULT: OK")
    except Exception:
        lines += [traceback.format_exc(), "RESULT: FAIL"]
    with open(os.path.join(gui.APP_DIR, "check.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0 if lines[-1] == "RESULT: OK" else 1


if __name__ == "__main__":
    if "--check" in sys.argv:
        raise SystemExit(self_check())
    from src.gui import main
    raise SystemExit(main())
