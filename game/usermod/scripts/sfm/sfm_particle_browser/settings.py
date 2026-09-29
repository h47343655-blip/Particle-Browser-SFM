# -*- coding: utf-8 -*-
"""Persistent user settings (favorites, recents, preview and creation options)."""
from __future__ import absolute_import

import copy

from .compat import load_json, save_json, to_text

MAX_RECENT = 25

DEFAULTS = {
    "favorites": [],
    "recent": [],
    "hide_children": True,
    "view": u"all",
    "group_by_file": False,
    "last_query": u"",
    "live_preview": True,
    "preview_source": u"native",
    "show_grid": True,
    "preview_background": u"",  # "" = default gradient, otherwise "#rrggbb"
    "side_tab": 0,  # 0 Info, 1 Render
    "loop_preview": True,
    "loop_seconds": 3.0,
    "preview_distance": 128.0,
    "drive_render": True,
    "mirror_viewport": True,
    "mirror_zoom": 1.0,
    "avoid_viewport": True,
    "viewport_hint": u"",
    "replace_stock_menu": True,
    "patch_all_menus": True,
    "include_unmounted": False,
    "game_mod": u"usermod",
    "scan_custom": [],  # folders added from the search box's folder menu (gamefs.custom_label)
    "scan_disabled": [],  # folders unticked in that menu (gamefs.folder_key)
    "render_threads": 0,  # built-in renderer threads: 0 = automatic (cores - 1, at most 8), 1 = off
}


class Settings(object):
    def __init__(self, path=None):
        self.path = path
        self.data = copy.deepcopy(DEFAULTS)
        if path:
            stored = load_json(path, {})
            if isinstance(stored, dict):
                for key, value in stored.items():
                    if key in DEFAULTS and _same_kind(DEFAULTS[key], value):
                        self.data[key] = value

    def get(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def set(self, key, value):
        self.data[key] = value

    def save(self):
        if self.path:
            save_json(self.path, self.data)

    def favorites(self):
        return set(to_text(n) for n in self.data["favorites"])

    def is_favorite(self, name):
        return to_text(name) in self.favorites()

    def toggle_favorite(self, name):
        name = to_text(name)
        favorites = [to_text(n) for n in self.data["favorites"]]
        if name in favorites:
            favorites.remove(name)
            state = False
        else:
            favorites.append(name)
            state = True
        self.data["favorites"] = sorted(favorites, key=lambda n: n.lower())
        return state

    def add_recent(self, name):
        name = to_text(name)
        recent = [to_text(n) for n in self.data["recent"] if to_text(n) != name]
        self.data["recent"] = ([name] + recent)[:MAX_RECENT]


def _same_kind(default, value):
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, list):
        return isinstance(value, list)
    if isinstance(default, type(u"")):
        return isinstance(value, (type(u""), str))
    return True
