# -*- coding: utf-8 -*-
"""Put the picker into SFM's menus and hand the chosen name to SFM's own dialog."""
from __future__ import absolute_import

import ctypes
import os
import sys
import time

from .compat import to_text
from .qt import QAction, QtCore, QtGui, QtWidgets, same_object

ACTION_OBJECT_NAME = "sfmParticleBrowserAction"
ACTION_TEXT = u"Create Animation Set for New Particle System\u2026"
STOCK_TEXT = u"create animation set for new particle system"


def normalize(text):
    text = to_text(text or u"").replace(u"&", u"").replace(u"\u2026", u"").replace(u"...", u"")
    return u" ".join(text.lower().split())


def is_stock_particle_action(action):
    try:
        if action is None or action.isSeparator() or action.objectName() == ACTION_OBJECT_NAME:
            return False
        text = normalize(action.text())
    except RuntimeError:  # the C++ object is gone
        return False
    return text == STOCK_TEXT or (u"new particle system" in text and u"animation set" in text)


def find_stock_action(menu):
    try:
        actions = menu.actions()
    except RuntimeError:
        return None
    for action in actions:
        if is_stock_particle_action(action):
            return action
    return None


def find_add_menus(root):
    """Menus of the Animation Set Editor that contain SFM's particle action."""
    menus = []
    for editor in root.findChildren(QtWidgets.QWidget):
        if editor.objectName() != "QAnimationSetEditor":
            continue
        for widget in editor.findChildren(QtWidgets.QWidget):
            getter = getattr(widget, "menu", None)
            if getter is None:
                continue
            try:
                menu = getter()
            except Exception:
                continue
            if menu is not None and find_stock_action(menu) is not None and menu not in menus:
                menus.append(menu)
    return menus


def _alive(action):
    try:
        action.text()
        return True
    except (RuntimeError, AttributeError):
        return False


class MenuHook(QtCore.QObject):
    """Inserts our action in front of SFM's particle action wherever it appears.

    The Animation Set Editor can rebuild its menus, so besides patching the "+"
    menu once, an application event filter patches any matching menu on show.
    """

    def __init__(self, main_window, on_trigger, settings, log=None):
        super(MenuHook, self).__init__(main_window)
        self.main_window = main_window
        self.settings = settings
        self.log = log or (lambda message: None)
        self._on_trigger = on_trigger
        self._busy = False
        self._filter_installed = False
        self.stock_action = None
        self.action = QAction(ACTION_TEXT, main_window)
        self.action.setObjectName(ACTION_OBJECT_NAME)
        self.action.setToolTip(u"Particle Browser: search, favorites and live preview")
        self.action.triggered.connect(self._triggered)

    @property
    def filter_installed(self):
        return self._filter_installed

    def _triggered(self, *_args):
        if self._busy:
            return
        self._busy = True
        try:
            self._on_trigger()
        finally:
            self._busy = False

    def patch_menu(self, menu):
        stock = find_stock_action(menu)
        if stock is None:
            return False
        self.stock_action = stock
        if self.action.icon().isNull() and not stock.icon().isNull():
            self.action.setIcon(stock.icon())
        if self.action not in menu.actions():
            menu.insertAction(stock, self.action)
        stock.setVisible(not self.settings.get("replace_stock_menu"))
        return True

    def install(self):
        patched = 0
        for menu in find_add_menus(self.main_window):
            if self.patch_menu(menu):
                patched += 1
        if self.settings.get("patch_all_menus") and not self._filter_installed:
            app = QtWidgets.QApplication.instance()
            if app is not None:
                app.installEventFilter(self)
                self._filter_installed = True
        return patched

    def uninstall(self):
        if self._filter_installed:
            app = QtWidgets.QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
            self._filter_installed = False
        getter = (getattr(self.action, "associatedWidgets", None)
                  or getattr(self.action, "associatedObjects", None))
        for widget in list(getter() if getter else ()):
            remove = getattr(widget, "removeAction", None)
            if remove is not None:
                remove(self.action)
        if self.stock_action is not None and _alive(self.stock_action):
            self.stock_action.setVisible(True)

    def live_stock_action(self):
        if self.stock_action is not None and _alive(self.stock_action):
            return self.stock_action
        return None

    def eventFilter(self, obj, event):
        try:
            if event.type() == QtCore.QEvent.Show and isinstance(obj, QtWidgets.QMenu):
                self.patch_menu(obj)
        except Exception:
            pass  # never let the hook break SFM's event processing
        return False


# ---------------------------------------------------------------------------
# Handing the choice over to SFM's own dialog
# ---------------------------------------------------------------------------

def describe_widget(widget):
    try:
        text = u""
        for getter in ("text", "windowTitle", "toolTip"):
            method = getattr(widget, getter, None)
            if method is not None:
                text = to_text(method())
                if text:
                    break
        return u"%s#%s %r" % (widget.metaObject().className(), to_text(widget.objectName()), text)
    except Exception as exc:
        return u"<unavailable: %s>" % exc


def describe_tree(widget, depth=0, limit=120):
    lines = [u"  " * depth + describe_widget(widget)]
    for child in widget.children():
        if len(lines) >= limit:
            lines.append(u"  " * depth + u"\u2026")
            break
        if isinstance(child, QtWidgets.QWidget):
            lines.extend(describe_tree(child, depth + 1, limit - len(lines)))
    return lines


def _looks_numeric(edit):
    if isinstance(edit.validator(), (QtGui.QIntValidator, QtGui.QDoubleValidator)):
        return True
    text = to_text(edit.text()).strip()
    if not text:
        return False
    try:
        float(text)
        return True
    except ValueError:
        return False


def _edit_next_to_browse(dialog, edits):
    """The text field on the same row as a "Browse..." button, left of it."""
    buttons = [b for b in dialog.findChildren(QtWidgets.QAbstractButton)
               if u"brows" in normalize(b.text()) and b.isVisible()]
    best, best_score = None, None
    for button in buttons:
        button_center = button.mapTo(dialog, button.rect().center())
        for edit in edits:
            edit_center = edit.mapTo(dialog, edit.rect().center())
            dy = abs(edit_center.y() - button_center.y())
            dx = button_center.x() - edit_center.x()
            if dx < 0 or dy > max(edit.height(), button.height()):
                continue
            score = dy * 4 + dx
            if best_score is None or score < best_score:
                best, best_score = edit, score
    return best


# Object names in SFM's "Choose Particle System" dialog (CQParticleSystemPickerDialog),
# taken from a console log of a real SFM install.
DIALOG_NAME = "ParticleSystemPickerDialog"
FILE_EDIT_NAME = "particleDefinitionFileLineEdit"
DEFINITION_COMBO_NAME = "particleDefinitionComboBox"

# Ways to tell SFM that the file field changed, gentlest first. No Return key event:
# QLineEdit passes Return on to the dialog, which would press OK.
NOTIFY_STAGES = ("text", "signals", "focus")
BROWSE = "browse"  # press SFM's Browse button and answer the file dialog it opens
BROWSE_BUTTON_NAME = "browseParticleFileButton"
OPEN_WORDS = (u"open", u"ok", u"\u043e\u0442\u043a\u0440\u044b\u0442\u044c", u"select", u"choose")


def find_browse_button(dialog):
    for button in dialog.findChildren(QtWidgets.QAbstractButton):
        if button.objectName() == BROWSE_BUTTON_NAME:
            return button
    for button in dialog.findChildren(QtWidgets.QAbstractButton):
        if u"brows" in normalize(button.text()) and button.isVisible():
            return button
    return None


def fill_qt_file_dialog(dialog, path):
    """Answer a Qt file dialog (or a simple look-alike) with ``path``; ``(ok, detail)``."""
    if isinstance(dialog, QtWidgets.QFileDialog):
        dialog.selectFile(path)
        for edit in dialog.findChildren(QtWidgets.QLineEdit):
            if edit.objectName() == "fileNameEdit":
                edit.setText(path)
        dialog.accept()
        return True, u"Qt file dialog %s" % describe_widget(dialog)
    edits = [e for e in dialog.findChildren(QtWidgets.QLineEdit)
             if e.isVisible() and e.isEnabled() and not isinstance(e.parent(), QtWidgets.QAbstractSpinBox)]
    if len(edits) != 1:
        return False, u"unknown dialog %s with %d text fields" % (describe_widget(dialog), len(edits))
    edits[0].setText(path)
    for button in dialog.findChildren(QtWidgets.QAbstractButton):
        if button.isVisible() and normalize(button.text()).strip(u"&. ") in OPEN_WORDS:
            button.click()
            return True, u"dialog %s via %r" % (describe_widget(dialog), to_text(button.text()))
    accept = getattr(dialog, "accept", None)
    if accept is None:
        return False, u"dialog %s has no open button" % describe_widget(dialog)
    accept()
    return True, u"dialog %s accepted" % describe_widget(dialog)


def find_stock_fields(dialog):
    """``(file_edit, definition_combo)`` of SFM's dialog; either may be None."""
    edit = combo = None
    for widget in dialog.findChildren(QtWidgets.QLineEdit):
        if widget.objectName() == FILE_EDIT_NAME:
            edit = widget
    for widget in dialog.findChildren(QtWidgets.QComboBox):
        if widget.objectName() == DEFINITION_COMBO_NAME:
            combo = widget
    if edit is None:
        edits = [e for e in dialog.findChildren(QtWidgets.QLineEdit)
                 if e.isVisible() and not _looks_numeric(e)
                 and not isinstance(e.parent(), (QtWidgets.QAbstractSpinBox, QtWidgets.QComboBox))]
        edit = _edit_next_to_browse(dialog, edits)
    if combo is None:
        combos = [c for c in dialog.findChildren(QtWidgets.QComboBox) if c.isVisible()]
        combo = combos[0] if len(combos) == 1 else None
    return edit, combo


def combo_items(combo):
    return [to_text(combo.itemText(i)) for i in range(combo.count())]


def find_item(items, name):
    for i, text in enumerate(items):
        if text == name:
            return i
    lowered = name.lower()
    for i, text in enumerate(items):
        if text.lower() == lowered:
            return i
    return -1


def _emit(obj, signal, *args):
    try:
        getattr(obj, signal).emit(*args)
    except Exception:
        pass


def notify_file_edit(edit, text, stage, focus_target=None):
    """Put ``text`` into the file field and tell SFM, using one ``NOTIFY_STAGES`` way."""
    if stage == "text":
        edit.setFocus()
        edit.setText(text)  # emits textChanged
        _emit(edit, "textEdited", text)
    elif stage == "signals":
        _emit(edit, "returnPressed")
        _emit(edit, "editingFinished")
    elif stage == "focus":
        edit.setFocus()
        if focus_target is not None:
            focus_target.setFocus()  # focus-out makes QLineEdit emit editingFinished


def select_item(combo, index):
    combo.setCurrentIndex(index)  # emits currentIndexChanged
    _emit(combo, "activated", index)


def _top_levels():
    return list(QtWidgets.QApplication.topLevelWidgets())


class StockDialogFiller(QtCore.QObject):
    """Waits for SFM's particle dialog, fills in the .pcf file and selects the system.

    SFM lists a file's systems only after it has read the file, so each candidate path is
    tried with each notification stage until the definition list contains ``name``. The
    ``browse_file`` step presses Browse and answers the file dialog like a user would.
    """

    def __init__(self, name, files=(), log=None, timeout=8.0, parent=None, settle_ticks=3,
                 browse_file=None, browse_ticks=40, native=None):
        super(StockDialogFiller, self).__init__(parent)
        self.name = name
        self.files = []
        for path in files:
            if path and path not in self.files:
                self.files.append(path)
        self.browse_file = browse_file if browse_file and os.path.isabs(browse_file) else None
        self.browse_ticks = browse_ticks
        self.native = native if native is not None else NativeFileDialogs()
        self.log = log or (lambda message: None)
        self.settle_ticks = settle_ticks
        self.result = None
        self.tried = []
        self._known = _top_levels()  # holding the wrappers keeps identity checks valid
        self._deadline = time.time() + timeout
        self._dialog = None
        self._edit = self._combo = None
        self._steps = []
        self._step = None
        self._waited = 0
        self._initial_items = []
        self._browse = None  # "open": waiting for the file dialog, "list": answered it
        self._native_known = set()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll)

    def start(self):
        self._timer.start()

    def _find_dialog(self):
        # SFM's own dialog by its object name first: another modal window (a message box, a
        # progress dialog) must never be mistaken for it
        for widget in _top_levels():
            if (isinstance(widget, QtWidgets.QDialog) and widget.objectName() == DIALOG_NAME
                    and widget.isVisible()):
                return widget
        dialog = QtWidgets.QApplication.activeModalWidget()
        if dialog is not None:
            return dialog
        for widget in _top_levels():
            # the type check comes first: SFM's main window may show up as a plain QObject
            if (isinstance(widget, QtWidgets.QDialog) and widget not in self._known
                    and widget.isVisible()):
                return widget
        return None

    def _poll(self):
        if self._dialog is not None:
            try:
                self._check_step()
            except RuntimeError:  # the dialog was closed while we were filling it
                self._finish(False, u"SFM's dialog closed before the list was filled")
            except Exception as exc:
                self._finish(False, u"%s" % exc)
            return
        dialog = self._find_dialog()
        if dialog is None:
            if time.time() > self._deadline:
                self._finish(False, u"SFM's particle dialog was not detected; the name is in"
                                    u" the clipboard (Ctrl+V).")
            return
        self.log(u"SFM dialog layout (for bug reports):\n" + u"\n".join(describe_tree(dialog)))
        try:
            self._start(dialog)
        except Exception as exc:
            self._finish(False, u"%s" % exc)

    def _start(self, dialog):
        edit, combo = find_stock_fields(dialog)
        if edit is None or combo is None or not (self.files or self.browse_file):
            # Never type the system's name into the file field: SFM expects a .pcf there and
            # shows an empty list otherwise. Leave the dialog as it is; the name is in the clipboard.
            self._finish(False, u"SFM's dialog has an unknown layout or the .pcf was not found; pick the"
                                u" file with Browse\u2026, the name is in the clipboard.")
            return
        self._dialog, self._edit, self._combo = dialog, edit, combo
        self._initial_items = combo_items(combo)
        if find_item(self._initial_items, self.name) >= 0:
            select_item(combo, find_item(self._initial_items, self.name))
            self._finish(True, u"selected %r (already listed)" % self.name)
            return
        self._steps = self._plan()
        self._next_step()

    def _plan(self):
        """Typing the relative path first (cheap), then Browse, then the other path forms."""
        steps = [(self.files[0], stage) for stage in NOTIFY_STAGES] if self.files else []
        if self.browse_file and find_browse_button(self._dialog) is not None:
            steps.append((self.browse_file, BROWSE))
        steps += [(path, stage) for path in self.files[1:] for stage in NOTIFY_STAGES]
        return steps

    def _next_step(self):
        if not self._steps:
            self._give_up()
            return
        self._step = self._steps.pop(0)
        self._waited = 0
        path, stage = self._step
        if stage in (NOTIFY_STAGES[0], BROWSE) and self._combo.count():
            self._combo.clear()  # a list left by an earlier path must not be mistaken for ours
            self._initial_items = []
        if stage == BROWSE:
            self._start_browse()
            return
        notify_file_edit(self._edit, path, stage, focus_target=self._combo)
        self._check_step()

    def _start_browse(self):
        self._browse = "open"
        self._native_known = set(self.native.dialogs()) if self.native.available else set()
        button = find_browse_button(self._dialog)
        # queued: the file dialog runs a nested event loop, which must not start inside
        # _poll; this timer keeps ticking in that loop and answers the dialog
        QtCore.QTimer.singleShot(0, button.click)

    def _poll_browse(self):
        """While the file dialog should be open; returns True when the step moved on."""
        path = self._step[0]
        modal = QtWidgets.QApplication.activeModalWidget()
        if modal is not None and not same_object(modal, self._dialog):
            self.log(u"Browse opened (for bug reports):\n" + u"\n".join(describe_tree(modal)))
            ok, detail = fill_qt_file_dialog(modal, path)
            if not ok:
                modal.close()
            return self._browse_answered(ok, detail)
        if self.native.available:
            hwnd = self.native.find_new(self._native_known)
            if hwnd:
                ok, detail = self.native.fill(hwnd, path)
                if not ok:
                    self.native.close(hwnd)
                return self._browse_answered(ok, detail)
        if self._waited < self.browse_ticks:
            self._waited += 1
            return False
        self._browse = None
        self.tried.append(u"%s [browse]: no file dialog was detected" % path)
        self._next_step()
        return True

    def _browse_answered(self, ok, detail):
        path = self._step[0]
        if not ok:
            self._browse = None
            self.tried.append(u"%s [browse]: %s" % (path, detail))
            self._next_step()
            return True
        self._browse = "list"
        self._waited = 0
        self.tried.append(u"%s [browse]: answered %s" % (path, detail))
        return False

    def _check_step(self):
        path, stage = self._step
        if stage == BROWSE and self._browse == "open":
            self._poll_browse()
            return
        items = combo_items(self._combo)
        if items and items != self._initial_items:
            index = find_item(items, self.name)
            if index >= 0:
                select_item(self._combo, index)
                if stage == BROWSE:
                    self.tried.append(u"SFM wrote %r into the file field" % to_text(self._edit.text()))
                self.tried.append(u"%s [%s]: listed %d, selected" % (path, stage, len(items)))
                self._finish(True, u"file %r, selected %r (via %s)" % (path, self.name, stage))
                return
            self.tried.append(u"%s [%s]: listed %d, %r not among them" % (path, stage, len(items), self.name))
            self._skip_path(path)
            self._next_step()
            return
        # after Browse SFM may read a big file first; allow it more time
        settle = self.settle_ticks * 5 if stage == BROWSE else self.settle_ticks
        if self._waited < settle:
            self._waited += 1
            return
        self.tried.append(u"%s [%s]: list stayed empty" % (path, stage))
        self._next_step()

    def _skip_path(self, path):
        # SFM read this file: more stages for the same path would list the same systems
        self._steps = [step for step in self._steps if step[0] != path]

    def _give_up(self):
        try:
            self._edit.setText(self.files[0] if self.files else self.browse_file)
        except RuntimeError:
            pass
        self._finish(False, u"SFM did not list %r for any file path (%s). Use Browse\u2026 to pick"
                            u" the file; the name is in the clipboard." % (self.name, u"; ".join(self.tried)))

    def _finish(self, ok, detail):
        self._timer.stop()
        self._dialog = None
        self._browse = None
        self.result = (ok, detail)
        self.log(u"Hand-off %s: %s" % (u"succeeded" if ok else u"incomplete", detail))
        if self.tried:
            self.log(u"Hand-off attempts:\n  " + u"\n  ".join(self.tried))


def handoff_to_stock(name, hook=None, main_window=None, log=None, files=(), browse_file=None):
    """Open SFM's particle dialog with ``name`` selected; returns ``(filler, problem)``.

    ``files``: paths of the system's .pcf to try in the dialog's file field, best first.
    ``browse_file``: absolute path of that .pcf for the Browse button's file dialog.
    """
    QtWidgets.QApplication.clipboard().setText(name)
    stock = hook.live_stock_action() if hook is not None else None
    if stock is None and main_window is not None:
        for menu in find_add_menus(main_window):
            stock = find_stock_action(menu)
            if stock is not None:
                break
    if stock is None:
        return None, (u"SFM's \u201cCreate Animation Set for New Particle System\u201d action"
                      u" was not found. The name \u201c%s\u201d is in the clipboard." % name)
    filler = StockDialogFiller(name, files=files, log=log, parent=main_window, browse_file=browse_file,
                               timeout=8.0)
    filler.start()
    # Hidden actions are also disabled in Qt; make it visible while triggering it.
    was_visible = stock.isVisible()
    stock.setVisible(True)
    try:
        stock.trigger()  # SFM's handler usually runs its modal dialog right here
    finally:
        if _alive(stock):
            stock.setVisible(was_visible)
    return filler, None


# -- native Windows "Open" dialog --------------------------------------------


# Fill in a standard Windows "Open" dialog of this process (Win32 through ctypes). Used when
# SFM's "Browse..." opens the native file dialog, which Qt cannot see. Only messages are sent;
# nothing in SFM's memory is touched.
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
