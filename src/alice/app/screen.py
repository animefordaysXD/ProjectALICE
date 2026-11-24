"""Local screen text capture through Windows OCR.

The screenshot exists only in memory and is never written to disk. OCR text is
returned for local redaction; the caller decides whether a redacted excerpt may
be sent to the configured model.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import logging
import re
import sys
from dataclasses import dataclass
from ctypes import wintypes

logger = logging.getLogger(__name__)

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0
SM_CXSCREEN = 0
SM_CYSCREEN = 1
PROCESS_PER_MONITOR_DPI_AWARE = 2


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


@dataclass(frozen=True)
class ScreenText:
    text: str
    width: int
    height: int
    digest: str


def capture_screen_text() -> ScreenText | None:
    if sys.platform != "win32":
        return None
    try:
        raw, width, height = _grab_primary()
        if width <= 0 or height <= 0:
            return None
        text = _ocr(raw, width, height)
        clean = _clean(text)
        if not clean:
            return None
        return ScreenText(
            text=clean,
            width=width,
            height=height,
            digest=hashlib.sha1(clean.encode("utf-8")).hexdigest(),
        )
    except Exception:
        logger.exception("screen OCR failed")
        return None


def _grab_primary() -> tuple[bytes, int, int]:
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    width = int(user32.GetSystemMetrics(SM_CXSCREEN))
    height = int(user32.GetSystemMetrics(SM_CYSCREEN))
    if width <= 0 or height <= 0:
        return b"", 0, 0

    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.BitBlt.argtypes = [
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.DWORD,
    ]
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(BITMAPINFO),
        wintypes.UINT,
    ]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]

    hdc = user32.GetDC(0)
    if not hdc:
        return b"", 0, 0
    memdc = gdi32.CreateCompatibleDC(hdc)
    bitmap = gdi32.CreateCompatibleBitmap(hdc, width, height)
    previous = gdi32.SelectObject(memdc, bitmap)
    try:
        if not gdi32.BitBlt(memdc, 0, 0, width, height, hdc, 0, 0, SRCCOPY):
            return b"", 0, 0
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        buffer = ctypes.create_string_buffer(width * height * 4)
        copied = gdi32.GetDIBits(
            memdc,
            bitmap,
            0,
            height,
            ctypes.cast(buffer, ctypes.c_void_p),
            ctypes.byref(info),
            DIB_RGB_COLORS,
        )
        if not copied:
            return b"", 0, 0
        return buffer.raw, width, height
    finally:
        gdi32.SelectObject(memdc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(0, hdc)


def _ocr(raw: bytes, width: int, height: int) -> str:
    from winrt.windows.graphics.imaging import (
        BitmapAlphaMode,
        BitmapPixelFormat,
        SoftwareBitmap,
    )
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    writer = DataWriter()
    writer.write_bytes(raw)
    buffer = writer.detach_buffer()
    bitmap = SoftwareBitmap.create_copy_from_buffer(
        buffer,
        BitmapPixelFormat.BGRA8,
        width,
        height,
        BitmapAlphaMode.PREMULTIPLIED,
    )
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("Windows OCR has no language pack installed")
    async def run(operation):
        return await operation

    result = asyncio.run(run(engine.recognize_async(bitmap)))
    return "\n".join(line.text for line in result.lines)


def _clean(text: str) -> str:
    lines = []
    for line in text.replace("\r", "\n").splitlines():
        clean = re.sub(r"\s+", " ", line).strip()
        if clean:
            lines.append(clean)
    return "\n".join(lines)
