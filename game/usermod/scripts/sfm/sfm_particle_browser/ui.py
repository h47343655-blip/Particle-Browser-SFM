# -*- coding: utf-8 -*-
"""Picker dialog: search, favorites, recents and a live viewport preview."""
from __future__ import absolute_import, division

import traceback
from collections import OrderedDict

from . import gamefs, viewport
from .compat import clock, to_text
from .qt import (MIDDLE_BUTTON, QShortcut, QtCore, QtGui, QtWidgets, as_widget, available_geometry, event_global_pos, event_pos,
                 html_escape, set_placeholder, wheel_steps)

MAX_ROWS = 3000
SEARCH_MAX_WIDTH = 430  # search box plus its folder button; the filter checkboxes sit on the right
FOLDERS_BUTTON_STYLE = (
    u"QToolButton#particleBrowserFoldersButton { background: palette(base);"
    u" border: 1px solid palette(mid); padding: 0px; }"
    u" QToolButton#particleBrowserFoldersButton:hover { background: palette(alternate-base); }"
    u" QToolButton#particleBrowserFoldersButton:pressed { background: palette(dark); }")
LOOP_FPS = 24  # SFM viewport mode
NATIVE_MAX_FPS = 60  # built-in renderer: the frame rate when frames are cheap (MAX_DUTY limits it)
# Built-in preview frame budget: the renderer runs on SFM's UI thread, so it must stay cheap.
FRAME_BUDGET = 0.012  # seconds of rendering per frame the resolution adapts to
MAX_DUTY = 0.35  # at most this share of the time spent rendering; lower the frame rate beyond it
MIN_RENDER_SCALE = 0.3
MAX_RENDER_PIXELS = 360000  # about 690x520; larger panels are upscaled
MAX_FRAME_INTERVAL = 0.25
STILL_FRAME_EVERY = 6  # without looping, refresh the mirrored frame every Nth tick
ZOOM_STEP = 1.25
DRAG_SENSITIVITY = 0.5  # camera drags in the built-in preview: half of the original speed
STAR = u"\u2605 "
VIEWS = ((u"all", u"All"), (u"favorites", u"Favorites"), (u"recent", u"Recent"))
PREVIEW_SOURCES = ((u"native", u"Built-in (own camera)"), (u"scene", u"SFM viewport"), (u"off", u"Off"))
DEFAULT_GRADIENT = (0x000000, 0x000000)  # the Particle Editor's preview is black
ROLE = QtCore.Qt.UserRole


def count_text(count, capacity):
    """The Particle Editor's overlay: ``"Particle Count:   37/   68"``."""
    if capacity:
        return u"Particle Count: %4d/ %4d" % (count, capacity)
    return u"Particle Count: %4d" % count


def parse_color(value):
    """``"#rrggbb"`` -> int, or None for the default / anything malformed."""
    text = to_text(value or u"").strip()
    if len(text) != 7 or not text.startswith(u"#"):
        return None
    try:
        return int(text[1:], 16)
    except ValueError:
        return None


def background_colors(value):
    """Top and bottom colours for the built-in renderer."""
    color = parse_color(value)
    return DEFAULT_GRADIENT if color is None else (color, color)


def burger_icon(color):
    """Three thin bars like the \u2630 glyph, drawn so the button never depends on the font."""
    pixmap = QtGui.QPixmap(10, 9)
    pixmap.fill(QtGui.QColor(0, 0, 0, 0))
    painter = QtGui.QPainter(pixmap)
    try:
        for y in (1, 4, 7):
            painter.fillRect(0, y, 10, 1, color)
    finally:
        painter.end()
    return QtGui.QIcon(pixmap)


class FoldersMenu(QtWidgets.QMenu):
    """Popup menu that stays open while folders are ticked on and off."""

    def mouseReleaseEvent(self, event):
        action = self.activeAction()
        if action is not None and action.isEnabled() and action.isCheckable():
            action.trigger()
            event.accept()
            return
        super(FoldersMenu, self).mouseReleaseEvent(event)


class FolderMenuRow(QtWidgets.QWidget):
    """Custom folder row painted like a native checkable QMenu item, with an X button."""

    def __init__(self, menu, title, count, checked, remove, toggle, parent=None):
        super(FolderMenuRow, self).__init__(parent)
        self._menu = menu
        self._title = title
        self._count = count
        self._checked = bool(checked)
        self._toggle = toggle
        self._remove_width = 24
        self.setMouseTracking(True)
        self.setMinimumHeight(QtGui.QFontMetrics(menu.font()).height() + 8)
        self.setMinimumWidth(max(220, menu.sizeHint().width()))

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 2, 0)
        layout.setSpacing(0)
        layout.addStretch(1)
        button = QtWidgets.QToolButton(self)
        button.setText(u"\u00d7")
        button.setAutoRaise(True)
        button.setFixedSize(self._remove_width, 22)
        button.setToolTip(u"Remove from the scan list; files will not be deleted.")
        button.clicked.connect(remove)
        layout.addWidget(button)

    def set_checked(self, checked):
        self._checked = bool(checked)
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        try:
            option = QtWidgets.QStyleOptionMenuItem()
            option.initFrom(self)
            option.rect = QtCore.QRect(0, 0, max(1, self.width() - self._remove_width), self.height())
            option.menuItemType = QtWidgets.QStyleOptionMenuItem.Normal
            option.checkType = QtWidgets.QStyleOptionMenuItem.NonExclusive
            option.checked = self._checked
            option.text = u"%s\t%s" % (self._title, self._count)
            option.font = self._menu.font()
            option.fontMetrics = QtGui.QFontMetrics(option.font)
            if self.underMouse():
                option.state |= QtWidgets.QStyle.State_Selected
            else:
                option.state &= ~QtWidgets.QStyle.State_Selected
            self._menu.style().drawControl(QtWidgets.QStyle.CE_MenuItem, option, painter, self._menu)
        finally:
            painter.end()

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and event.x() < self.width() - self._remove_width:
            self._toggle()
            event.accept()
            return
        super(FolderMenuRow, self).mouseReleaseEvent(event)


class BackgroundPicker(QtWidgets.QWidget):
    """A custom colour button; ``value`` is "" (default black) or "#rrggbb"."""

    changed = QtCore.Signal(object)

    def __init__(self, value=u"", parent=None):
        super(BackgroundPicker, self).__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._value = u""
        self._custom = u"#3a4a66"
        self.custom_button = QtWidgets.QPushButton(u"Custom\u2026")
        self.custom_button.setAutoDefault(False)
        self.custom_button.setToolTip(u"Pick any colour")
        self.custom_button.clicked.connect(self._pick_custom)
        layout.addWidget(self.custom_button)
        self.label = QtWidgets.QLabel()
        self.label.setMinimumWidth(56)
        layout.addWidget(self.label)
        layout.addStretch(1)
        self.set_value(value)

    def value(self):
        return self._value

    def set_value(self, value, emit=False):
        value = to_text(value or u"").lower()
        if value and parse_color(value) is None:
            value = u""
        self._value = value
        if value:
            self._custom = value
        self.label.setText(value.upper() if value else u"default")
        if emit:
            self.changed.emit(value)

    def _pick_custom(self):
        initial = QtGui.QColor(self._value or self._custom)
        color = QtWidgets.QColorDialog.getColor(initial, self, u"Background colour")
        if color.isValid():
            self.set_value(u"#%02x%02x%02x" % (color.red(), color.green(), color.blue()), emit=True)


class ViewportView(QtWidgets.QWidget):
    """Draws a preview frame (aspect kept) or a message; optionally drives a camera."""

    zoom_requested = QtCore.Signal(float)  # multiplicative factor; 0 resets
    orbit = QtCore.Signal(float, float)
    pan = QtCore.Signal(float, float)
    dolly = QtCore.Signal(float)
    reset_view = QtCore.Signal()

    def __init__(self, parent=None):
        super(ViewportView, self).__init__(parent)
        self.setObjectName("particleBrowserViewportMirror")
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self._image = None
        self._message = u""
        self._caption = u""
        self._count = u""
        self._warning = u""
        self._interactive = False
        self._last = None
        self.set_interactive(False)

    def set_interactive(self, interactive):
        self._interactive = bool(interactive)
        self._last = None
        if self._interactive:
            self.setToolTip(u"Drag: orbit \u00b7 middle-drag or Shift+drag: pan \u00b7 wheel or right-drag: zoom"
                            u" \u00b7 double-click: reset")
        else:
            self.setToolTip(u"Mouse wheel: zoom into the centre, double-click: reset.")

    def is_interactive(self):
        return self._interactive

    def has_frame(self):
        return self._image is not None

    def message(self):
        return self._message

    def set_frame(self, image, caption=u"", warning=u"", count=u""):
        self._image = image
        self._message = u""
        self._count = count
        self._caption = caption
        self._warning = warning
        self.update()

    def set_message(self, text):
        self._image = None
        self._message = text
        self._count = u""
        self._caption = u""
        self._warning = u""
        self.update()

    def sizeHint(self):
        return QtCore.QSize(480, 270)

    def paintEvent(self, _event):
        painter = QtGui.QPainter(self)
        try:
            painter.fillRect(self.rect(), QtGui.QColor(0, 0, 0))
            if self._image is not None and not self._image.isNull():
                size = self._image.size()
                size.scale(self.size(), QtCore.Qt.KeepAspectRatio)
                x = (self.width() - size.width()) // 2
                y = (self.height() - size.height()) // 2
                painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
                painter.drawImage(QtCore.QRect(x, y, size.width(), size.height()), self._image)
            if self._message:
                painter.setPen(QtGui.QColor(160, 160, 165))
                painter.drawText(self.rect().adjusted(16, 16, -16, -16),
                                 QtCore.Qt.AlignCenter | QtCore.Qt.TextWordWrap, self._message)
            if not self._message:
                self._paint_overlays(painter)
        finally:
            painter.end()

    def _paint_overlays(self, painter):
        """As in the Particle Editor: "Particle Count: n/ max" in the lower right corner, bold white.
        Frame stats sit small and grey in the lower left, a warning above them."""
        area = self.rect().adjusted(8, 6, -10, -6)
        if getattr(self, "_count", u""):
            font = QtGui.QFont(self.font())
            font.setPixelSize(12)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QtGui.QColor(0, 0, 0))
            painter.drawText(area.translated(1, 1), QtCore.Qt.AlignRight | QtCore.Qt.AlignBottom, self._count)
            painter.setPen(QtGui.QColor(255, 255, 255))
            painter.drawText(area, QtCore.Qt.AlignRight | QtCore.Qt.AlignBottom, self._count)
        small = QtGui.QFont(self.font())
        small.setPixelSize(10)
        painter.setFont(small)
        metrics = QtGui.QFontMetrics(small)
        if self._caption:
            painter.setPen(QtGui.QColor(150, 150, 150))
            painter.drawText(area, QtCore.Qt.AlignLeft | QtCore.Qt.AlignBottom, self._caption)
        if self._warning:
            painter.setPen(QtGui.QColor(255, 200, 90))
            lifted = area.adjusted(0, 0, 0, -(metrics.height() if self._caption else 0))
            painter.drawText(lifted, QtCore.Qt.AlignLeft | QtCore.Qt.AlignBottom, self._warning)

    def wheelEvent(self, event):
        steps = wheel_steps(event)
        if steps:
            if self._interactive:
                self.dolly.emit(steps)
            else:
                self.zoom_requested.emit(ZOOM_STEP ** steps)
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if self._interactive:
            self.reset_view.emit()
        else:
            self.zoom_requested.emit(0.0)
        event.accept()

    def mousePressEvent(self, event):
        if not self._interactive:
            return super(ViewportView, self).mousePressEvent(event)
        # While the camera is dragged the cursor is hidden and held where the drag started;
        # every move is measured from that point and the cursor is put back there.
        self._last = event_global_pos(event)
        self._anchor = QtCore.QPoint(self._last)
        self.setCursor(QtCore.Qt.BlankCursor)
        event.accept()

    def mouseMoveEvent(self, event):
        if not self._interactive or self._last is None:
            return super(ViewportView, self).mouseMoveEvent(event)
        pos = event_global_pos(event)
        dx, dy = float(pos.x() - self._last.x()), float(pos.y() - self._last.y())
        if dx == 0.0 and dy == 0.0:
            event.accept()  # the event our own cursor warp produced
            return
        dx *= DRAG_SENSITIVITY
        dy *= DRAG_SENSITIVITY
        buttons = event.buttons()
        shift = bool(event.modifiers() & QtCore.Qt.ShiftModifier)
        if buttons & MIDDLE_BUTTON or (buttons & QtCore.Qt.LeftButton and shift):
            self.pan.emit(dx, dy)
        elif buttons & QtCore.Qt.LeftButton:
            self.orbit.emit(dx, dy)
        elif buttons & QtCore.Qt.RightButton:
            self.dolly.emit(-dy / 40.0)
        self._last = self._hold_cursor(pos)
        event.accept()

    def _hold_cursor(self, pos):
        """Put the cursor back where the drag started; returns the point the next move counts from."""
        anchor = getattr(self, "_anchor", None)
        if anchor is None:
            return pos
        QtGui.QCursor.setPos(anchor)
        # a platform that refuses to move the cursor: count from where it is instead
        return anchor if QtGui.QCursor.pos() == anchor else pos

    def mouseReleaseEvent(self, event):
        if self._last is not None:
            self.unsetCursor()
            anchor = getattr(self, "_anchor", None)
            if anchor is not None:
                QtGui.QCursor.setPos(anchor)
        self._last = None
        self._anchor = None
        return super(ViewportView, self).mouseReleaseEvent(event)


class PickerDialog(QtWidgets.QDialog):
    def __init__(self, index, settings, bridge=None, parent=None,
                 rescan=None, log=None, mirror=None, native=None):
        super(PickerDialog, self).__init__(as_widget(parent))
        self.index = index
        self.settings = settings
        self.bridge = bridge
        self.rescan_callback = rescan
        self.log = log or (lambda message: None)
        self.mirror = mirror
        self.native_provider = native
        if mirror is not None:
            mirror.exclude = self  # never mirror our own widgets
        self.selected = None
        self._results = []
        self._session = None
        self._native_scene = None
        self._native_last = 0.0
        self._render_scale = 1.0
        self._render_cost = 0.0  # smoothed seconds per native frame
        self._frame_gap = 0.0  # smoothed seconds between drawn native frames (for the fps readout)
        self._last_frame_at = 0.0
        self._native_idle_key = None  # what the last frame showed when nothing was moving
        self._camera = None
        self._preview_name = None
        self._shot_range = None
        self._t0 = bridge.head_time() if self._preview_supported() else 0.0
        self._loop_clock = 0.0
        self._loop_cycle = 0
        self._frame_count = 0
        self._zoom = viewport.clamp_zoom(settings.get("mirror_zoom"))
        self._placed = False
        self._in_tick = False
        self._shortcuts = []
        self._notice = u""  # one-off note appended to the next status line
        self._folder_cache = None
        self._dim = QtGui.QBrush(QtGui.QColor(135, 135, 135))
        self.setWindowTitle(u"Particle Browser")
        self.resize(1180, 740)
        self._build()
        self._search_timer = self._single_shot(150, self._refresh_results)
        self._preview_timer = self._single_shot(150, self._apply_preview)
        self._frame_timer = QtCore.QTimer(self)
        if hasattr(self._frame_timer, "setTimerType") and hasattr(QtCore.Qt, "PreciseTimer"):
            self._frame_timer.setTimerType(QtCore.Qt.PreciseTimer)  # Qt 5+; Qt 4 has no timer types
        self._frame_timer.setInterval(int(round(1000.0 / LOOP_FPS)))
        self._frame_timer.timeout.connect(self._tick)
        self._refresh_results()
        self.search.setFocus()
        self.search.installEventFilter(self)
        self.tree.installEventFilter(self)

    # -- construction ------------------------------------------------------

    def _single_shot(self, msec, slot):
        timer = QtCore.QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(msec)
        timer.timeout.connect(slot)
        return timer

    def _spin(self, low, high, value, step, decimals, suffix):
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setDecimals(decimals)
        spin.setSingleStep(step)
        spin.setSuffix(suffix)
        spin.setValue(float(value))
        return spin

    def _info_label(self):
        label = QtWidgets.QLabel()
        label.setTextFormat(QtCore.Qt.RichText)
        label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        # long file paths must not widen the side panel
        label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        return label

    def _info_page(self):
        page = QtWidgets.QWidget()
        page.setObjectName("particleBrowserInfoPage")
        form = QtWidgets.QFormLayout(page)
        # Qt 4 styles default to FieldsStayAtSizeHint, which collapses the Ignored-width labels
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        self.info_name = self._info_label()
        self.info_source = self._info_label()
        self.info_file = self._info_label()
        self.info_defined = self._info_label()
        form.addRow(u"Name", self.info_name)
        form.addRow(u"Source", self.info_source)
        form.addRow(u"File", self.info_file)
        form.addRow(u"Defined in", self.info_defined)
        return page

    def _render_page(self):
        page = QtWidgets.QWidget()
        page.setObjectName("particleBrowserRenderPage")
        form = QtWidgets.QFormLayout(page)

        def row(*widgets):
            box = QtWidgets.QHBoxLayout()
            box.setContentsMargins(0, 0, 0, 0)
            for widget in widgets:
                box.addWidget(widget)
            box.addStretch(1)
            return box

        form.addRow(u"Renderer", row(self.source, self.restart_button))
        form.addRow(u"Loop", row(self.loop, self.loop_seconds))
        form.addRow(u"Distance", row(self.distance, self.drive_render))
        form.addRow(u"Grid", row(self.grid))
        form.addRow(u"Background", self.background)
        return page

    def _build(self):
        s = self.settings
        self.search = QtWidgets.QLineEdit(to_text(s.get("last_query") or u""))
        set_placeholder(self.search, u"Search by name or file (all words must match)")
        if hasattr(self.search, "setClearButtonEnabled"):
            self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _text: self._search_timer.start())
        self.search.returnPressed.connect(self._focus_results)
        self.folders_button = QtWidgets.QToolButton()
        self.folders_button.setObjectName("particleBrowserFoldersButton")
        self.folders_button.setText(u"\u2630")
        self.folders_button.setIcon(burger_icon(QtGui.QColor(200, 200, 200)))
        self.folders_button.setIconSize(QtCore.QSize(10, 9))
        self.folders_button.setFixedSize(22, max(20, self.search.sizeHint().height()))
        # drawn like part of the search field (as in the mockup), not as a raised tool button
        self.folders_button.setStyleSheet(FOLDERS_BUTTON_STYLE)
        self.folders_button.setToolTip(u"Folders to scan")
        self.folders_button.clicked.connect(self._show_folders_menu)
        self.folders_menu = FoldersMenu(self)
        self.folders_menu.setObjectName("particleBrowserFoldersMenu")
        self.folders_menu.aboutToShow.connect(self._fill_folders_menu)
        search_box = QtWidgets.QWidget()
        search_row = QtWidgets.QHBoxLayout(search_box)
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(2)
        search_row.addWidget(self.search, 1)
        search_row.addWidget(self.folders_button)
        search_box.setMaximumWidth(SEARCH_MAX_WIDTH)
        self.view = QtWidgets.QComboBox()
        for key, label in VIEWS:
            self.view.addItem(label, key)
        keys = [key for key, _ in VIEWS]
        self.view.setCurrentIndex(keys.index(s.get("view")) if s.get("view") in keys else 0)
        self.view.currentIndexChanged.connect(lambda _i: self._refresh_results())
        self.hide_children = QtWidgets.QCheckBox(u"Hide child systems")
        self.hide_children.setChecked(bool(s.get("hide_children")))
        self.hide_children.setToolTip(u"Child systems are normally spawned by a parent effect.")
        self.hide_children.toggled.connect(lambda _c: self._refresh_results())
        self.group_by_file = QtWidgets.QCheckBox(u"Group by file")
        self.group_by_file.setChecked(bool(s.get("group_by_file")))
        self.group_by_file.toggled.connect(lambda _c: self._refresh_results())

        filters = QtWidgets.QHBoxLayout()
        filters.addWidget(search_box, 1)
        filters.addWidget(QtWidgets.QLabel(u"Show:"))
        filters.addWidget(self.view)
        filters.addStretch(1)
        filters.addWidget(self.hide_children)
        filters.addSpacing(8)
        filters.addWidget(self.group_by_file)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels([u"Particle system", u"File", u"Source"])
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 270)
        self.tree.setColumnWidth(1, 210)
        self.tree.currentItemChanged.connect(self._on_current_changed)
        self.tree.itemActivated.connect(self._on_item_activated)

        side = QtWidgets.QWidget()
        side_layout = QtWidgets.QVBoxLayout(side)
        side_layout.setContentsMargins(6, 0, 0, 0)

        self.viewport_view = ViewportView()
        self.viewport_view.zoom_requested.connect(self._on_zoom_requested)
        self.viewport_view.orbit.connect(self._on_orbit)
        self.viewport_view.pan.connect(self._on_pan)
        self.viewport_view.dolly.connect(self._on_dolly)
        self.viewport_view.reset_view.connect(self._on_reset_view)
        side_layout.addWidget(self.viewport_view, 1)

        self.source = QtWidgets.QComboBox()
        self.source.setToolTip(u"Built-in: the plugin's own renderer with its own camera.\n"
                               u"SFM viewport: the system is placed in the shot and SFM's viewport is shown here.")
        for key, label in PREVIEW_SOURCES:
            self.source.addItem(label, key)
        self._init_source(to_text(s.get("preview_source") or u"native"))
        self.loop = QtWidgets.QCheckBox()
        self.loop.setChecked(bool(s.get("loop_preview")))
        self.loop.setToolTip(u"Restart the effect after the loop length. In SFM viewport mode the"
                             u" playhead moves and is restored when the window closes.")
        self.loop.toggled.connect(lambda _c: self._on_loop_toggled())
        self.loop_seconds = self._spin(0.5, 30.0, s.get("loop_seconds"), 0.5, 1, u" s")
        self.loop_seconds.setToolTip(u"Loop length")
        self.distance = self._spin(8.0, 4096.0, s.get("preview_distance"), 16.0, 0, u" units")
        self.distance.setToolTip(u"SFM viewport mode: distance in front of the shot camera.")
        self.distance.valueChanged.connect(lambda _v: self._schedule_preview(force=True))
        self.drive_render = QtWidgets.QCheckBox(u"Render while open")
        self.drive_render.setChecked(bool(s.get("drive_render")))
        self.drive_render.setToolTip(u"SFM viewport mode: calls sfmApp.ProcessEvents() for each preview"
                                     u" frame so the engine keeps drawing while this window is open.")
        self.grid = QtWidgets.QCheckBox()
        self.grid.setChecked(bool(s.get("show_grid")))
        self.grid.setToolTip(u"Built-in mode: ground grid and control point axes.")
        self.grid.toggled.connect(self._on_grid_toggled)
        self.background = BackgroundPicker(s.get("preview_background"))
        self.background.setToolTip(u"Built-in mode: viewport background.")
        self.background.changed.connect(self._on_background_changed)
        self.restart_button = QtWidgets.QPushButton(u"Restart")
        self.restart_button.setToolTip(u"Restart the effect")
        self.restart_button.clicked.connect(self._restart_effect)

        # Notes about the preview sit under the viewport so they stay visible on either tab.
        self.preview_status = QtWidgets.QLabel()
        self.preview_status.setWordWrap(True)
        self.preview_status.setTextFormat(QtCore.Qt.RichText)
        side_layout.addWidget(self.preview_status)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setObjectName("particleBrowserTabs")
        # The Windows XP/Vista styles SFM runs with fill tab pages with the light theme colour and
        # ignore SFM's dark palette, which left light text on white. Draw the pane from the palette.
        self.tabs.setStyleSheet(
            u"QTabWidget#particleBrowserTabs::pane { background: palette(window);"
            u" border: 1px solid palette(dark); top: -1px; }"
            u" QWidget#particleBrowserInfoPage, QWidget#particleBrowserRenderPage"
            u" { background: transparent; }")
        # extra height goes to the viewport, not to empty space in the pane
        self.tabs.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Maximum)
        self.tabs.addTab(self._info_page(), u"Info")
        self.tabs.addTab(self._render_page(), u"Render")
        tab = s.get("side_tab")
        self.tabs.setCurrentIndex(tab if tab in (0, 1) else 0)
        side_layout.addWidget(self.tabs)

        self.source.currentIndexChanged.connect(self._on_source_changed)
        self._update_controls()
        self._show_idle_message()

        row = QtWidgets.QHBoxLayout()
        self.favorite_button = QtWidgets.QPushButton(u"Add to favorites")
        self.favorite_button.clicked.connect(self._toggle_favorite)
        self.copy_button = QtWidgets.QPushButton(u"Copy name")
        self.copy_button.clicked.connect(self._copy_name)
        row.addWidget(self.favorite_button)
        row.addWidget(self.copy_button)
        side_layout.addLayout(row)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.addWidget(self.tree)
        splitter.addWidget(side)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([620, 540])

        self.status = QtWidgets.QLabel()
        bottom = QtWidgets.QHBoxLayout()
        bottom.addWidget(self.status, 1)
        if self.rescan_callback is not None:
            rescan = QtWidgets.QPushButton(u"Rescan files")
            rescan.setAutoDefault(False)
            rescan.clicked.connect(self._rescan)
            bottom.addWidget(rescan)
        self.buttons = QtWidgets.QDialogButtonBox()
        self.accept_button = self.buttons.addButton(u"Use particle", QtWidgets.QDialogButtonBox.AcceptRole)
        cancel = self.buttons.addButton(QtWidgets.QDialogButtonBox.Cancel)
        for button in (self.accept_button, cancel, self.favorite_button, self.copy_button,
                       self.restart_button):
            # Enter in the search box must not accept the dialog.
            button.setAutoDefault(False)
            button.setDefault(False)
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)
        bottom.addWidget(self.buttons)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(filters)
        layout.addWidget(splitter, 1)
        layout.addLayout(bottom)

        for keys, slot in ((u"Ctrl+F", self._focus_search), (u"Ctrl+D", self._toggle_favorite)):
            shortcut = QShortcut(QtGui.QKeySequence(keys), self)
            shortcut.activated.connect(slot)
            self._shortcuts.append(shortcut)

    # -- results -----------------------------------------------------------

    def _current_view(self):
        return to_text(self.view.itemData(self.view.currentIndex())) or u"all"

    def _query_results(self):
        query = self.search.text()
        view = self._current_view()
        if view == u"recent":
            order = dict((to_text(n), i) for i, n in enumerate(self.settings.get("recent")))
            results = [r for r in self.index.search(query, hide_children=False) if r.name in order]
            results.sort(key=lambda r: (order[r.name], r.pcf.lower()))
            return self._enabled_only(results)
        return self._enabled_only(self.index.search(query, hide_children=self.hide_children.isChecked(),
                                                    favorites=self.settings.favorites(),
                                                    favorites_only=(view == u"favorites")))

    def _refresh_results(self):
        keep = self._current_record()
        keep_key = (keep.name, keep.pcf) if keep is not None else None
        self._results = self._query_results()
        shown = self._results[:MAX_ROWS]
        favorites = self.settings.favorites()
        grouped = self.group_by_file.isChecked()
        select = None
        self.tree.setUpdatesEnabled(False)
        self.tree.blockSignals(True)
        try:
            self.tree.clear()
            self.tree.setRootIsDecorated(grouped)
            if grouped:
                groups = OrderedDict()
                for i, record in enumerate(shown):
                    groups.setdefault(record.pcf, []).append(i)
                expand = bool(self.search.text().strip()) or len(groups) <= 3
                for pcf_path in sorted(groups, key=lambda p: p.lower()):
                    indices = groups[pcf_path]
                    parent = QtWidgets.QTreeWidgetItem(
                        [pcf_path, u"%d systems" % len(indices), shown[indices[0]].origin])
                    for i in indices:
                        item = self._make_item(shown[i], i, favorites)
                        parent.addChild(item)
                        if keep_key == (shown[i].name, shown[i].pcf):
                            select = item
                    self.tree.addTopLevelItem(parent)
                    parent.setExpanded(expand or select is not None)
            else:
                items = []
                for i, record in enumerate(shown):
                    item = self._make_item(record, i, favorites)
                    items.append(item)
                    if keep_key == (record.name, record.pcf):
                        select = item
                self.tree.addTopLevelItems(items)
        finally:
            self.tree.blockSignals(False)
            self.tree.setUpdatesEnabled(True)
        if select is None:
            select = self._first_record_item()
        if select is not None:
            self.tree.setCurrentItem(select)
            self.tree.scrollToItem(select)
        else:
            self._on_current_changed(None, None)
        self._update_status()

    def _make_item(self, record, position, favorites):
        label = (STAR if record.name in favorites else u"") + record.name
        item = QtWidgets.QTreeWidgetItem([label, record.pcf, record.origin])
        item.setData(0, ROLE, position)
        tips = []
        if record.flags:
            item.setForeground(0, self._dim)
            tips.append(u"Child system: normally spawned by its parent effect.")
        count = self.index.name_counts.get(record.name, 1)
        if count > 1:
            tips.append(u"This name is defined in %d files; SFM uses whichever it loaded"
                        u" last." % count)
        if tips:
            item.setToolTip(0, u"\n".join(tips))
        return item

    def _first_record_item(self):
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if self._record_for(item) is not None:
                return item
            if item.childCount():
                return item.child(0)
        return None

    def _record_for(self, item):
        if item is None:
            return None
        try:
            position = int(item.data(0, ROLE))
        except (TypeError, ValueError):
            return None
        if 0 <= position < len(self._results):
            return self._results[position]
        return None

    def _current_record(self):
        return self._record_for(self.tree.currentItem()) if hasattr(self, "tree") else None

    def _update_status(self):
        matching = len(self._results)
        _custom, disabled, counts = self.folder_state()
        systems, files, enabled, total = gamefs.totals(self.index, counts, disabled)
        text = u"%d matching \u00b7 %d systems in %d files" % (matching, systems, files)
        if enabled < total:
            text += u" \u00b7 %d/%d folders" % (enabled, total)
        if matching > MAX_ROWS:
            text += u" \u00b7 showing the first %d, refine the search" % MAX_ROWS
        if self.index.errors:
            text += u" \u00b7 %d file(s) could not be read" % len(self.index.errors)
            self.status.setToolTip(u"\n".join(u"%s: %s" % item for item in self.index.errors[:20]))
        if self._notice:
            text += u" \u00b7 " + self._notice
            self._notice = u""
        self.status.setText(text)

    # -- scan folders (the button next to the search box) ----------------------

    def folder_state(self):
        """``(custom folders, unticked folder keys, counts)``; counts are cached per index."""
        custom = gamefs.custom_folders(self.settings)
        key = (self.index, len(self.index.records), tuple(custom))
        if self._folder_cache is None or self._folder_cache[0] != key:
            self._folder_cache = (key, gamefs.folder_counts(self.index, custom))
        return custom, gamefs.disabled_folders(self.settings), self._folder_cache[1]

    def _enabled_only(self, results):
        disabled = gamefs.disabled_folders(self.settings)
        if not disabled:
            return results
        return gamefs.only_enabled(results, disabled, gamefs.custom_folders(self.settings))

    def _show_folders_menu(self):
        button = self.folders_button
        self.folders_menu.popup(button.mapToGlobal(QtCore.QPoint(0, button.height())))

    def _fill_folders_menu(self):
        menu = self.folders_menu
        menu.clear()
        custom, disabled, counts = self.folder_state()
        header = menu.addAction(u"SCAN THESE FOLDERS")
        header.setEnabled(False)
        custom_set = set(custom)
        for key, (files, _systems) in counts.items():
            if key in custom_set:
                row = FolderMenuRow(
                    menu, gamefs.menu_text(key), gamefs.files_text(files), key not in disabled,
                    lambda _checked=False, p=key: self.remove_scan_folder(p),
                    lambda k=key: self.set_folder_enabled(k, k in gamefs.disabled_folders(self.settings)))
                action = QtWidgets.QWidgetAction(menu)
                action.setDefaultWidget(row)
                menu.addAction(action)
            else:
                action = menu.addAction(u"%s\t%s" % (
                    gamefs.menu_text(gamefs.folder_title(key, custom)), gamefs.files_text(files)))
                action.setCheckable(True)
                action.setChecked(key not in disabled)
                action.toggled.connect(lambda checked, k=key: self.set_folder_enabled(k, checked))
        menu.addSeparator()
        browse = menu.addAction(u"Browse for a folder\u2026")
        browse.triggered.connect(lambda _checked=False: self._browse_folder())

    def set_folder_enabled(self, key, enabled):
        disabled = gamefs.disabled_folders(self.settings)
        if enabled:
            disabled.discard(key)
        else:
            disabled.add(key)
        self.settings.set("scan_disabled", sorted(disabled))
        self._save_quietly()
        self._refresh_results()
        # Keep the inline checkmark current while the popup remains open.
        for action in self.folders_menu.actions():
            row = action.defaultWidget() if isinstance(action, QtWidgets.QWidgetAction) else None
            if isinstance(row, FolderMenuRow) and row._title == gamefs.menu_text(key):
                row.set_checked(enabled)

    def _browse_folder(self):
        custom = gamefs.custom_folders(self.settings)
        start = custom[-1] if custom else (gamefs.find_game_dir() or u"")
        path = QtWidgets.QFileDialog.getExistingDirectory(self, u"Choose a folder to scan", start)
        if isinstance(path, tuple):
            path = path[0]
        path = to_text(path or u"").strip()
        if path:
            self.add_scan_folder(path)

    def add_scan_folder(self, path, game_dir=None):
        """Tick the listed folder ``path`` points at, or add it as a new folder and rescan."""
        custom, disabled, counts = self.folder_state()
        if game_dir is None:
            game_dir = gamefs.find_game_dir()
        key = gamefs.known_folder(path, counts, custom, game_dir)
        if key is not None:
            self._notice = u"%s is already in the list \u2014 enabled" % gamefs.folder_title(key, custom)
            self.set_folder_enabled(key, True)
            return key
        label = gamefs.custom_label(path)
        disabled.discard(label)
        self.settings.set("scan_custom", custom + [label])
        self.settings.set("scan_disabled", sorted(disabled))
        self._save_quietly()
        self._notice = u"added %s" % label
        if not self._rescan():
            self._refresh_results()
        return label

    def remove_scan_folder(self, path):
        disabled = gamefs.disabled_folders(self.settings)
        disabled.discard(path)
        self.settings.set("scan_custom", [p for p in gamefs.custom_folders(self.settings) if p != path])
        self.settings.set("scan_disabled", sorted(disabled))
        self._save_quietly()
        self._notice = u"removed %s" % path
        if not self._rescan():
            self._refresh_results()
        if self.folders_menu.isVisible():
            QtCore.QTimer.singleShot(0, self._fill_folders_menu)

    # -- selection and details ----------------------------------------------

    def _on_current_changed(self, current, _previous):
        record = self._record_for(current)
        self._show_details(record)
        if record is not None and record.name != self._preview_name:
            self._schedule_preview()

    def _on_item_activated(self, item, _column):
        if self._record_for(item) is not None:
            self._on_accept()

    def _show_details(self, record):
        enabled = record is not None
        self.favorite_button.setEnabled(enabled)
        self.copy_button.setEnabled(enabled)
        self.accept_button.setEnabled(enabled)
        esc = html_escape
        if record is None:
            self.info_name.setText(u"<i>Select a particle system.</i>")
            for label in (self.info_source, self.info_file, self.info_defined):
                label.setText(u"")
                label.setToolTip(u"")
            return
        self.info_name.setText(u"<b style='font-size:14px'>%s</b>" % esc(record.name))
        self.info_name.setToolTip(record.name)
        self.info_source.setText(esc(record.origin))
        self.info_source.setToolTip(record.origin)
        self.info_file.setText(esc(record.pcf))
        self.info_file.setToolTip(record.pcf)
        count = self.index.name_counts.get(record.name, 1)
        if count > 1:
            self.info_defined.setText(u"<span style='color:#c08000'>%d files</span>"
                                      u" <span style='color:#878787'>\u2014 SFM uses whichever it loaded last</span>"
                                      % count)
        else:
            self.info_defined.setText(u"1 file")
        self.info_defined.setToolTip(u"")
        favorite = self.settings.is_favorite(record.name)
        self.favorite_button.setText(u"Remove from favorites" if favorite else u"Add to favorites")

    def _toggle_favorite(self):
        record = self._current_record()
        if record is None:
            return
        self.settings.toggle_favorite(record.name)
        self._save_quietly()
        self._refresh_results()

    def _copy_name(self):
        record = self._current_record()
        if record is None:
            self.status.setText(u"Select a particle system first.")
        else:
            QtWidgets.QApplication.clipboard().setText(record.name)
            self.status.setText(u"Copied %s to the clipboard." % record.name)

    def _focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def _focus_results(self):
        if self.tree.currentItem() is None:
            first = self._first_record_item()
            if first is not None:
                self.tree.setCurrentItem(first)
        self.tree.setFocus()

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.KeyPress and watched in (self.search, self.tree):
            if self._filter_dialog_key(event):
                return True
        return super(PickerDialog, self).eventFilter(watched, event)

    def keyPressEvent(self, event):
        if self._filter_dialog_key(event):
            return
        super(PickerDialog, self).keyPressEvent(event)

    def _filter_dialog_key(self, event):
        key = event.key()
        focused = QtWidgets.QApplication.focusWidget()
        in_search = focused is self.search or self.search.hasFocus()
        if key == QtCore.Qt.Key_Escape and in_search and self.search.text():
            # First Esc clears the query; a second Esc cancels the dialog.
            self.search.clear()
            event.accept()
            return True
        if key == QtCore.Qt.Key_Down and in_search:
            self._focus_results()
            event.accept()
            return True
        if key in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter) and in_search:
            # Enter in the search box moves to the results; itemActivated handles the list.
            self._focus_results()
            event.accept()
            return True
        return False

    def _rescan(self):
        """Rebuild the index; returns whether the results were refreshed."""
        if self.rescan_callback is None:
            return False
        index = self.rescan_callback(self)
        if index is None:
            return False
        self.index = index
        self._refresh_results()
        return True

    # -- preview -------------------------------------------------------------

    def _preview_supported(self):
        """In-scene preview through SFM (needs the SFM bridge)."""
        return self.bridge is not None and getattr(self.bridge, "available", False)

    def _native(self, force=False):
        if self.native_provider is None:
            return None
        return self.native_provider.get(force=force)

    def _native_reason(self):
        return to_text(getattr(self.native_provider, "reason", None) or u"")

    def _init_source(self, wanted):
        if wanted == u"native" and self.native_provider is None:
            wanted = u"scene" if self._preview_supported() else u"off"
        if wanted == u"scene" and not self._preview_supported():
            wanted = u"native" if self.native_provider is not None else u"off"
        self.set_preview_source(wanted)
        model = self.source.model()
        for i in range(self.source.count()):
            key = to_text(self.source.itemData(i))
            off = (key == u"native" and self.native_provider is None) or (key == u"scene" and not self._preview_supported())
            try:
                item = model.item(i)
            except AttributeError:
                item = None
            if item is not None and off:
                item.setEnabled(False)

    def preview_source(self):
        return to_text(self.source.itemData(self.source.currentIndex())) or u"off"

    def set_preview_source(self, key):
        for i in range(self.source.count()):
            if to_text(self.source.itemData(i)) == key:
                self.source.setCurrentIndex(i)
                return True
        return False

    def _update_controls(self):
        source = self.preview_source()
        scene_mode = source == u"scene" and self._preview_supported()
        native_mode = source == u"native"
        active = scene_mode or native_mode
        for widget in (self.loop, self.restart_button):
            widget.setEnabled(active)
        self.loop_seconds.setEnabled(active and self.loop.isChecked())
        self.distance.setEnabled(scene_mode)
        self.drive_render.setEnabled(scene_mode)
        self.grid.setEnabled(native_mode)
        self.background.setEnabled(native_mode)
        self.viewport_view.set_interactive(native_mode)

    def _on_loop_toggled(self):
        self._update_controls()
        self._restart_loop()

    def _schedule_preview(self, force=False):
        if force:
            self._preview_name = None
        if self.preview_source() != u"off":
            self._preview_timer.start()

    def _set_preview_status(self, html, error=False):
        color = u"#d04040" if error else u"#6a9a4a"
        self.preview_status.setText(u"<span style='color:%s'>%s</span>" % (color, html))

    def _apply_preview(self):
        record = self._current_record()
        source = self.preview_source()
        if record is None or source == u"off":
            return
        if source == u"native":
            self._apply_native(record)
        elif self._preview_supported():
            self._apply_scene(record)

    def _apply_scene(self, record):
        try:
            if self._session is None:
                self._session = self.bridge.start_preview()
                self._shot_range = self.bridge.shot_range(self._session.shot)
            position = self.bridge.point_in_front_of_camera(self._session.shot,
                                                            self.distance.value())
            self.bridge.show_preview(self._session, record.name, position)
            self._preview_name = record.name
            self._set_preview_status(u"Previewing <b>%s</b>" % html_escape(record.name))
            self._restart_loop()
        except Exception as exc:
            self._preview_name = None
            self._frame_timer.stop()
            self._set_preview_status(u"Preview failed: %s" % html_escape(to_text(str(exc))),
                                     error=True)
            self.log(traceback.format_exc())

    def _apply_native(self, record):
        renderer = self._native()
        self._close_native()
        if renderer is None:
            self._preview_name = None
            self.viewport_view.set_message(self._native_unavailable_text())
            return
        try:
            scene = renderer.scene_for(record.name, record.pcf)
        except Exception as exc:
            self._preview_name = None
            self.viewport_view.set_message(u"The built-in renderer cannot show %s:\n%s"
                                           % (record.name, to_text(str(exc))))
            self.preview_status.setText(u"")
            self.log(traceback.format_exc())
            return
        self._apply_scene_options(scene)
        self._native_scene = scene
        self._preview_name = record.name
        if self._camera is None:
            from .native import OrbitCamera
            self._camera = OrbitCamera()
        self._camera.reset()
        self._loop_cycle = 0
        self._native_last = clock()
        self._native_idle_key = None
        self._set_native_status(scene)
        self._tick()
        self._frame_timer.start()

    def _native_unavailable_text(self):
        reason = self._native_reason()
        return u"The built-in renderer is not available%s." % ((u":\n" + reason) if reason else u"")

    def _set_native_status(self, scene):
        missing = scene.issue_names(u"not rendered", u"unsupported")
        approx = scene.issue_names(u"approximated")
        material_issues = getattr(scene, "material_issues", ())
        missing += [text for status, text in material_issues if status == u"not rendered"]
        textures = [text for status, text in material_issues if status == u"missing"]
        parts = []
        if missing:
            parts.append(u"<span style='color:#d08a30'>Not drawn: %s</span>" % html_escape(u", ".join(missing)))
        if textures:
            parts.append(u"<span style='color:#d08a30'>Missing (drawn as white dots): %s</span>"
                         % html_escape(u"; ".join(textures)))
        if approx:
            parts.append(u"<span style='color:#909090'>Approximated: %s</span>" % html_escape(u", ".join(approx)))
        if not parts:
            parts.append(u"<span style='color:#6a9a4a'>Built-in renderer</span>")
        self.preview_status.setText(u"<br>".join(parts))

    def _close_native(self):
        scene, self._native_scene = self._native_scene, None
        self._native_idle_key = None
        self._set_frame_interval(1.0 / LOOP_FPS)  # SFM viewport mode keeps its own pace
        if scene is not None:
            try:
                scene.close()
            except Exception:
                self.log(traceback.format_exc())

    def _restart_loop(self):
        self._loop_clock = clock()
        self._loop_cycle = 0
        self._frame_count = 0
        if self._native_scene is not None:
            self._native_scene.restart()
            self._native_last = clock()
            self._frame_timer.start()
            return
        if self._session is None:
            self._frame_timer.stop()
            return
        if not self.loop.isChecked():
            self.bridge.refresh_viewport(restart_time=True,
                                         render=self.drive_render.isChecked())
            self._update_mirror()
        if self.loop.isChecked() or self.mirror is not None:
            self._frame_timer.start()
        else:
            self._frame_timer.stop()

    def loop_window(self):
        """Return ``(base, length)`` of the loop, kept inside the shot."""
        length = max(0.25, self.loop_seconds.value())
        base = self._t0
        if self._shot_range is not None:
            start, end = self._shot_range
            length = min(length, end - start)
            base = max(start, min(base, end - length))
        return base, length

    def _tick(self):
        if self._in_tick:
            return
        if self._native_scene is not None:
            self._in_tick = True
            try:
                self._tick_native()
            finally:
                self._in_tick = False
            return
        if self._session is None:
            return
        self._in_tick = True  # ProcessEvents() below can re-enter this timer
        try:
            looping = self.loop.isChecked()
            self._frame_count += 1
            if looping:
                base, length = self.loop_window()
                elapsed = clock() - self._loop_clock
                cycle = int(elapsed // length)
                if cycle != self._loop_cycle:
                    self._loop_cycle = cycle
                    self.bridge.restart_preview(self._session)
                self.bridge.set_head_time(base + (elapsed - cycle * length))
            if looping or self._frame_count % STILL_FRAME_EVERY == 0:
                if self.drive_render.isChecked():
                    self.bridge.render()
                self._update_mirror()
        except Exception as exc:
            self._frame_timer.stop()
            self._set_preview_status(u"Preview stopped: %s" % html_escape(to_text(str(exc))),
                                     error=True)
            self.log(traceback.format_exc())
        finally:
            self._in_tick = False

    def _tick_native(self):
        scene = self._native_scene
        now = clock()
        dt = min(MAX_FRAME_INTERVAL + 0.05, max(0.0, now - self._native_last))
        self._native_last = now
        try:
            if self.loop.isChecked() and scene.time >= max(0.25, self.loop_seconds.value()):
                self._loop_cycle += 1
                scene.restart(self._loop_cycle + 1)
            scene.step(dt)
            count = scene.particle_count()
            self._camera.fit(scene.bounds(), dt)
            scene.set_camera(self._camera)
            width, height = self.native_render_size()
            # an empty scene under a still camera looks the same as the last frame
            key = None if count else (width, height, self._camera.yaw, self._camera.pitch,
                                      self._camera.distance, tuple(self._camera.target))
            if key is not None and key == self._native_idle_key and self.viewport_view.has_frame():
                return
            started = clock()
            data, thinned = scene.render(width, height)
            finished = clock()
            self._adapt_scale(finished - started)
            self._note_frame(finished)
            self._native_idle_key = key
            image = QtGui.QImage(data, width, height, width * 4, QtGui.QImage.Format_RGB32).copy()
        except Exception as exc:
            self._frame_timer.stop()
            self._close_native()
            self.viewport_view.set_message(u"Built-in preview stopped: %s" % to_text(str(exc)))
            self.log(traceback.format_exc())
            return
        caption = self.native_stats_text(width, height)
        warning = u"Simplified: many large particles" if thinned else u""
        capacity = scene.capacity() if hasattr(scene, "capacity") else None
        self.viewport_view.set_frame(image, caption, warning, count_text(count, capacity))

    def _note_frame(self, now):
        if self._last_frame_at:
            gap = now - self._last_frame_at
            if 0.0 < gap < 1.0:
                self._frame_gap = gap if not self._frame_gap else self._frame_gap * 0.8 + gap * 0.2
        self._last_frame_at = now

    def native_stats_text(self, width, height):
        """``"690\u00d7520 \u00b7 6.1 ms \u00b7 30 fps"``: render size, smoothed render time and frame rate."""
        text = u"%d\u00d7%d \u00b7 %.1f ms" % (width, height, self._render_cost * 1000.0)
        if self._frame_gap > 0.0:
            text += u" \u00b7 %d fps" % int(round(1.0 / self._frame_gap))
        return text

    def native_render_size(self):
        view_w, view_h = max(1, self.viewport_view.width()), max(1, self.viewport_view.height())
        scale = min(self._render_scale, (MAX_RENDER_PIXELS / float(view_w * view_h)) ** 0.5)
        return max(64, int(view_w * scale)), max(36, int(view_h * scale))

    def _adapt_scale(self, cost):
        """Keep rendering within FRAME_BUDGET by resolution, then within MAX_DUTY by frame rate."""
        self._render_cost = cost if not self._render_cost else self._render_cost * 0.7 + cost * 0.3
        if cost > FRAME_BUDGET * 1.25 and self._render_scale > MIN_RENDER_SCALE:
            # pixels cost the square of the scale
            factor = max(0.7, min(0.95, (FRAME_BUDGET / cost) ** 0.5))
            self._render_scale = max(MIN_RENDER_SCALE, self._render_scale * factor)
        elif cost < FRAME_BUDGET * 0.5 and self._render_scale < 1.0:
            self._render_scale = min(1.0, self._render_scale * 1.08)
        interval = max(1.0 / NATIVE_MAX_FPS, min(MAX_FRAME_INTERVAL, self._render_cost / MAX_DUTY))
        self._set_frame_interval(interval)

    def _set_frame_interval(self, seconds):
        msec = int(round(seconds * 1000))
        if self._frame_timer.interval() != msec:
            self._frame_timer.setInterval(msec)

    def _on_orbit(self, dx, dy):
        if self._camera is not None:
            self._camera.orbit(dx, dy)
            self._kick_native()

    def _on_pan(self, dx, dy):
        if self._camera is not None:
            self._camera.pan(dx, dy, max(1, self.viewport_view.height()))
            self._kick_native()

    def _on_dolly(self, steps):
        if self._camera is not None:
            self._camera.dolly(steps)
            self._kick_native()

    def _on_reset_view(self):
        if self._camera is not None:
            self._camera.reset()
            self._kick_native()

    def _kick_native(self):
        """Camera input: draw now when a frame is nearly due, and restart the timer from here.

        The view then follows the mouse without waiting up to a whole frame interval, while
        frames stay about one interval apart (the MAX_DUTY limit still holds).
        """
        if self._native_scene is None or self._in_tick or not self._frame_timer.isActive():
            return
        due = self._frame_timer.interval() / 1000.0 * 0.75
        if clock() - self._native_last >= due:
            self._tick()
            if self._native_scene is not None and self._frame_timer.isActive():
                self._frame_timer.start()

    def _on_grid_toggled(self, checked):
        if self._native_scene is not None:
            self._apply_scene_options(self._native_scene)

    def _on_background_changed(self, _value):
        if self._native_scene is not None:
            self._apply_scene_options(self._native_scene)

    def _apply_scene_options(self, scene):
        self._native_idle_key = None
        top, bottom = background_colors(self.background.value())
        grid = self.grid.isChecked()
        try:
            scene.set_options(grid=grid, axes=grid, top=top, bottom=bottom, editor_grid=True)
        except TypeError:  # a scene object without the Particle Editor grid
            scene.set_options(grid=grid, axes=grid, top=top, bottom=bottom)

    # -- viewport mirror -------------------------------------------------------

    def _show_idle_message(self):
        source = self.preview_source()
        if source == u"off":
            text = u"Preview is off."
        elif source == u"native":
            if self.native_provider is None or getattr(self.native_provider, "failed", False):
                text = self._native_unavailable_text()
            else:
                text = u"Select a particle system to preview it here."
        elif not self._preview_supported():
            text = u"The SFM viewport preview is only available inside SFM."
        elif self.mirror is None:
            text = u"The preview is drawn in SFM's main viewport."
        else:
            text = u"Select a particle system to preview it here."
        self.viewport_view.set_message(text)

    def _covering_viewport(self):
        rect = self.mirror.last_rect if self.mirror is not None else None
        if rect is None or not self.isVisible():
            return False
        frame = self.frameGeometry()
        mine = (frame.x(), frame.y(), frame.width(), frame.height())
        return viewport.overlap_area(mine, rect) > 0

    def _update_mirror(self):
        if self.mirror is None or self._session is None:
            return
        image = self.mirror.capture(self._zoom)
        if image is None:
            self.viewport_view.set_message(self.mirror.status)
            return
        warning = self.mirror.status
        if not warning and self._covering_viewport():
            warning = u"This window covers part of SFM's viewport; move it aside."
        caption = u"SFM viewport \u00b7 %.2g\u00d7" % self._zoom
        self.viewport_view.set_frame(image, caption, warning)

    def _on_zoom_requested(self, factor):
        zoom = 1.0 if factor <= 0 else self._zoom * factor
        self._zoom = viewport.clamp_zoom(zoom)
        if self._session is not None and not self._in_tick:
            self._update_mirror()

    def showEvent(self, event):
        super(PickerDialog, self).showEvent(event)
        if not self._placed:
            self._placed = True
            QtCore.QTimer.singleShot(0, self._move_off_viewport)

    def _move_off_viewport(self):
        """Keep SFM's real viewport visible (it is also the source of the mirror)."""
        if self.mirror is None or not self.settings.get("avoid_viewport") or self.preview_source() != u"scene":
            return
        try:
            avoid = self.mirror.viewport_rect()
            if avoid is None:
                return
            frame = self.frameGeometry()
            target = viewport.choose_position((frame.width(), frame.height()), avoid,
                                              available_geometry(self),
                                              (frame.x(), frame.y()))
            if target != (frame.x(), frame.y()):
                self.move(target[0], target[1])
        except Exception:
            self.log(traceback.format_exc())

    def _on_source_changed(self, _index):
        self._stop_preview()
        if self.preview_source() == u"native" and self.native_provider is not None and self._native() is None:
            # explicit choice: retry once, even after a crash guard tripped
            self._native(force=True)
        self._update_controls()
        self._show_idle_message()
        self.preview_status.setText(u"")
        self._schedule_preview(force=True)
        if self.preview_source() == u"scene":
            QtCore.QTimer.singleShot(0, self._move_off_viewport)

    def _restart_effect(self):
        if self._native_scene is not None:
            self._native_scene.restart()
            self._native_last = clock()
            return
        if self._session is not None and self._preview_name:
            try:
                self.bridge.restart_preview(self._session)
            except Exception as exc:
                self._set_preview_status(html_escape(to_text(str(exc))), error=True)
                return
            self._restart_loop()
        else:
            self._schedule_preview(force=True)

    def _stop_preview(self):
        self._frame_timer.stop()
        self._preview_timer.stop()
        self._close_native()
        session, self._session = self._session, None
        self._preview_name = None
        if session is not None:
            try:
                self.bridge.stop_preview(session)
            except Exception:
                self.log(traceback.format_exc())
            try:
                self.bridge.set_head_time(self._t0)
                self.bridge.refresh_viewport(restart_time=False,
                                             render=self.drive_render.isChecked())
            except Exception:
                self.log(traceback.format_exc())
        self._show_idle_message()

    # -- result ----------------------------------------------------------------

    def _on_accept(self):
        record = self._current_record()
        if record is None:
            self.status.setText(u"Select a particle system first.")
            return
        self.selected = record
        self.accept()

    def done(self, result):
        self._stop_preview()
        self._store_settings()
        super(PickerDialog, self).done(result)

    def _store_settings(self):
        s = self.settings
        s.set("last_query", to_text(self.search.text()))
        s.set("view", self._current_view())
        s.set("hide_children", self.hide_children.isChecked())
        s.set("group_by_file", self.group_by_file.isChecked())
        s.set("preview_source", self.preview_source())
        s.set("show_grid", self.grid.isChecked())
        s.set("preview_background", self.background.value())
        s.set("side_tab", self.tabs.currentIndex())
        s.set("loop_preview", self.loop.isChecked())
        s.set("loop_seconds", self.loop_seconds.value())
        s.set("preview_distance", self.distance.value())
        s.set("drive_render", self.drive_render.isChecked())
        s.set("mirror_zoom", self._zoom)
        self._save_quietly()

    def _save_quietly(self):
        try:
            self.settings.save()
        except (IOError, OSError):
            self.log(traceback.format_exc())
