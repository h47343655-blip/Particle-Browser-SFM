# -*- coding: utf-8 -*-
"""Show SFM's 3D viewport inside the picker window.

Python has no way to open a second engine view, so the picker mirrors the Primary
Viewport instead: after each engine frame it copies the viewport's pixels (the
preview particle sits in the middle of the scene camera's view) and draws them,
optionally zoomed, next to the list.
"""
from __future__ import absolute_import, division

from . import geometry
from .compat import to_text
from .qt import QtCore, QtWidgets, as_widget, grab_screen, grab_widget_window, same_object

NAME_HINTS = (u"viewport", u"engine", u"render", u"3d")
MIN_WIDTH = 160
MIN_HEIGHT = 120
BLANK_TOLERANCE = 3
SAMPLE_GRID = 16  # 256 samples: small effects on dark maps still count as content
BLANK_SWITCH_AFTER = 3

STRATEGY_WINDOW = "window"  # read the viewport's own native window
STRATEGY_SCREEN = "screen"  # read the screen area where the viewport is shown


def describe(widget):
    try:
        return u"%s#%s" % (to_text(widget.metaObject().className()),
                           to_text(widget.objectName()))
    except RuntimeError:
        return u"<deleted>"


def _depth(widget, root):
    """Ancestors between ``widget`` and ``root``.

    Walks QObject parents: in SFM the main window's wrapper is a plain QObject, so
    ``parentWidget()`` is not available on it, and wrappers are compared by C++ object.
    """
    depth = 0
    try:
        parent = widget.parent()
        while parent is not None and not same_object(parent, root) and depth < 512:
            depth += 1
            parent = parent.parent()
    except (AttributeError, RuntimeError):
        pass
    return depth


def score_widget(widget, hint=u""):
    """Viewport-likeness of ``widget``, or ``None`` if it cannot be the viewport."""
    try:
        if not widget.isVisible():
            return None
        width, height = widget.width(), widget.height()
        text = u"%s %s" % (to_text(widget.metaObject().className()),
                           to_text(widget.objectName()))
    except RuntimeError:
        return None
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return None
    text = text.lower()
    score = 0
    if hint and hint.lower() in text:
        score += 100
    if any(h in text for h in NAME_HINTS):
        score += 4
    # Direct3D/OpenGL widgets paint on screen themselves and need a native window.
    if widget.testAttribute(QtCore.Qt.WA_PaintOnScreen):
        score += 4
    if widget.testAttribute(QtCore.Qt.WA_NativeWindow):
        score += 2
    visible_children = [c for c in widget.children()
                        if isinstance(c, QtWidgets.QWidget) and c.isVisible()]
    if not visible_children:
        score += 1
    return score


def rank_candidates(root, exclude=None, hint=u""):
    """``[(score, depth, area, widget)]`` best first; widgets of ``exclude`` are skipped."""
    ranked = []
    for widget in root.findChildren(QtWidgets.QWidget):
        widget = as_widget(widget)
        if widget is None:
            continue
        try:
            if exclude is not None and (widget is exclude or same_object(widget.window(), exclude)):
                continue
        except RuntimeError:
            continue
        score = score_widget(widget, hint)
        if score is None or score <= 0:
            continue
        ranked.append((score, _depth(widget, root), widget.width() * widget.height(), widget))
    # Native ancestors of the viewport look similar; prefer the deepest, then the largest.
    ranked.sort(key=lambda item: (-item[0], -item[1], -item[2]))
    return ranked


def is_blank(image, tolerance=BLANK_TOLERANCE, grid=SAMPLE_GRID):
    """True when every sampled pixel has (almost) the same colour."""
    width, height = image.width(), image.height()
    if width <= 0 or height <= 0:
        return True
    low = [255, 255, 255]
    high = [0, 0, 0]
    for i in range(grid):
        for j in range(grid):
            value = image.pixel(int((i + 0.5) * width / grid), int((j + 0.5) * height / grid))
            for k, shift in enumerate((16, 8, 0)):
                channel = (value >> shift) & 0xFF
                low[k] = min(low[k], channel)
                high[k] = max(high[k], channel)
    return all(high[k] - low[k] <= tolerance for k in range(3))


def grab_region(widget, strategy, rect):
    """Copy ``rect`` (widget coordinates) of ``widget``; returns a QImage or None."""
    x, y, w, h = rect
    if strategy == STRATEGY_WINDOW:
        pixmap = grab_widget_window(widget, x, y, w, h)
    else:
        origin = widget.mapToGlobal(QtCore.QPoint(x, y))
        pixmap = grab_screen(origin.x(), origin.y(), w, h)
    if pixmap is None or pixmap.isNull():
        return None
    return pixmap.toImage()


class ViewportMirror(object):
    def __init__(self, root, exclude=None, hint=u"", finder=None, grabber=None):
        self.root = root
        self.exclude = exclude
        self.hint = to_text(hint or u"")
        self._finder = finder or (lambda: self._find())
        self._grab = grabber or grab_region
        self.widget = None
        self.strategy = STRATEGY_WINDOW
        self.status = u""
        self.last_rect = None  # global rect of the last capture
        self._blank_streak = 0
        self._locked = False  # a strategy produced a real image

    def _find(self):
        ranked = rank_candidates(self.root, self.exclude, self.hint)
        return ranked[0][3] if ranked else None

    def locate(self):
        widget = self.widget
        if widget is not None:
            try:
                if widget.isVisible():
                    return widget
            except RuntimeError:  # SFM rebuilt its layout
                pass
        self.widget = self._finder()
        return self.widget

    def viewport_rect(self):
        widget = self.locate()
        if widget is None:
            return None
        origin = widget.mapToGlobal(QtCore.QPoint(0, 0))
        return (origin.x(), origin.y(), widget.width(), widget.height())

    def capture(self, zoom=1.0):
        widget = self.locate()
        if widget is None:
            self.status = u"SFM's viewport was not found."
            return None
        rect = geometry.center_crop(widget.width(), widget.height(), zoom)
        origin = widget.mapToGlobal(QtCore.QPoint(rect[0], rect[1]))
        self.last_rect = (origin.x(), origin.y(), rect[2], rect[3])
        try:
            image = self._grab(widget, self.strategy, rect)
        except Exception as exc:
            image = None
            self.status = u"Capture failed: %s" % to_text(str(exc))
        if image is None or image.isNull():
            self._note_blank()
            if not self.status.startswith(u"Capture failed"):
                self.status = u"The viewport could not be captured."
            return None
        if is_blank(image):
            self._note_blank()
            self.status = u"The viewport image is empty (nothing visible, or capture unsupported)."
        else:
            self._blank_streak = 0
            self._locked = True
            self.status = u""
        return image

    def _note_blank(self):
        # Window capture of Direct3D content can come back black on some systems;
        # alternate strategies until one of them produces a real image.
        self._blank_streak += 1
        if not self._locked and self._blank_streak >= BLANK_SWITCH_AFTER:
            self._blank_streak = 0
            self.strategy = (STRATEGY_SCREEN if self.strategy == STRATEGY_WINDOW
                             else STRATEGY_WINDOW)
