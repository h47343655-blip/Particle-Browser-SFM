# -*- coding: utf-8 -*-
"""Qt binding shim: SFM ships PySide 1.x (Qt 4); tests run on PySide6."""
from __future__ import absolute_import

import importlib

try:
    from PySide import QtCore, QtGui  # noqa: F401
    QtWidgets = QtGui
    QT_API = "PySide"
except ImportError:
    try:
        from PySide2 import QtCore, QtGui, QtWidgets  # noqa: F401
        QT_API = "PySide2"
    except ImportError:
        from PySide6 import QtCore, QtGui, QtWidgets  # noqa: F401
        QT_API = "PySide6"

QAction = getattr(QtWidgets, "QAction", None) or QtGui.QAction
QShortcut = getattr(QtWidgets, "QShortcut", None) or QtGui.QShortcut
# Qt.MiddleButton appeared in Qt 4.7; older builds only have MidButton.
MIDDLE_BUTTON = getattr(QtCore.Qt, "MiddleButton", None) or getattr(QtCore.Qt, "MidButton")

DIALOG_ACCEPTED = 1  # QDialog::Accepted in every Qt version

_SHIBOKEN_NAMES = {
    "PySide": ("PySide.shiboken", "shiboken"),  # SFM has PySide/shiboken.pyd
    "PySide2": ("shiboken2", "PySide2.shiboken2"),
    "PySide6": ("shiboken6",),
}
_shiboken = []
_recast = {}  # C++ address -> widget-typed wrapper, kept for the whole session


def binding_version():
    for name in (QT_API, "PySide", "PySide2", "PySide6"):
        try:
            return getattr(importlib.import_module(name), "__version__", u"?")
        except ImportError:
            continue
    return u"?"


def shiboken_module():
    """The binding's shiboken module (wrapInstance/getCppPointer), or None."""
    if not _shiboken:
        found = None
        for name in _SHIBOKEN_NAMES.get(QT_API, ()):
            try:
                module = importlib.import_module(name)
            except ImportError:
                continue
            if hasattr(module, "wrapInstance") and hasattr(module, "getCppPointer"):
                found = module
                break
        _shiboken.append(found)
    return _shiboken[0]


def cpp_address(obj):
    """Address of the C++ object behind a Qt wrapper, or None."""
    module = shiboken_module()
    if obj is None or module is None:
        return None
    try:
        return int(module.getCppPointer(obj)[0])
    except Exception:
        return None


def same_object(a, b):
    """True when two wrappers stand for the same C++ object (wrappers may differ)."""
    if a is b:
        return True
    if a is None or b is None:
        return False
    address = cpp_address(a)
    return address is not None and address == cpp_address(b)


def as_widget(obj):
    """``obj`` as a QWidget, or None if it is not a widget.

    SFM's ``sfmApp.GetMainWindow()`` returns the main window typed as a plain QObject,
    and PySide then hands out that same wrapper wherever Qt returns the main window
    (``parentWidget()``, ``activeWindow()``, ``topLevelWidgets()``). Widget methods and
    ``QDialog(parent)`` reject it, so the C++ object is re-wrapped with its real type.
    """
    if obj is None or isinstance(obj, QtWidgets.QWidget):
        return obj
    try:
        if not obj.isWidgetType():
            return None
        main = obj.inherits("QMainWindow")
        class_name = obj.metaObject().className()
    except (AttributeError, RuntimeError, TypeError):
        return None
    address = cpp_address(obj)
    if address is None:
        return None
    cached = _recast.get(address)
    try:
        if cached is not None and cached.metaObject().className() == class_name:
            return cached
    except RuntimeError:
        pass
    kind = QtWidgets.QMainWindow if main else QtWidgets.QWidget
    try:
        widget = shiboken_module().wrapInstance(address, kind)
    except Exception:
        return None
    # Dialogs get this wrapper as their parent; it must outlive them.
    _recast[address] = widget
    return widget


def modality_for(parent):
    # WindowModal needs a parent window; without one only ApplicationModal blocks input.
    return QtCore.Qt.WindowModal if parent is not None else QtCore.Qt.ApplicationModal


def set_placeholder(line_edit, text):
    """QLineEdit.setPlaceholderText exists from Qt 4.7 on."""
    setter = getattr(line_edit, "setPlaceholderText", None)
    if setter is not None:
        setter(text)


def exec_dialog(widget):
    run = getattr(widget, "exec_", None) or getattr(widget, "exec")
    return run()


def app_instance():
    return QtWidgets.QApplication.instance()


def _legacy_grab():
    # QPixmap.grabWindow exists up to Qt 5 (deprecated there) and is gone in Qt 6.
    return getattr(QtGui.QPixmap, "grabWindow", None)


def grab_widget_window(widget, x, y, w, h):
    """Copy a region of ``widget``'s native window as the window system has it."""
    legacy = _legacy_grab()
    if legacy is not None:
        return legacy(widget.winId(), x, y, w, h)
    screen = widget.screen() if hasattr(widget, "screen") else None
    screen = screen or QtGui.QGuiApplication.primaryScreen()
    return screen.grabWindow(int(widget.winId()), x, y, w, h)


def grab_screen(global_x, global_y, w, h):
    """Copy a region of the screen (whatever is visible there)."""
    legacy = _legacy_grab()
    if legacy is not None:
        return legacy(QtWidgets.QApplication.desktop().winId(), global_x, global_y, w, h)
    point = QtCore.QPoint(global_x, global_y)
    screen = QtGui.QGuiApplication.screenAt(point) or QtGui.QGuiApplication.primaryScreen()
    origin = screen.geometry().topLeft()
    return screen.grabWindow(0, global_x - origin.x(), global_y - origin.y(), w, h)


def available_geometry(widget):
    """``(x, y, w, h)`` of the usable screen area around ``widget``."""
    screen_getter = getattr(widget, "screen", None)
    if screen_getter is not None:
        rect = screen_getter().availableGeometry()
    else:
        rect = QtWidgets.QApplication.desktop().availableGeometry(widget)
    return (rect.x(), rect.y(), rect.width(), rect.height())


def event_pos(event):
    """Mouse position as QPoint (``pos()`` is deprecated in Qt 6)."""
    position = getattr(event, "position", None)
    return position().toPoint() if position is not None else event.pos()


def wheel_steps(event):
    """Wheel notches of ``event`` (positive = away from the user)."""
    angle = getattr(event, "angleDelta", None)
    delta = angle().y() if angle is not None else event.delta()
    return delta / 120.0


def html_escape(text):
    return (text.replace(u"&", u"&amp;").replace(u"<", u"&lt;")
            .replace(u">", u"&gt;").replace(u"\"", u"&quot;"))
