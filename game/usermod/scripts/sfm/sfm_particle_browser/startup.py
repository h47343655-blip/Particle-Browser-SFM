# -*- coding: utf-8 -*-
"""Add/remove the menu-hook block in ``<mod>/scripts/sfm/sfm_init.py``.

Works on bytes so that the user's existing file is preserved exactly.
"""
from __future__ import absolute_import

import os
import shutil

from .compat import PY2, write_bytes_atomic
from .gamefs import DEFAULT_MOD

BEGIN = b"# >>> sfm_particle_browser startup >>>"
END = b"# <<< sfm_particle_browser startup <<<"
BACKUP_SUFFIX = ".particle_browser.bak"
# The plugin was called Particle Picker before; its block is removed as well.
LEGACY_MARKERS = ((b"# >>> sfm_particle_picker startup >>>", b"# <<< sfm_particle_picker startup <<<"),)
LEGACY_BACKUP_SUFFIXES = (".particle_picker.bak",)


def init_script_path(game_dir, mod=DEFAULT_MOD):
    return os.path.join(game_dir, mod, "scripts", "sfm", "sfm_init.py")


def _literal(path):
    # sfm_init.py has no coding declaration, so the literal must stay ASCII.
    text = repr(path) if PY2 else ascii(path)  # noqa: F821 - py3 builtin
    return text.encode("ascii")


def snippet(package_parent):
    lines = [
        BEGIN,
        b"try:",
        b"    import sys as _pp_sys",
        b"    _pp_path = " + _literal(package_parent),
        b"    if _pp_path not in _pp_sys.path:",
        b"        _pp_sys.path.append(_pp_path)",
        b"    import sfm_particle_browser as _pp",
        b"    _pp.install_menu_hook(deferred=True)",
        b"except Exception as _pp_error:",
        b"    print('[particle browser] startup hook failed: %r' % (_pp_error,))",
        END,
    ]
    return b"\n".join(lines) + b"\n"


def _strip_one(data, begin, end_marker):
    start = data.find(begin)
    if start < 0:
        return data, False
    end = data.find(end_marker, start)
    if end < 0:
        return data, False  # damaged block: leave the file alone
    end += len(end_marker)
    if data[end:end + 2] == b"\r\n":
        end += 2
    elif data[end:end + 1] == b"\n":
        end += 1
    return data[:start] + data[end:], True


def strip_block(data):
    """Remove this plugin's startup block (and the old Particle Picker one)."""
    removed = False
    for begin, end_marker in ((BEGIN, END),) + LEGACY_MARKERS:
        while True:
            data, found = _strip_one(data, begin, end_marker)
            if not found:
                break
            removed = True
    return data, removed


def _read(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as handle:
        return handle.read()


def is_enabled(game_dir, mod=DEFAULT_MOD):
    data = _read(init_script_path(game_dir, mod))
    return data is not None and BEGIN in data


def enable(game_dir, package_parent, mod=DEFAULT_MOD):
    path = init_script_path(game_dir, mod)
    original = _read(path)
    data, _ = strip_block(original or b"")
    newline = b"\r\n" if b"\r\n" in data else b"\n"
    if data and not data.endswith(b"\n"):
        data += newline
    block = snippet(package_parent).replace(b"\n", newline)
    had_backup = any(os.path.exists(path + suffix) for suffix in (BACKUP_SUFFIX,) + LEGACY_BACKUP_SUFFIXES)
    if original is not None and not had_backup:
        shutil.copyfile(path, path + BACKUP_SUFFIX)
    write_bytes_atomic(path, data + block)
    return path


def disable(game_dir, mod=DEFAULT_MOD):
    path = init_script_path(game_dir, mod)
    original = _read(path)
    if original is None:
        return path, False
    data, removed = strip_block(original)
    if removed:
        write_bytes_atomic(path, data)
    return path, removed
