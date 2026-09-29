# -*- coding: utf-8 -*-
"""Built-in particle preview renderer: glue for the pp_render DLL.

The DLL simulates and software-renders particle systems on its own, so the
picker can show an isolated preview with its own camera.  It never touches
SFM's engine or memory; everything it needs (definitions, VTF textures) is
read from the game files here and handed over as plain bytes.
"""
from __future__ import absolute_import, division

import ctypes
import math
import os
import struct
import sys
import time
from collections import OrderedDict

from . import dmx, gamefs
from .compat import load_json, save_json, to_text
from .dmx import DEFINITION_TYPE

ABI_VERSION = 1
MAT_ADDITIVE = 1
MAT_NO_BLEND_FRAMES = 2
MAT_INVISIBLE = 4
OPT_GRID = 1
OPT_AXES = 2
OPT_EDITOR_GRID = 4  # the Particle Editor's grid; an older DLL ignores the bit

CATEGORIES = (("renderers", 0), ("operators", 1), ("initializers", 2), ("emitters", 3),
              ("forces", 4), ("constraints", 5))
AT_INT, AT_FLOAT, AT_BOOL, AT_STRING, AT_COLOR, AT_VEC2, AT_VEC3, AT_VEC4 = range(1, 9)
try:
    _INTEGER_TYPES = (int, long)  # Python 2
except NameError:
    _INTEGER_TYPES = (int,)
MAX_DEFS = 4096
# SFM is a 32-bit process: keep decoded textures and parsed .pcf files few
MAX_MATERIALS = 24
MAX_DOCS = 2
GUARD_FILE = "native_guard.json"
ENV_LIBRARY = "PP_RENDER_LIBRARY"


class NativeError(Exception):
    pass


def _cbytes(text):
    """Bytes for a ``c_char_p`` argument on both Python 2 and 3."""
    return to_text(text or u"").encode("utf-8")


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

def library_name(bits=None, platform=None):
    bits = bits or struct.calcsize("P") * 8
    platform = platform or sys.platform
    if platform.startswith("win"):
        return "pp_render_win32.dll" if bits == 32 else "pp_render_win64.dll"
    return "libpp_render.so"


def library_path():
    explicit = os.environ.get(ENV_LIBRARY)
    if explicit:
        return explicit
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", library_name())


_PROTOTYPES = (
    ("pp_abi_version", ctypes.c_int, ()),
    ("pp_last_error", ctypes.c_char_p, ()),
    ("pp_self_test", ctypes.c_int, ()),
    ("pp_material_create_vtf", ctypes.c_int, (ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_float)),
    ("pp_material_create_rgba", ctypes.c_int,
     (ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_float)),
    ("pp_material_destroy", None, (ctypes.c_int,)),
    ("pp_material_info", ctypes.c_int, (ctypes.c_int,) + (ctypes.POINTER(ctypes.c_int),) * 4),
    ("pp_material_live_count", ctypes.c_int, ()),
    ("pp_scene_create", ctypes.c_int, ()),
    ("pp_scene_destroy", None, (ctypes.c_int,)),
    ("pp_scene_load", ctypes.c_int, (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p)),
    ("pp_scene_material_count", ctypes.c_int, (ctypes.c_int,)),
    ("pp_scene_material_name", ctypes.c_char_p, (ctypes.c_int, ctypes.c_int)),
    ("pp_scene_bind_material", ctypes.c_int, (ctypes.c_int, ctypes.c_char_p, ctypes.c_int)),
    ("pp_scene_issue_count", ctypes.c_int, (ctypes.c_int,)),
    ("pp_scene_issue", ctypes.c_char_p, (ctypes.c_int, ctypes.c_int)),
    ("pp_scene_restart", None, (ctypes.c_int, ctypes.c_uint)),
    ("pp_scene_step", None, (ctypes.c_int, ctypes.c_float)),
    ("pp_scene_time", ctypes.c_float, (ctypes.c_int,)),
    ("pp_scene_particle_count", ctypes.c_int, (ctypes.c_int,)),
    ("pp_scene_system_count", ctypes.c_int, (ctypes.c_int,)),
    ("pp_scene_bounds", ctypes.c_int, (ctypes.c_int, ctypes.POINTER(ctypes.c_float))),
    ("pp_scene_set_camera", None, (ctypes.c_int,) + (ctypes.c_float,) * 7),
    ("pp_scene_set_options", None, (ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint)),
    ("pp_scene_set_control_point", None, (ctypes.c_int, ctypes.c_int) + (ctypes.c_float,) * 3),
    ("pp_scene_render", ctypes.c_int, (ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int)),
)

# Exports newer DLLs have; an older DLL without them still works (single-threaded).
_OPTIONAL_PROTOTYPES = (
    ("pp_set_render_threads", ctypes.c_int, (ctypes.c_int,)),
    ("pp_render_thread_count", ctypes.c_int, ()),
    ("pp_scene_particle_capacity", ctypes.c_int, (ctypes.c_int,)),
)


class Library(object):
    """The loaded DLL with typed prototypes."""

    def __init__(self, path=None):
        self.path = path or library_path()
        if not os.path.isfile(self.path):
            raise NativeError("renderer library not found: %s" % self.path)
        try:
            # unchanged type: ctypes picks LoadLibraryW for unicode and LoadLibraryA for bytes
            self.dll = ctypes.CDLL(self.path)
        except OSError as exc:
            raise NativeError("cannot load %s: %s" % (self.path, exc))
        for name, restype, argtypes in _PROTOTYPES:
            try:
                function = getattr(self.dll, name)
            except AttributeError:
                raise NativeError("%s does not export %s" % (self.path, name))
            function.restype = restype
            function.argtypes = list(argtypes)
            setattr(self, name[3:], function)
        for name, restype, argtypes in _OPTIONAL_PROTOTYPES:
            function = getattr(self.dll, name, None)
            if function is not None:
                function.restype = restype
                function.argtypes = list(argtypes)
            setattr(self, name[3:], function)
        version = self.abi_version()
        if version != ABI_VERSION:
            raise NativeError("renderer ABI %d, expected %d" % (version, ABI_VERSION))

    def error(self):
        return to_text(self.last_error() or b"")

    def set_threads(self, count):
        """Rendering threads (0 = automatic); returns the count in use, 1 for an older DLL."""
        if self.set_render_threads is None:
            return 1
        return self.set_render_threads(max(0, int(count or 0)))

    def thread_count(self):
        if self.render_thread_count is None:
            return 1
        return self.render_thread_count()

    def self_check(self):
        if not self.self_test():
            raise NativeError("renderer self-test failed: %s" % self.error())


# ---------------------------------------------------------------------------
# Definition blob
# ---------------------------------------------------------------------------

def _pack_str(text):
    raw = to_text(text or u"").encode("utf-8")[:4096]
    return struct.pack("<H", len(raw)) + raw


def _pack_attr(key, value):
    head = _pack_str(key)
    if isinstance(value, bool):
        return head + struct.pack("<BB", AT_BOOL, 1 if value else 0)
    if isinstance(value, dmx.DmxColor):
        if len(value) != 4:
            return None
        return head + struct.pack("<B4B", AT_COLOR, *[max(0, min(255, int(c))) for c in value])
    if isinstance(value, dmx.DmxVector):
        size = len(value)
        if size not in (2, 3, 4):
            return None
        kind = {2: AT_VEC2, 3: AT_VEC3, 4: AT_VEC4}[size]
        return head + struct.pack("<B%df" % size, kind, *[float(v) for v in value])
    if isinstance(value, float):
        return head + struct.pack("<Bf", AT_FLOAT, value if abs(value) < 3e38 else 0.0)
    if isinstance(value, _INTEGER_TYPES):
        return head + struct.pack("<Bi", AT_INT, max(-2 ** 31, min(2 ** 31 - 1, int(value))))
    if isinstance(value, type(u"")):
        return head + struct.pack("<B", AT_STRING) + _pack_str(value)
    return None


def _pack_attrs(attributes, skip=()):
    parts = []
    for key in sorted(attributes):
        if key in skip:
            continue
        packed = _pack_attr(key, attributes[key])
        if packed is not None:
            parts.append(packed)
    return struct.pack("<I", len(parts)) + b"".join(parts)


def reachable_definitions(root):
    """``root`` and every definition it spawns through ``children`` (each once)."""
    order, index, pending = [], {}, [root]
    while pending and len(order) < MAX_DEFS:
        definition = pending.pop(0)
        if id(definition) in index:
            continue
        index[id(definition)] = len(order)
        order.append(definition)
        for ref in definition.get("children") or ():
            target = ref.get("child") if ref is not None else None
            if target is not None and target.type == DEFINITION_TYPE and id(target) not in index:
                pending.append(target)
    return order, index


PATH_FUNCTIONS = ("position along path sequential", "position along path random",
                  "sequential position along path", "random position along path")
PATH_LENGTH = 256.0  # units between a path's start and end control points in the preview


def _attr_ci(element, key):
    value = element.get(key)
    if value is not None:
        return value
    key = key.lower()
    for name, value in element.attributes.items():
        if to_text(name).lower() == key:
            return value
    return None


def path_control_points(root):
    """``{cp: (x, y, z)}`` for control points that effects draw paths between.

    The preview has no scene to put control points in, so every one sits at the origin and a
    beam from control point 0 to 1 collapses into a dot. This spreads the end points of each
    path out along +X (further paths a little to the side) and leaves control point 0 alone.
    """
    order, _index = reachable_definitions(root)
    pairs = []
    for definition in order:
        for list_name, _category in CATEGORIES:
            for function in definition.get(list_name) or ():
                if function is None:
                    continue
                name = function.get("functionName")
                name = to_text(name if isinstance(name, (type(u""), str)) else function.name).lower()
                if name not in PATH_FUNCTIONS:
                    continue
                start, end = (_attr_ci(function, "start control point number"),
                              _attr_ci(function, "end control point number"))
                start = int(start) if isinstance(start, _INTEGER_TYPES + (float,)) else 0
                end = int(end) if isinstance(end, _INTEGER_TYPES + (float,)) else 0
                if start != end and 0 <= start < 16 and 0 <= end < 16 and (start, end) not in pairs:
                    pairs.append((start, end))
    positions = {0: (0.0, 0.0, 0.0)}
    for lane, (start, end) in enumerate(pairs):
        if start not in positions:
            positions[start] = (0.0, 64.0 * lane, 0.0)
        if end not in positions:
            sx, sy, sz = positions[start]
            positions[end] = (sx + PATH_LENGTH, sy, sz)
    positions.pop(0)
    return positions


def encode_definitions(root):
    """Serialize ``root`` (a DmxElement) and its children for ``pp_scene_load``."""
    order, index = reachable_definitions(root)
    lists = set(name for name, _ in CATEGORIES)
    out = [b"PPB1", struct.pack("<I", len(order))]
    for definition in order:
        out.append(_pack_str(definition.name))
        out.append(_pack_attrs(definition.attributes, skip=lists | set(["children"])))
        functions = []
        for list_name, category in CATEGORIES:
            for function in definition.get(list_name) or ():
                if function is not None:
                    functions.append((category, function))
        out.append(struct.pack("<I", len(functions)))
        for category, function in functions:
            name = function.get("functionName")
            if not isinstance(name, type(u"")):
                name = function.name
            out.append(struct.pack("<B", category) + _pack_str(name))
            out.append(_pack_attrs(function.attributes, skip=("functionName",)))
        children = []
        for ref in definition.get("children") or ():
            target = ref.get("child") if ref is not None else None
            if target is not None and id(target) in index:
                delay = ref.get("delay")
                children.append(struct.pack("<IfB", index[id(target)],
                                            float(delay) if isinstance(delay, float) else 0.0,
                                            1 if ref.get("end cap effect") else 0))
        out.append(struct.pack("<I", len(children)) + b"".join(children))
    return b"".join(out)


def find_definition(doc, name):
    name = to_text(name)
    for element in doc.elements:
        if element.type == DEFINITION_TYPE and element.name == name:
            return element
    return None


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------

def normalize_material(name):
    name = to_text(name or u"").replace(u"\\", u"/").strip().lower()
    if name.startswith(u"materials/"):
        name = name[len(u"materials/"):]
    if name.endswith(u".vmt"):
        name = name[:-4]
    return name


def parse_vmt(text):
    """Return ``(shader, {param: value})`` with lower-case parameter names."""
    items = gamefs.parse_keyvalues(text)
    for key, value in items:
        if isinstance(value, list):
            params = {}
            for pkey, pvalue in value:
                params[pkey.lower()] = pvalue
            return key.lower(), params
    return None, {}


def read_vmt(fs, name, depth=0):
    data = fs.read(u"materials/%s.vmt" % normalize_material(name))
    if data is None:
        return None, {}
    shader, params = parse_vmt(data.decode("utf-8", "replace"))
    if shader == u"patch" and depth < 4:
        include = params.get(u"include")
        base_shader, base = read_vmt(fs, include, depth + 1) if isinstance(include, type(u"")) else (None, {})
        merged = dict(base)
        for block in (u"insert", u"replace"):
            extra = params.get(block)
            if isinstance(extra, list):
                for pkey, pvalue in extra:
                    merged[pkey.lower()] = pvalue
        return base_shader, merged
    return shader, params


def _flag(params, key):
    value = params.get(key)
    return isinstance(value, type(u"")) and value.strip() not in (u"", u"0", u"0.0")


def material_settings(shader, params):
    """``(texture, flags, overbright)`` for a parsed VMT."""
    flags = 0
    if _flag(params, u"$additive"):
        flags |= MAT_ADDITIVE
    blend = params.get(u"$blendframes")
    if isinstance(blend, type(u"")) and blend.strip() == u"0":
        flags |= MAT_NO_BLEND_FRAMES
    try:
        overbright = float(params.get(u"$overbrightfactor", u"1"))
    except (TypeError, ValueError):
        overbright = 1.0
    texture = params.get(u"$basetexture")
    return (texture if isinstance(texture, type(u"")) else None), flags, overbright


def is_refraction(shader, params):
    """Heat haze and similar: the engine warps what is behind; there is no colour to draw."""
    if u"refract" in (shader or u""):
        return True
    return not params.get(u"$basetexture") and any(
        params.get(key) for key in (u"$normalmap", u"$bumpmap", u"$dudvmap", u"$refracttexture"))


class MaterialCache(object):
    """Material name -> DLL handle, least recently used first out."""

    def __init__(self, library, fs, log=None, limit=MAX_MATERIALS):
        self.library = library
        self.fs = fs
        self.log = log or (lambda message: None)
        self.limit = limit
        self._handles = OrderedDict()
        self.notes = {}
        self.hidden = set()  # materials drawn as nothing (refraction)

    def get(self, name):
        name = normalize_material(name)
        if name in self._handles:
            handle = self._handles.pop(name)
            self._handles[name] = handle
            return handle
        handle = self._load(name)
        self._handles[name] = handle
        return handle

    def _load(self, name):
        shader, params = read_vmt(self.fs, name)
        if shader is None:
            self.notes[name] = u"material not found"
            return 0
        texture, flags, overbright = material_settings(shader, params)
        if is_refraction(shader, params):
            self.hidden.add(name)
            # transparent too, so an older DLL without MAT_INVISIBLE also draws nothing
            return self.library.material_create_rgba(b"\x00" * 4, 1, 1, flags | MAT_INVISIBLE, 1.0)
        if not texture:
            white = b"\xff" * 4 * 16
            return self.library.material_create_rgba(white, 4, 4, flags, overbright)
        data = self.fs.read(u"materials/%s.vtf" % normalize_material(texture).rstrip(u"/"))
        if data is None and texture.lower().endswith(u".vtf"):
            data = self.fs.read(u"materials/%s" % normalize_material(texture))
        if data is None:
            self.notes[name] = u"texture %s not found" % texture
            return 0
        handle = self.library.material_create_vtf(data, len(data), flags, overbright)
        if not handle:
            self.notes[name] = self.library.error()
            self.log(u"Material %s: %s" % (name, self.library.error()))
        return handle

    def issues(self, names):
        """``[(status, text)]`` for the given materials, in order."""
        out = []
        for name in names:
            name = normalize_material(name)
            if name in self.hidden:
                out.append((u"not rendered", u"Refraction (%s)" % name))
            elif name in self.notes:
                out.append((u"missing", u"%s: %s" % (name, self.notes[name])))
        return out

    def trim(self, keep=()):
        keep = set(normalize_material(k) for k in keep)
        while len(self._handles) > self.limit:
            for name in self._handles:
                if name not in keep:
                    handle = self._handles.pop(name)
                    if handle:
                        self.library.material_destroy(handle)
                    break
            else:
                break

    def clear(self):
        for handle in self._handles.values():
            if handle:
                self.library.material_destroy(handle)
        self._handles.clear()
        self.notes.clear()  # files may have changed (rescan)
        self.hidden.clear()


# ---------------------------------------------------------------------------
# Crash guard
# ---------------------------------------------------------------------------

class CrashGuard(object):
    """Remembers what the renderer was doing so a crash disables it next time."""

    def __init__(self, folder):
        self.path = os.path.join(folder, GUARD_FILE) if folder else None
        self.armed = False

    def previous(self):
        if not self.path:
            return None
        state = load_json(self.path, None)
        return state if isinstance(state, dict) else None

    def arm(self, what):
        if not self.path:
            return
        try:
            save_json(self.path, {"doing": to_text(what), "time": int(time.time())})
            self.armed = True
        except (IOError, OSError):
            self.armed = False

    def disarm(self):
        if self.path and (self.armed or os.path.exists(self.path)):
            try:
                os.remove(self.path)
            except OSError:
                pass
        self.armed = False


# ---------------------------------------------------------------------------
# Scenes
# ---------------------------------------------------------------------------

class Issue(object):
    __slots__ = ("status", "category", "name")

    def __init__(self, status, category, name):
        self.status = status
        self.category = category
        self.name = name


class PreviewScene(object):
    def __init__(self, renderer, handle, name, materials):
        self.renderer = renderer
        self.library = renderer.library
        self.handle = handle
        self.name = name
        self.materials = materials
        self.issues = []
        self.material_issues = renderer.materials.issues(materials)
        for i in range(self.library.scene_issue_count(handle)):
            parts = to_text(self.library.scene_issue(handle, i)).split(u"|", 2)
            if len(parts) == 3:
                self.issues.append(Issue(*parts))
        self._buffer = None
        self._bounds = (ctypes.c_float * 6)()
        self.first_frame_done = False

    def issue_names(self, *statuses):
        return sorted(set(i.name for i in self.issues if i.status in statuses))

    def restart(self, seed=1):
        self.library.scene_restart(self.handle, seed)

    def step(self, seconds):
        self.library.scene_step(self.handle, float(seconds))

    @property
    def time(self):
        return self.library.scene_time(self.handle)

    def particle_count(self):
        return self.library.scene_particle_count(self.handle)

    def system_count(self):
        return self.library.scene_system_count(self.handle)

    def bounds(self):
        if not self.library.scene_bounds(self.handle, self._bounds):
            return None
        values = list(self._bounds)
        return tuple(values[:3]), tuple(values[3:])

    def set_camera(self, camera):
        tx, ty, tz = camera.target
        self.library.scene_set_camera(self.handle, camera.yaw, camera.pitch, camera.distance,
                                      tx, ty, tz, camera.fov)

    def set_options(self, grid=True, axes=True, top=0x2c2f36, bottom=0x121316, editor_grid=False):
        flags = (OPT_GRID if grid else 0) | (OPT_AXES if axes else 0) | (OPT_EDITOR_GRID if editor_grid else 0)
        self.library.scene_set_options(self.handle, flags, top, bottom)

    def capacity(self):
        """Particles the effect can hold at once, or None with an older DLL."""
        if self.library.scene_particle_capacity is None:
            return None
        return self.library.scene_particle_capacity(self.handle)

    def render(self, width, height):
        """Render to BGRA bytes; returns ``(data, thinned)``."""
        width, height = max(1, int(width)), max(1, int(height))
        size = width * height * 4
        if self._buffer is None or len(self._buffer) < size:
            self._buffer = ctypes.create_string_buffer(size)
        result = self.library.scene_render(self.handle, ctypes.addressof(self._buffer), width, height, width * 4)
        if not result:
            raise NativeError(self.library.error())
        if not self.first_frame_done:
            self.first_frame_done = True
            self.renderer.guard.disarm()
        return ctypes.string_at(self._buffer, size), result == 2

    def close(self):
        if self.handle:
            self.library.scene_destroy(self.handle)
            self.handle = 0


class NativeRenderer(object):
    """Session-wide renderer: DLL, game files, material and document caches."""

    def __init__(self, library, fs, guard=None, log=None):
        self.library = library
        self.fs = fs
        self.guard = guard or CrashGuard(None)
        self.log = log or (lambda message: None)
        self.materials = MaterialCache(library, fs, self.log)
        self._docs = OrderedDict()

    def document(self, relpath):
        key = to_text(relpath).lower()
        doc = self._docs.pop(key, None)
        if doc is None:
            data = self.fs.read(relpath)
            if data is None:
                raise NativeError(u"%s was not found in the game files" % relpath)
            doc = dmx.load(data)
        self._docs[key] = doc
        while len(self._docs) > MAX_DOCS:
            self._docs.popitem(last=False)
        return doc

    def scene_for(self, name, pcf_path):
        doc = self.document(pcf_path)
        root = find_definition(doc, name)
        if root is None:
            raise NativeError(u"%s is not defined in %s" % (name, pcf_path))
        blob = encode_definitions(root)
        self.guard.arm(u"%s (%s)" % (name, pcf_path))
        handle = self.library.scene_create()
        if not handle:
            raise NativeError(self.library.error())
        if not self.library.scene_load(handle, blob, len(blob), b""):
            message = self.library.error()
            self.library.scene_destroy(handle)
            self.guard.disarm()
            raise NativeError(message)
        control_points = path_control_points(root)
        for index, (x, y, z) in sorted(control_points.items()):
            self.library.scene_set_control_point(handle, index, x, y, z)
        if control_points:
            self.library.scene_restart(handle, 1)  # systems copy the control points when they start
        names = [to_text(self.library.scene_material_name(handle, i))
                 for i in range(self.library.scene_material_count(handle))]
        self.materials.trim(keep=names)
        for material in names:
            material_handle = self.materials.get(material)
            if material_handle:
                self.library.scene_bind_material(handle, _cbytes(material), material_handle)
        return PreviewScene(self, handle, name, names)

    def close(self):
        self.materials.clear()
        self._docs.clear()


def create(fs, config_folder=None, log=None, path=None, threads=0):
    """Return ``(renderer, None)`` or ``(None, reason)``; never raises.

    ``threads``: rendering threads, 0 = automatic. The self test checks the threaded path and
    the DLL drops to one thread by itself if it gives a different frame.
    """
    guard = CrashGuard(config_folder)
    previous = guard.previous()
    if previous is not None:
        return None, (u"The built-in renderer was turned off because SFM closed while it was busy "
                      u"with %s. Choose \u201cBuilt-in\u201d again to retry." % previous.get("doing", u"?"))
    guard.arm(u"loading the renderer")
    try:
        library = Library(path)
        library.set_threads(threads)
        library.self_check()
    except NativeError as exc:
        guard.disarm()
        return None, to_text(str(exc))
    except Exception as exc:  # e.g. access violation raised as WindowsError
        guard.disarm()
        return None, u"%s: %s" % (type(exc).__name__, to_text(str(exc)))
    guard.disarm()
    return NativeRenderer(library, fs, guard, log), None


class NativeProvider(object):
    """Loads the renderer on first use; ``failed`` and ``reason`` explain a refusal."""

    def __init__(self, fs_factory, config_folder=None, log=None, path=None, threads=0):
        self.fs_factory = fs_factory
        self.config_folder = config_folder
        self.log = log or (lambda message: None)
        self.path = path
        self.threads = threads
        self.renderer = None
        self.reason = None
        self.failed = False
        self._tried = False

    def get(self, force=False):
        if self.renderer is not None:
            return self.renderer
        if self._tried and not force:
            return None
        self._tried = True
        if force:
            CrashGuard(self.config_folder).disarm()
        try:
            fs = self.fs_factory()
        except Exception as exc:
            self.failed, self.reason = True, u"game files: %s" % to_text(str(exc))
            return None
        self.renderer, self.reason = create(fs, self.config_folder, self.log, self.path, self.threads)
        self.failed = self.renderer is None
        if self.failed:
            self.log(u"Built-in renderer unavailable: %s" % self.reason)
        return self.renderer

    def reset(self):
        """Forget the file system (after a rescan); keeps the loaded DLL."""
        if self.renderer is not None:
            try:
                self.renderer.fs = self.fs_factory()
                self.renderer.materials.fs = self.renderer.fs
                self.renderer.materials.clear()
                self.renderer._docs.clear()
            except Exception as exc:
                self.log(u"Renderer reset failed: %s" % exc)


# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------

class OrbitCamera(object):
    """Orbit camera around the effect; frames itself until the user takes over."""

    MIN_DISTANCE = 16.0
    MAX_DISTANCE = 20000.0

    def __init__(self, fov=50.0):
        self.fov = fov
        self.reset()

    def reset(self):
        self.yaw = 35.0
        self.pitch = 18.0
        self.distance = 160.0
        self.target = (0.0, 0.0, 0.0)
        self.user = False
        self._fit_radius = 0.0
        self._fit_center = None

    def orbit(self, dx, dy):
        self.yaw = (self.yaw - dx * 0.4) % 360.0
        # screen y grows downwards: dragging up (dy < 0) raises the camera over the effect
        self.pitch = max(-85.0, min(85.0, self.pitch - dy * 0.3))
        self.user = True

    def dolly(self, steps):
        self.distance = max(self.MIN_DISTANCE, min(self.MAX_DISTANCE, self.distance * (0.85 ** steps)))
        self.user = True

    def pan(self, dx, dy, view_height):
        scale = 2.0 * self.distance * math.tan(math.radians(self.fov) * 0.5) / max(1.0, float(view_height))
        yaw, pitch = math.radians(self.yaw), math.radians(self.pitch)
        right = (math.sin(yaw), -math.cos(yaw), 0.0)
        up = (-math.sin(pitch) * math.cos(yaw), -math.sin(pitch) * math.sin(yaw), math.cos(pitch))
        tx, ty, tz = self.target
        self.target = (tx - (right[0] * dx - up[0] * dy) * scale,
                       ty - (right[1] * dx - up[1] * dy) * scale,
                       tz - (right[2] * dx - up[2] * dy) * scale)
        self.user = True

    def fit(self, bounds, dt):
        """Ease towards framing the largest extent seen so far."""
        if self.user or bounds is None:
            return
        lo, hi = bounds
        center = tuple((a + b) * 0.5 for a, b in zip(lo, hi))
        radius = 0.5 * math.sqrt(sum((b - a) ** 2 for a, b in zip(lo, hi)))
        if not (radius < 1e6):
            return
        if radius > self._fit_radius:
            self._fit_radius = radius
            self._fit_center = center
        want = max(self.MIN_DISTANCE, min(self.MAX_DISTANCE, 1.15 * self._fit_radius /
                                          math.sin(math.radians(self.fov) * 0.5)))
        k = min(1.0, max(0.0, dt) * 4.0)
        self.distance += (want - self.distance) * k
        if self._fit_center is not None:
            self.target = tuple(t + (c - t) * k for t, c in zip(self.target, self._fit_center))
