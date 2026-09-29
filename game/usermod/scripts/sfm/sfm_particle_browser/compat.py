# -*- coding: utf-8 -*-
"""Helpers that keep the package working on SFM's Python 2.7 and on Python 3."""
from __future__ import absolute_import

import io
import json
import os
import sys
import time

PY2 = sys.version_info[0] == 2

# A clock for measuring short intervals (frame times). time.time() can advance in 15.6 ms
# steps on Windows, which is coarser than a whole preview frame; time.clock() on Python 2
# for Windows reads QueryPerformanceCounter, and Python 3 has perf_counter everywhere.
if PY2 and sys.platform.startswith("win"):  # pragma: no cover - SFM only
    clock = time.clock
else:
    clock = getattr(time, "perf_counter", time.time)

if PY2:  # pragma: no cover - exercised under the 2.7 test run
    text_type = unicode  # noqa: F821
    string_types = (str, unicode)  # noqa: F821
    xrange = xrange  # noqa: F821
else:
    text_type = str
    string_types = (str,)
    xrange = range


def to_text(value, encoding="utf-8"):
    """Decode bytes (py2 ``str``) to text; non-UTF-8 falls back to latin-1."""
    if value is None:
        return None
    if isinstance(value, text_type):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            return value.decode("latin-1")
    return text_type(value)


def to_native(value):
    """Return the native ``str`` type that SFM's SWIG bindings expect."""
    if value is None:
        return None
    if PY2:
        if isinstance(value, unicode):  # noqa: F821
            return value.encode("utf-8")
        return str(value)
    return to_text(value)


def makedirs(path):
    try:
        os.makedirs(path)
    except OSError:
        if not os.path.isdir(path):
            raise


def read_text(path):
    with io.open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def write_bytes_atomic(path, data):
    """Write via a temp file so a crash never leaves a truncated file behind."""
    folder = os.path.dirname(path)
    if folder:
        makedirs(folder)
    tmp = path + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(data)
    if os.path.exists(path):
        # os.rename cannot overwrite on Windows under Python 2.
        os.remove(path)
    os.rename(tmp, path)


def load_json(path, default=None):
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except (IOError, OSError):
        return default
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return default


def save_json(path, obj):
    data = json.dumps(obj, ensure_ascii=True, indent=1, sort_keys=True)
    if not isinstance(data, bytes):
        data = data.encode("ascii")
    write_bytes_atomic(path, data)
