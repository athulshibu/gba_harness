"""Targeted Windows key messages for SkyEmu's GUI-only speed hotkey."""

import ctypes
from ctypes import wintypes
import os


class WindowsSpeedControl:
    def __init__(self, key="F"):
        if os.name != "nt":
            raise ValueError("Automatic 2x speed currently requires Windows. Use --speed 1 elsewhere.")
        key = key.strip().upper()
        if len(key) != 1 or not key.isascii() or not key.isalnum():
            raise ValueError("SKYEMU_FAST_FORWARD_KEY must be one letter or digit matching SkyEmu's 2x fast-forward binding")
        self.key = key
        self.virtual_key = ord(key)
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.PostMessageW.restype = wintypes.BOOL
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user32.EnumWindows.argtypes = [self.callback_type, wintypes.LPARAM]
        self.user32.EnumWindows.restype = wintypes.BOOL
        self.user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        self.user32.MapVirtualKeyW.restype = wintypes.UINT
        windows = []

        def inspect(handle, _):
            title = ctypes.create_unicode_buffer(self.user32.GetWindowTextLengthW(handle) + 1)
            self.user32.GetWindowTextW(handle, title, len(title))
            window_class = ctypes.create_unicode_buffer(256)
            self.user32.GetClassNameW(handle, window_class, len(window_class))
            if title.value == "SkyEmu" and "SOKOL" in window_class.value.upper():
                windows.append(handle)
            return True

        if not self.user32.EnumWindows(self.callback_type(inspect), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        if len(windows) != 1:
            raise ValueError("2x speed requires exactly one SkyEmu game window. Close duplicate instances or use --speed 1.")
        self.handle = windows[0]

    def send(self, pressed):
        if not self.user32.IsWindow(self.handle):
            raise ValueError("The SkyEmu window closed while setting game speed")
        scan_code = self.user32.MapVirtualKeyW(self.virtual_key, 0)
        flags = 1 | (scan_code << 16)
        if not pressed:
            flags |= (1 << 30) | (1 << 31)
        if not self.user32.PostMessageW(self.handle, 0x0100 if pressed else 0x0101, self.virtual_key, flags):
            raise ctypes.WinError(ctypes.get_last_error())
