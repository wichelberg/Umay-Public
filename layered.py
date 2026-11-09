"""Win32 layered windows for Umay's Tk windows (the text box's neon frame, the light around the screen):
per-pixel alpha bitmaps given to UpdateLayeredWindow, and the work area. Lived in desktop_pet.py until the
HUD replaced the pet.
"""
import ctypes
from ctypes import wintypes

import numpy as np

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020  # mouse clicks pass through the window
ULW_ALPHA = 0x2
AC_SRC_ALPHA = 0x1
DIB_RGB_COLORS = 0
SPI_GETWORKAREA = 0x0030


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
    wintypes.HDC, ctypes.POINTER(wintypes.POINT), wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION),
    wintypes.DWORD]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]  # a 64-bit handle overflows the default int


def work_area():
    """The primary monitor minus the taskbar: (left, top, right, bottom)."""
    rect = wintypes.RECT()
    user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
    return rect.left, rect.top, rect.right, rect.bottom


def premultiplied(image):
    """An RGBA image as the premultiplied BGRA bytes UpdateLayeredWindow wants."""
    rgba = np.asarray(image, dtype=np.uint16)
    alpha = rgba[..., 3:4]
    return np.concatenate([rgba[..., 2::-1] * alpha // 255, alpha], axis=2).astype(np.uint8).tobytes()


def dib(dc, width, height):
    """A top-down 32-bit DIB section: (handle, pointer to its pixels)."""
    header = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=width,
                              biHeight=-height, biPlanes=1, biBitCount=32, biCompression=0)
    bits = ctypes.c_void_p()
    handle = gdi32.CreateDIBSection(dc, ctypes.byref(header), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
    return handle, bits


class Frame:
    """An image as a premultiplied 32-bit bitmap ready for UpdateLayeredWindow."""

    def __init__(self, image, dc):
        self.width, self.height = image.size
        self.bitmap, bits = dib(dc, self.width, self.height)
        data = premultiplied(image)
        ctypes.memmove(bits, data, len(data))

    def free(self):
        gdi32.DeleteObject(self.bitmap)
