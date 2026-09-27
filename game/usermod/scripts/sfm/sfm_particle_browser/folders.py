# -*- coding: utf-8 -*-
"""Scan folders behind the browser's burger menu.

A folder is the top-level game folder a record came from ("tf" for both tf/ and
tf/tf2_misc_dir.vpk, shown as "tf/particles") or a folder the user picked, keyed by its full
path (see ``gamefs.custom_label``). Unticked folders are hidden from the results.
"""
from __future__ import absolute_import

import os
from collections import OrderedDict

from . import gamefs
from .compat import to_text


def custom_folders(settings):
    """User-picked folders from the settings, in the order they were added."""
    result = []
    for path in settings.get("scan_custom") or ():
        path = to_text(path).strip()
        if path and path not in result:
            result.append(path)
    return result


def disabled_folders(settings):
    return set(to_text(key) for key in settings.get("scan_disabled") or ())


def folder_key(origin, custom=()):
    origin = to_text(origin or u"")
    if origin in custom:
        return origin
    return origin.split(u"/", 1)[0]


def folder_title(key, custom=()):
    return key if key in custom else u"%s/particles" % key


def files_text(count):
    return u"1 file" if count == 1 else u"%d files" % count


def folder_counts(index, custom=()):
    """``OrderedDict`` key -> ``[files, systems]``: game folders in mount order, then custom ones."""
    customs = set(custom)
    counts = OrderedDict()
    by_origin = getattr(index, "origin_files", None) or {}
    for origin, files in by_origin.items():
        counts.setdefault(folder_key(origin, customs), [0, 0])[0] += files
    seen = set()
    for record in index.records:
        key = folder_key(record.origin, customs)
        entry = counts.setdefault(key, [0, 0])
        entry[1] += 1
        if not by_origin and (key, record.pcf) not in seen:
            seen.add((key, record.pcf))
            entry[0] += 1
    for path in custom:
        counts.setdefault(path, [0, 0])
    return counts


def only_enabled(records, disabled, custom=()):
    if not disabled:
        return records
    customs = set(custom)
    return [r for r in records if folder_key(r.origin, customs) not in disabled]


def totals(index, counts, disabled):
    """``(systems, files, enabled, total)`` for the status line.

    With every folder ticked the numbers are the whole index, exactly as before folders existed.
    """
    enabled = [key for key in counts if key not in disabled]
    if len(enabled) == len(counts):
        return len(index.records), index.file_count, len(enabled), len(counts)
    return (sum(counts[key][1] for key in enabled), sum(counts[key][0] for key in enabled),
            len(enabled), len(counts))


def known_folder(path, counts, custom=(), game_dir=None):
    """The listed folder ``path`` points at (a custom folder or a game folder), else None."""
    label = gamefs.custom_label(path)
    for key in custom:
        if os.path.normcase(key) == os.path.normcase(label):
            return key
    if not game_dir:
        return None
    base = os.path.normpath(os.path.abspath(to_text(path)))
    if os.path.basename(base).lower() == "particles":
        base = os.path.dirname(base)
    try:
        rel = os.path.relpath(base, game_dir).replace("\\", "/")
    except ValueError:  # another drive on Windows
        return None
    if rel in (".", "..") or rel.startswith("../"):
        return None
    tag = rel.split("/", 1)[0].lower()
    customs = set(custom)
    for key in counts:
        if key not in customs and key.lower() == tag:
            return key
    return None


def menu_text(text):
    """Menu labels treat "&" as a mnemonic marker."""
    return to_text(text).replace(u"&", u"&&")
