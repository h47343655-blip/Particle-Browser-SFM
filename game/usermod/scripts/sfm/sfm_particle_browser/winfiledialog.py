# -*- coding: utf-8 -*-
"""Fill in a standard Windows "Open" dialog of this process (Win32 through ctypes).

Used when SFM's "Browse..." opens the native file dialog, which Qt cannot see.
Only messages are sent; nothing in SFM's memory is touched.
"""
from __future__ import absolute_import

import ctypes
import os
import sys

DIALOG_CLASS = u"#32770"
# Control IDs of the file name field: cmb13 (combo with edit, also in the Vista dialog)
# and edt1 (older dialogs). The Edit window sits inside cmb13.
FILENAME_IDS = (0x47C, 0x480)
WM_SETTEXT = 0x000C
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
IDOK = 1


class NativeFileDialogs(object):
    """Finds and fills Windows file dialogs; ``available`` is False off Windows."""

    def __init__(self):
        self.available = False
        if not sys.platform.startswith("win"):
            return
        try:
            from ctypes import wintypes
            self._w = wintypes
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            u = self._user32
            u.EnumWindows.argtypes = [self._proc, wintypes.LPARAM]
            u.EnumChildWindows.argtypes = [wintypes.HWND, self._proc, wintypes.LPARAM]
            u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            u.IsWindowVisible.argtypes = [wintypes.HWND]
            u.GetDlgCtrlID.argtypes = [wintypes.HWND]
            u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            u.SendMessageW.restype = ctypes.c_ssize_t
            u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            self.available = True
        except Exception:
            self.available = False

    def _hwnd(self, value):
        return int(value) if value else 0

    def _class(self, hwnd):
        buf = ctypes.create_unicode_buffer(256)
        self._user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def _enum(self, parent=None):
        found = []

        def callback(hwnd, _lparam):
            found.append(self._hwnd(hwnd))
            return True
        proc = self._proc(callback)  # must stay referenced during the call
        if parent is None:
            self._user32.EnumWindows(proc, 0)
        else:
            self._user32.EnumChildWindows(parent, proc, 0)
        return found

    def dialogs(self):
        """Visible top-level dialog windows of this process."""
        if not self.available:
            return []
        pid = os.getpid()
        result = []
        for hwnd in self._enum():
            owner = self._w.DWORD()
            self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and self._user32.IsWindowVisible(hwnd) and self._class(hwnd) == DIALOG_CLASS:
                result.append(hwnd)
        return result

    def filename_edit(self, hwnd):
        children = self._enum(hwnd)
        for wanted in FILENAME_IDS:
            for child in children:
                if self._user32.GetDlgCtrlID(child) != wanted:
                    continue
                if self._class(child) == u"Edit":
                    return child
                for inner in self._enum(child):
                    if self._class(inner) == u"Edit":
                        return inner
        return 0

    def find_new(self, known):
        """A file dialog that was not in ``known`` (window handles), or 0."""
        for hwnd in self.dialogs():
            if hwnd not in known and self.filename_edit(hwnd):
                return hwnd
        return 0

    def fill(self, hwnd, path):
        edit = self.filename_edit(hwnd)
        if not edit:
            return False, u"no file name field in dialog %#x" % hwnd
        text = ctypes.create_unicode_buffer(path)
        self._user32.SendMessageW(edit, WM_SETTEXT, 0, ctypes.addressof(text))
        # posted, not sent: the dialog runs its own loop and SendMessage could wait on it
        self._user32.PostMessageW(hwnd, WM_COMMAND, IDOK, 0)
        return True, u"native dialog %#x" % hwnd

    def close(self, hwnd):
        self._user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
