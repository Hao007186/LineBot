import ctypes

# 必須在 pywinauto / 截圖之前設定，讓截圖座標與 UIA 座標一致
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass
