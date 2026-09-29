# -*- coding: utf-8 -*-
"""Particle Browser for Source Filmmaker: searchable particle browser with live preview.

The scripts in ``scripts/sfm/{mainmenu,autoinit}`` are thin wrappers around
these functions.  Imports are lazy so that the pure-Python parts work without Qt.
"""
from __future__ import absolute_import

__version__ = "0.1.0"


def open_picker(entry="mainmenu", sfm_module=None):
    from . import app
    return app.open_picker(entry=entry, sfm_module=sfm_module)


def install_menu_hook(deferred=False, notify=False):
    from . import app
    return app.install_menu_hook(deferred=deferred, notify=notify)


def uninstall_menu_hook():
    from . import app
    return app.uninstall_menu_hook()


def show_diagnostics(sfm_module=None):
    from . import app
    return app.show_diagnostics(sfm_module=sfm_module)


def toggle_startup():
    from . import app
    return app.toggle_startup()
