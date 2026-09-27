# -*- coding: utf-8 -*-
"""Environment report that users can paste into a bug report."""
from __future__ import absolute_import

import os
import struct
import sys
import traceback

from . import __version__, gamefs, menu_hook, native, viewport
from .compat import to_text
from .qt import QT_API, QtCore, QtWidgets, as_widget, binding_version, cpp_address, shiboken_module
from .sfm_bridge import (PARTICLE_ELEMENT_TYPE, STALE_PREFIXES, SfmBridge, children_of,
                         element_name)

_APP_METHODS = ("GetMainWindow", "GetShotAtCurrentTime", "GetHeadTimeInSeconds",
                "SetHeadTimeInSeconds", "GetFramesPerSecond", "ProcessEvents",
                "GetScriptController", "Version")


def list_attributes(element, limit=200):
    names = []
    attribute = element.FirstAttribute()
    while attribute is not None and len(names) < limit:
        name = to_text(attribute.GetName())
        type_name = u""
        for getter in ("GetTypeString", "GetType"):
            method = getattr(attribute, getter, None)
            if method is not None:
                try:
                    type_name = to_text(str(method()))
                    break
                except Exception:
                    pass
        names.append(u"%s:%s" % (name, type_name) if type_name else name)
        attribute = attribute.NextAttribute()
    return names


def _safe(add, label, func):
    try:
        add(u"%s: %s" % (label, to_text(func())))
    except Exception as exc:
        add(u"%s: <error %s: %s>" % (label, type(exc).__name__, exc))


def collect(sfm_module=None, hook=None, settings=None):
    lines = []
    add = lines.append
    add(u"Particle Browser %s" % __version__)
    add(u"Python %s, Qt %s, %s %s" % (sys.version.split()[0], QtCore.qVersion(), QT_API, binding_version()))
    module = shiboken_module()
    add(u"shiboken: %s" % (getattr(module, "__name__", None) or u"not available"))
    add(u"cwd: %s" % to_text(os.getcwd()))
    add(u"pointer size: %d bits" % (struct.calcsize("P") * 8))
    path = native.library_path()
    add(u"built-in renderer: %s (%s)" % (to_text(path), u"present" if os.path.isfile(path) else u"MISSING"))
    if os.path.isfile(path):
        def probe_native():
            library = native.Library(path)
            library.self_check()
            return u"loaded, ABI %d, self-test passed" % library.abi_version()
        _safe(add, u"  renderer check", probe_native)
    _section(add, u"game files", lambda: _game_files(add, settings))
    bridge = SfmBridge(sfm=sfm_module)
    _section(add, u"SFM API", lambda: _sfm_api(add, bridge))
    _section(add, u"main window", lambda: _main_window(add, bridge, settings))
    add(u"menu hook installed: %s (event filter: %s)" % (
        hook is not None, hook.filter_installed if hook is not None else False))
    return lines


def _section(add, label, func):
    # one failing part must not hide the rest of the report
    try:
        func()
    except Exception:
        add(u"%s: <error>\n%s" % (label, to_text(traceback.format_exc())))


def _game_files(add, settings):
    game = gamefs.find_game_dir()
    add(u"game folder: %s" % to_text(game))
    if game:
        mod = to_text(settings.get("game_mod")) if settings else gamefs.DEFAULT_MOD
        try:
            for key, value in gamefs.read_search_paths(game, mod):
                add(u"  search path %s -> %s" % (key, value))
        except Exception as exc:
            add(u"  search paths: <error %s>" % exc)
        mounts, warning = gamefs.discover_mounts(game, mod)
        if warning:
            add(u"  " + warning)
        sources, errors = gamefs.enumerate_sources(mounts)
        add(u"mounts: %d, pcf files: %d" % (len(mounts), len(sources)))
        for mount in mounts:
            count = sum(1 for s in sources if s.mount is mount)
            add(u"  %s %s (%d pcf)" % (mount.kind, mount.label, count))
        for path, error in errors:
            add(u"  error %s: %s" % (path, error))


def _sfm_api(add, bridge):
    add(u"modules: vs=%s sfm=%s sfmApp=%s sfmUtils=%s" % (
        bridge.vs is not None, bridge.sfm is not None, bridge.app is not None,
        bridge.utils is not None))
    _safe(add, u"sfm.GetCurrentShot()", lambda: element_name(bridge.context_shot()) or u"None")
    add(u"script context (direct creation possible): %s" % bridge.has_script_context())
    if bridge.app is not None:
        add(u"sfmApp methods: " + u", ".join(
            u"%s=%s" % (m, hasattr(bridge.app, m)) for m in _APP_METHODS))
        _safe(add, u"SFM version", lambda: bridge.app.Version())
        _safe(add, u"script controller members", lambda: u", ".join(
            m for m in dir(bridge.app.GetScriptController()) if not m.startswith(u"_")))
    if bridge.available:
        shot = bridge.current_shot()
        add(u"current shot: %s" % (element_name(shot) or u"None"))
        if shot is not None:
            _safe(add, u"shot range (s)", lambda: bridge.shot_range(shot))
            _safe(add, u"head time (s)", bridge.head_time)
            _safe(add, u"camera pose", lambda: bridge.camera_pose(shot))
            _safe(add, u"stale preview elements in scene", lambda: len(
                [c for c in children_of(bridge._scene(shot))
                 if element_name(c).startswith(STALE_PREFIXES)]))

            def probe():
                with bridge.undo_suspended(True):
                    element = bridge.vs.CreateElement(PARTICLE_ELEMENT_TYPE,
                                                      "__particle_browser_probe",
                                                      shot.GetFileId())
                    return u", ".join(list_attributes(element))
            _safe(add, u"%s attributes" % PARTICLE_ELEMENT_TYPE, probe)


def _main_window(add, bridge, settings):
    window = None
    if bridge.app is not None:
        try:
            window = bridge.app.GetMainWindow()
        except Exception:
            window = None
    if window is not None:
        address = cpp_address(window)
        add(u"main window: python type %s, C++ class %s, address %s, usable as widget: %s" % (
            type(window).__name__, to_text(window.metaObject().className()),
            u"%#x" % address if address else u"?", as_widget(window) is not None))
        editors = [w for w in window.findChildren(QtWidgets.QWidget)
                   if w.objectName() == "QAnimationSetEditor"]
        add(u"Animation Set Editor widgets: %d" % len(editors))
        for menu in menu_hook.find_add_menus(window):
            add(u"  menu with particle action: " + u" | ".join(
                to_text(a.text()) or u"---" for a in menu.actions()))
        hint = to_text(settings.get("viewport_hint")) if settings else u""
        ranked = viewport.rank_candidates(window, hint=hint)
        add(u"viewport candidates: %d" % len(ranked))
        for score, depth, _area, widget in ranked[:8]:
            add(u"  score %d depth %d %s %dx%d paintOnScreen=%s native=%s" % (
                score, depth, viewport.describe(widget), widget.width(), widget.height(),
                widget.testAttribute(QtCore.Qt.WA_PaintOnScreen),
                widget.testAttribute(QtCore.Qt.WA_NativeWindow)))
        if ranked:
            mirror = viewport.ViewportMirror(window, hint=hint)
            for strategy in (viewport.STRATEGY_WINDOW, viewport.STRATEGY_SCREEN):
                def probe(strategy=strategy):
                    mirror.strategy = strategy
                    image = mirror.capture(1.0)
                    if image is None:
                        return mirror.status
                    return u"%dx%d, %s" % (image.width(), image.height(),
                                           u"blank" if viewport.is_blank(image) else u"has content")
                _safe(add, u"  capture (%s)" % strategy, probe)


def make_report_dialog(text, parent=None):
    dialog = QtWidgets.QDialog(as_widget(parent))
    dialog.setWindowTitle(u"Particle Browser diagnostics")
    dialog.resize(760, 520)
    view = QtWidgets.QPlainTextEdit()
    view.setReadOnly(True)
    view.setPlainText(text)
    copy = QtWidgets.QPushButton(u"Copy to clipboard")
    copy.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(text))
    close = QtWidgets.QPushButton(u"Close")
    close.clicked.connect(dialog.accept)
    buttons = QtWidgets.QHBoxLayout()
    buttons.addStretch(1)
    buttons.addWidget(copy)
    buttons.addWidget(close)
    layout = QtWidgets.QVBoxLayout(dialog)
    layout.addWidget(view)
    layout.addLayout(buttons)
    return dialog


def show_report(text, parent=None):
    dialog = make_report_dialog(text, parent)
    run = getattr(dialog, "exec_", None) or getattr(dialog, "exec")
    run()
