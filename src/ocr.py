"""截圖、Windows OCR，以及辨識聊天室身份用的影像比對。"""
import asyncio

from PIL import Image, ImageChops, ImageGrab, ImageOps
from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import DataWriter

# (放大倍數, 白邊 px)，依序嘗試，取第一個有結果者；短名稱常需要較大倍數才讀得出來
OCR_VARIANTS = [(3, 40), (4, 60), (2, 30)]
TEXT_THRESHOLD = 110      # 灰階低於此值視為文字像素
MASK_TOLERANCE = 0.01     # 文字遮罩差異像素比例上限
MAX_SHIFT = 2             # 文字遮罩比對時容許的位移 px
AVATAR_TOLERANCE = 12     # 頭像縮圖平均灰階差上限（0–255）

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = OcrEngine.try_create_from_user_profile_languages()
        if _engine is None:
            raise RuntimeError("Windows OCR 無可用語言，請安裝繁體中文語言包")
    return _engine


def grab(rect):
    """擷取螢幕區域，rect 為 (left, top, right, bottom)。"""
    return ImageGrab.grab(bbox=rect, all_screens=True)


async def _recognize(img):
    img = img.convert("RGBA")
    writer = DataWriter()
    writer.write_bytes(img.tobytes("raw", "BGRA"))
    bmp = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.BGRA8, img.width, img.height)
    result = await _get_engine().recognize_async(bmp)
    return ["".join(w.text for w in line.words) for line in result.lines]


def _prepare(img, scale, pad):
    img = img.convert("RGB").resize((img.width * scale, img.height * scale), Image.LANCZOS)
    return ImageOps.expand(img, border=pad, fill="white")


def read_lines(img):
    """辨識圖片文字，回傳文字行（全部預處理都失敗則回傳空串列）。"""
    for scale, pad in OCR_VARIANTS:
        lines = asyncio.run(_recognize(_prepare(img, scale, pad)))
        if lines:
            return lines
    return []


def read_text(img):
    return "".join(read_lines(img))


def text_mask(img):
    """文字像素遮罩：忽略滑鼠移過、選取等背景色變化。"""
    return ImageOps.grayscale(img).point(lambda p: 255 if p < TEXT_THRESHOLD else 0)


def same_mask(a, b):
    """比對兩個文字遮罩，容許最多 MAX_SHIFT px 的位移（捲動後 UIA 座標會有 1px 捨入誤差）。"""
    if a.size != b.size:
        return False
    s = MAX_SHIFT
    inner = a.crop((s, s, a.width - s, a.height - s))
    pixels = inner.width * inner.height
    for dy in range(-s, s + 1):
        for dx in range(-s, s + 1):
            shifted = b.crop((s + dx, s + dy, s + dx + inner.width, s + dy + inner.height))
            diff = ImageChops.difference(inner, shifted).histogram()
            if sum(diff[1:]) / pixels < MASK_TOLERANCE:
                return True
    return False


def avatar_thumb(img):
    return ImageOps.grayscale(img).resize((16, 16), Image.BILINEAR)


def same_avatar(a, b):
    hist = ImageChops.difference(a, b).histogram()
    return sum(i * n for i, n in enumerate(hist)) / (a.width * a.height) < AVATAR_TOLERANCE
