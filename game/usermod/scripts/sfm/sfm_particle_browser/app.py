# -*- coding: utf-8 -*-
"""Glue between the entry scripts, the picker dialog, the menu hook and SFM."""
from __future__ import absolute_import

import os
import struct
import sys
import traceback

from . import __version__, gamefs, menu_hook, native, viewport
from .compat import to_text, write_bytes_atomic
from .index import ParticleIndex, ScanCancelled
from .qt import (DIALOG_ACCEPTED, QT_API, QtCore, QtWidgets, as_widget, binding_version, cpp_address, exec_dialog,
                 modality_for, shiboken_module)
from .settings import Settings
from .sfm_bridge import PARTICLE_ELEMENT_TYPE, STALE_PREFIXES, SfmBridge, children_of, element_name
from .ui import PickerDialog

_state = {"settings": None, "index": None, "hook": None, "filler": None, "retries": 0, "native": None}
MAX_HOOK_RETRIES = 30


def log(message):
    print(u"[particle browser] %s" % to_text(message))


def config_dir():
    game = gamefs.find_game_dir()
    if game:
        return os.path.join(game, gamefs.DEFAULT_MOD, "cfg", "sfm_particle_browser")
    return os.path.join(os.path.expanduser("~"), ".sfm_particle_browser")


LEGACY_CONFIG_NAME = "sfm_particle_picker"


def _migrate_legacy_settings(folder):
    """Carry favorites and options over from the plugin's old name, once."""
    target = os.path.join(folder, "settings.json")
    source = os.path.join(os.path.dirname(folder), LEGACY_CONFIG_NAME, "settings.json")
    if os.path.exists(target) or not os.path.isfile(source):
        return
    try:
        with open(source, "rb") as handle:
            write_bytes_atomic(target, handle.read())
        log(u"Settings copied from %s." % to_text(source))
    except (IOError, OSError) as exc:
        log(u"Could not copy old settings: %s" % exc)


def get_settings():
    if _state["settings"] is None:
        folder = config_dir()
        _migrate_legacy_settings(folder)
        _state["settings"] = Settings(os.path.join(folder, "settings.json"))
    return _state["settings"]


def main_object():
    """SFM's main window as SFM hands it out (typed as a plain QObject in SFM).

    Good for QObject calls such as findChildren(); use main_window() as a dialog parent.
    """
    try:
        import sfmApp
        window = sfmApp.GetMainWindow()
        if window is not None:
            return window
    except Exception:
        pass
    try:
        return QtWidgets.QApplication.activeWindow()
    except Exception:
        return None


def main_window():
    """SFM's main window as a QWidget, or None when it cannot be used as one."""
    return as_widget(main_object())


def game_mounts(settings=None):
    settings = settings or get_settings()
    game = gamefs.find_game_dir()
    if game is None:
        raise RuntimeError(u"SFM's game folder was not found (looked for usermod/gameinfo.txt"
                           u" relative to %s)." % to_text(os.getcwd()))
    mounts, warning = gamefs.discover_mounts(
        game, to_text(settings.get("game_mod")) or gamefs.DEFAULT_MOD,
        settings.get("include_unmounted"))
    # folders added in the browser's folder menu, after the game's own search paths
    custom = [to_text(path) for path in settings.get("scan_custom") or () if to_text(path).strip()]
    if custom:
        mounts = gamefs.with_custom_mounts(mounts, custom)
    return mounts, warning


def native_provider():
    """Session-wide provider for the built-in renderer (loaded on first use)."""
    if _state["native"] is None:
        try:
            threads = int(get_settings().get("render_threads") or 0)
        except (TypeError, ValueError):
            threads = 0
        _state["native"] = native.NativeProvider(lambda: gamefs.FileSystem(game_mounts()[0]),
                                                 config_dir(), log=log, threads=threads)
    return _state["native"]


def _message(kind, text):
    """Show a message box; never raises (errors end up in SFM's console)."""
    box = getattr(QtWidgets.QMessageBox, kind)
    try:
        parent = main_window()
    except Exception:
        parent = None
    for owner in ((parent, None) if parent is not None else (None,)):
        try:
            box(owner, u"Particle Browser", text)
            return
        except Exception:
            log(traceback.format_exc())
    log(text)


def build_index(parent=None, force=False):
    """Return the (cached) particle index, scanning files with a progress dialog."""
    index = _state["index"]
    if index is not None and index.built and not force:
        return index
    parent = as_widget(parent)
    settings = get_settings()
    mounts, warning = game_mounts(settings)
    if warning:
        log(warning)
    sources, errors = gamefs.enumerate_sources(mounts)
    for path, error in errors:
        log(u"%s: %s" % (path, error))

    progress = QtWidgets.QProgressDialog(u"Indexing particle files\u2026", u"Cancel", 0,
                                         max(1, len(sources)), parent)
    progress.setWindowTitle(u"Particle Browser")
    progress.setMinimumDuration(500)
    progress.setWindowModality(modality_for(parent))

    def report(done, total, label):
        progress.setMaximum(max(1, total))
        progress.setValue(done)
        progress.setLabelText(u"Indexing particle files\u2026\n%s" % label)
        QtWidgets.QApplication.processEvents()
        return not progress.wasCanceled()

    new_index = ParticleIndex(os.path.join(config_dir(), "index_cache.json"))
    try:
        parsed = new_index.build(sources, progress=report)
    except ScanCancelled:
        return index
    finally:
        progress.close()
    log(u"Indexed %d particle systems in %d files (%d parsed, the rest from cache)."
        % (len(new_index.records), new_index.file_count, parsed))
    _state["index"] = new_index
    if force and _state["native"] is not None:
        _state["native"].reset()
    return new_index


PCF_CACHE = "pcf_cache"


def stock_file_candidates(record):
    """``(texts, browse_file)`` for SFM's dialog.

    ``texts``: ways to type ``record``'s .pcf into the file field, most likely first (the
    format SFM expects there is unknown). ``browse_file``: an absolute path on disk for the
    Browse button's file dialog; a .pcf inside a VPK is copied out to the picker's cache.
    """
    relative = to_text(record.pcf).replace(u"\\", u"/")
    candidates = [relative, relative.replace(u"/", u"\\")]
    browse_file = None
    try:
        provider = _state["native"]
        fs = provider.renderer.fs if provider is not None and provider.renderer is not None else None
        if fs is None:
            fs = gamefs.FileSystem(game_mounts()[0])
        found = fs.locate(relative)
        if found is not None and found[0].kind == "dir":
            browse_file = os.path.normpath(to_text(found[1]))
            candidates.append(browse_file)
        elif found is not None:
            browse_file = _cache_pcf(relative, fs.read(relative))
    except Exception:
        log(traceback.format_exc())
    return candidates, browse_file


def _cache_pcf(relative, data):
    if data is None:
        return None
    target = os.path.normpath(os.path.join(config_dir(), PCF_CACHE, *relative.split(u"/")))
    if not (os.path.isfile(target) and os.path.getsize(target) == len(data)):
        write_bytes_atomic(target, data)
    return target


def open_picker(entry="mainmenu", sfm_module=None):
    try:
        return _open_picker(entry, sfm_module)
    except Exception as exc:
        log(traceback.format_exc())
        _message("critical", u"Particle Browser failed:\n%s" % to_text(str(exc)))
        return None


def _open_picker(entry, sfm_module):
    root = main_object()
    parent = as_widget(root)
    if root is not None and parent is None:
        log(u"SFM's main window cannot be used as a widget (%s); windows open unparented."
            % type(root).__name__)
    bridge = SfmBridge(sfm=sfm_module, log=log)
    settings = get_settings()
    index = build_index(parent)
    if index is None:
        return None
    log(u"Opening browser from %s." % entry)
    mirror = None
    if bridge.available and root is not None and settings.get("mirror_viewport"):
        mirror = viewport.ViewportMirror(root, hint=settings.get("viewport_hint"))
    dialog = PickerDialog(index, settings, bridge=bridge if bridge.available else None,
                          parent=parent,
                          rescan=lambda owner: build_index(owner, force=True), log=log,
                          mirror=mirror, native=native_provider())
    accepted = int(exec_dialog(dialog)) == DIALOG_ACCEPTED
    if mirror is not None:
        log(u"Viewport mirror used %s (strategy %s)." % (
            viewport.describe(mirror.widget) if mirror.widget is not None else u"no widget",
            mirror.strategy))
    record = dialog.selected
    dialog.deleteLater()
    if not accepted or record is None:
        return None
    settings.add_recent(record.name)
    settings.save()

    texts, browse_file = stock_file_candidates(record)
    filler, problem = menu_hook.handoff_to_stock(record.name, hook=_state["hook"],
                                                 main_window=root, log=log,
                                                 files=texts, browse_file=browse_file)
    _state["filler"] = filler  # keep the QObject alive until SFM's dialog is handled
    if problem:
        _message("information", problem)
    elif filler is not None and filler.result is not None and not filler.result[0]:
        _message("information", u"SFM's dialog could not be filled automatically (%s).\n"
                                u"The name \u201c%s\u201d is in the clipboard."
                                % (filler.result[1], record.name))
    return None


def install_menu_hook(deferred=False, notify=False):
    try:
        window = main_object()  # the hook only needs QObject calls
        if window is None:
            if deferred:
                _retry_install()
            if notify:
                _message("warning", u"SFM's main window was not found.")
            return False
        hook = _state["hook"]
        if hook is None:
            hook = menu_hook.MenuHook(window, on_trigger=lambda: open_picker(entry="plus"),
                                      settings=get_settings(), log=log)
            _state["hook"] = hook
        patched = hook.install()
        if patched:
            text = u"Patched %d Animation Set Editor menu(s)." % patched
        elif hook.filter_installed:
            text = u"The Animation Set Editor menu will be patched the next time it opens."
        else:
            text = u"The Animation Set Editor '+' menu was not found; open the editor and retry."
            if deferred:
                _retry_install()
        log(text)
        if notify:
            _message("information", text)
        return bool(patched) or hook.filter_installed
    except Exception:
        log(traceback.format_exc())
        if notify:
            _message("critical", traceback.format_exc())
        return False


def _retry_install():
    if _state["retries"] >= MAX_HOOK_RETRIES:
        log(u"Giving up on the menu hook; run Scripts > ParticleBrowser > Install Plus Menu Hook.")
        return
    _state["retries"] += 1
    QtCore.QTimer.singleShot(2000, lambda: install_menu_hook(deferred=True))


def uninstall_menu_hook():
    hook, _state["hook"] = _state["hook"], None
    if hook is not None:
        hook.uninstall()
    return hook is not None


def show_diagnostics(sfm_module=None):
    try:
        text = u"\n".join(collect_diagnostics(sfm_module=sfm_module, hook=_state["hook"],
                                              settings=get_settings()))
    except Exception:
        text = traceback.format_exc()
    log(text)
    try:
        show_report(text, main_window())
    except Exception:
        log(traceback.format_exc())
    return text


def _package_parent():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def toggle_startup():
    """Scripts > ParticleBrowser > Toggle At Startup: add the startup block, or remove it if present."""
    game = gamefs.find_game_dir()
    if game is not None and gamefs.startup_enabled(game):
        return disable_startup()
    return enable_startup()


def enable_startup():
    game = gamefs.find_game_dir()
    if game is None:
        _message("warning", u"SFM's game folder was not found.")
        return None
    path = gamefs.enable_startup(game, _package_parent())
    install_menu_hook()
    _message("information", u"The '+' menu hook will be installed every time SFM starts.\n"
                            u"Added a block to:\n%s" % to_text(path))
    return path


def disable_startup():
    game = gamefs.find_game_dir()
    if game is None:
        return None
    path, removed = gamefs.disable_startup(game)
    uninstall_menu_hook()
    _message("information", (u"Removed the startup block from:\n%s" if removed
                             else u"No startup block found in:\n%s") % to_text(path))
    return path


# -- diagnostics (an environment report users can paste into a bug report) ---


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


def collect_diagnostics(sfm_module=None, hook=None, settings=None):
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
            library.set_threads(int((settings.get("render_threads") if settings is not None else 0) or 0))
            library.self_check()
            return u"loaded, ABI %d, self-test passed, %d render thread(s)" % (
                library.abi_version(), library.thread_count())
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
    add(u"script context: %s" % bridge.has_script_context())
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
