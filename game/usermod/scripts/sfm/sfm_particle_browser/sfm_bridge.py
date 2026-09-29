# -*- coding: utf-8 -*-
"""Every call into SFM's Python API goes through this module.

SFM has three layers:

* ``vs`` - SWIG wrappers of the datamodel; usable at any time.
* ``sfmApp`` - the application object; usable at any time.
* ``sfm`` - script commands; they only work while SFM is executing a script
  from a context menu.  Outside of that ``sfm.GetCurrentShot()`` returns ``None``.

The bindings are undocumented and differ between builds, hence the fallbacks.
"""
from __future__ import absolute_import, division

import contextlib
import math
import random

from .compat import to_native, to_text

PREVIEW_PREFIX = u"__particle_browser_preview"
# previews left in saved sessions by the plugin under its old name are cleaned up too
STALE_PREFIXES = (PREVIEW_PREFIX, u"__particle_picker_preview")
PARTICLE_ELEMENT_TYPE = "DmeGameParticleSystem"
TICKS_PER_SECOND = 10000.0  # DmeTime_t resolution


class BridgeError(Exception):
    pass


def _import(name):
    try:
        return __import__(name)
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Element helpers
# ---------------------------------------------------------------------------

def has_attr(element, name):
    try:
        return bool(element.HasAttribute(name))
    except Exception:
        return False


def get_attr(element, name, default=None):
    if element is None:
        return default
    try:
        if element.HasAttribute(name):
            return element.GetValue(name)
        return default
    except Exception:
        pass
    try:
        return getattr(element, name)
    except Exception:
        return default


def _assign(element, name, value):
    setattr(element, name, value)


def _set_value(element, name, value):
    element.SetValue(name, value)


def set_attr(element, name, value):
    # Plain assignment is what Valve's own scripts use for existing attributes;
    # SetValue is the fallback and also creates missing attributes.
    order = (_assign, _set_value) if has_attr(element, name) else (_set_value, _assign)
    for setter in order:
        try:
            setter(element, name, value)
            return True
        except Exception:
            continue
    return False


def element_name(element):
    if element is None:
        return u""
    getter = getattr(element, "GetName", None)
    if getter is not None:
        try:
            return to_text(getter())
        except Exception:
            pass
    return to_text(get_attr(element, "name", u"")) or u""


def iter_list(value):
    if value is None:
        return []
    try:
        return list(value)
    except Exception:
        pass
    try:
        return [value[i] for i in range(len(value))]
    except Exception:
        return []


def children_of(dag):
    return iter_list(get_attr(dag, "children"))


def _count_named(dag, name):
    return sum(1 for child in children_of(dag) if element_name(child) == name)


def same_element(a, b):
    """Compare SWIG proxies by the wrapped pointer; ``None`` when undecidable."""
    try:
        return a.this == b.this
    except Exception:
        pass
    try:
        return a.GetHandle() == b.GetHandle()
    except Exception:
        return None


def remove_child(dag, child):
    """Unlink ``child`` from ``dag``; verified because overloads vary between builds."""
    name = element_name(child)
    before = _count_named(dag, name)
    if before == 0:
        return False
    index = None
    for i, candidate in enumerate(children_of(dag)):
        same = same_element(candidate, child)
        # With duplicate names and no identity check we cannot tell which one to drop.
        if same or (same is None and before == 1 and element_name(candidate) == name):
            index = i
            break
    children = get_attr(dag, "children")
    attempts = [lambda: dag.RemoveChild(child)]
    if index is not None:
        attempts += [lambda: dag.RemoveChild(index),
                     lambda: children.Remove(index),
                     lambda: children.RemoveMultiple(index, 1)]
    for attempt in attempts:
        try:
            attempt()
        except Exception:
            continue
        if _count_named(dag, name) < before:
            return True
    return False


def remove_children_named(dag, predicate):
    removed = 0
    for child in children_of(dag):
        if predicate(element_name(child)) and remove_child(dag, child):
            removed += 1
    return removed


def time_to_seconds(value):
    if value is None:
        return 0.0
    getter = getattr(value, "GetSeconds", None)
    if getter is not None:
        try:
            return float(getter())
        except Exception:
            pass
    getter = getattr(value, "GetValue", None)
    if getter is not None:
        try:
            return getter() / TICKS_PER_SECOND
        except Exception:
            pass
    try:
        return float(value)
    except Exception:
        return 0.0


def vec3(value):
    if value is None:
        return None
    try:
        return (float(value.x), float(value.y), float(value.z))
    except Exception:
        pass
    try:
        return (float(value[0]), float(value[1]), float(value[2]))
    except Exception:
        return None


def quat4(value):
    if value is None:
        return None
    try:
        return (float(value.x), float(value.y), float(value.z), float(value.w))
    except Exception:
        pass
    try:
        return tuple(float(value[i]) for i in range(4))
    except Exception:
        return None


def forward_from_quaternion(q):
    """Rotate +X (Source's forward axis) by quaternion ``(x, y, z, w)``."""
    x, y, z, w = q
    return (1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y + w * z), 2.0 * (x * z - w * y))


def _default_log(message):
    print(u"[particle browser] %s" % to_text(message))


class PreviewSession(object):
    def __init__(self, shot, suspend_undo):
        self.shot = shot
        self.suspend_undo = suspend_undo
        self.element_name = None
        self.system_name = None
        self.position = (0.0, 0.0, 0.0)


class SfmBridge(object):
    def __init__(self, vs=None, sfm=None, sfmApp=None, sfmUtils=None, log=None):
        self.vs = vs if vs is not None else _import("vs")
        self.sfm = sfm if sfm is not None else _import("sfm")
        self.app = sfmApp if sfmApp is not None else _import("sfmApp")
        self.utils = sfmUtils if sfmUtils is not None else _import("sfmUtils")
        self.log = log or _default_log

    @property
    def available(self):
        return self.vs is not None and self.app is not None

    # -- context ---------------------------------------------------------

    def context_shot(self):
        if self.sfm is None:
            return None
        try:
            return self.sfm.GetCurrentShot()
        except Exception:
            return None

    def has_script_context(self):
        return self.context_shot() is not None and hasattr(self.sfm, "CreateAnimationSet")

    def current_shot(self):
        shot = self.context_shot()
        if shot is not None or self.app is None:
            return shot
        getter = getattr(self.app, "GetShotAtCurrentTime", None)
        if getter is None:
            return None
        for args in ((), (self.head_time_frames(),)):
            try:
                shot = getter(*args)
            except Exception:
                continue
            if shot is not None:
                return shot
        return None

    # -- time and rendering ---------------------------------------------

    def head_time(self):
        try:
            return float(self.app.GetHeadTimeInSeconds())
        except Exception:
            return 0.0

    def head_time_frames(self):
        try:
            return int(self.app.GetHeadTimeInFrames())
        except Exception:
            return 0

    def set_head_time(self, seconds):
        try:
            self.app.SetHeadTimeInSeconds(float(seconds))
            return True
        except Exception as exc:
            self.log(u"SetHeadTimeInSeconds failed: %s" % exc)
            return False

    def fps(self):
        try:
            return float(self.app.GetFramesPerSecond()) or 24.0
        except Exception:
            return 24.0

    def render(self):
        """Process Qt events and render one engine frame (sfmApp.ProcessEvents)."""
        process = getattr(self.app, "ProcessEvents", None)
        if process is None:
            return False
        try:
            process()
            return True
        except Exception:
            return False

    def refresh_viewport(self, restart_time=True, render=True):
        # Stepping back re-simulates particles from their start, like scrubbing.
        now = self.head_time()
        if restart_time:
            self.set_head_time(now - 1.0 / self.fps())
        self.set_head_time(now)
        if render:
            self.render()

    def shot_range(self, shot):
        """Return the shot's ``(start, end)`` in movie seconds, or ``None``."""
        frame = get_attr(shot, "timeFrame")
        if frame is None:
            return None
        start = time_to_seconds(get_attr(frame, "start"))
        duration = time_to_seconds(get_attr(frame, "duration"))
        if duration <= 0:
            return None
        return start, start + duration

    # -- geometry --------------------------------------------------------

    def camera_pose(self, shot):
        camera = get_attr(shot, "camera")
        if camera is None:
            return None
        position = orientation = None
        getter = getattr(camera, "GetAbsPosition", None)
        if getter is not None:
            try:
                position = vec3(getter())
            except Exception:
                position = None
        getter = getattr(camera, "GetAbsOrientation", None)
        if getter is not None:
            try:
                orientation = quat4(getter())
            except Exception:
                orientation = None
        if position is None or orientation is None:
            transform = get_attr(camera, "transform")
            if position is None:
                position = vec3(get_attr(transform, "position"))
            if orientation is None:
                orientation = quat4(get_attr(transform, "orientation"))
        if position is None:
            return None
        forward = forward_from_quaternion(orientation) if orientation else (1.0, 0.0, 0.0)
        return position, forward

    def point_in_front_of_camera(self, shot, distance):
        pose = self.camera_pose(shot)
        if pose is None:
            return (0.0, 0.0, 0.0)
        (px, py, pz), (fx, fy, fz) = pose
        length = math.sqrt(fx * fx + fy * fy + fz * fz) or 1.0
        scale = float(distance) / length
        return (px + fx * scale, py + fy * scale, pz + fz * scale)

    # -- undo --------------------------------------------------------------

    @contextlib.contextmanager
    def undo_suspended(self, suspend=True):
        datamodel = getattr(self.vs, "g_pDataModel", None) if suspend else None
        disabled = False
        previous = True
        if datamodel is not None:
            try:
                previous = bool(datamodel.IsUndoEnabled())
            except Exception:
                previous = True
            try:
                datamodel.SetUndoEnabled(False)
                disabled = True
            except Exception:
                disabled = False
        try:
            yield
        finally:
            if disabled:
                try:
                    datamodel.SetUndoEnabled(previous)
                except Exception:
                    pass

    # -- particle elements ------------------------------------------------

    def _new_particle_element(self, shot, name, system_name, position):
        if self.vs is None:
            raise BridgeError("SFM's 'vs' module is not available.")
        element = self.vs.CreateElement(PARTICLE_ELEMENT_TYPE, to_native(name), shot.GetFileId())
        if element is None:
            raise BridgeError("vs.CreateElement(%s) returned None." % PARTICLE_ELEMENT_TYPE)
        set_attr(element, "particleSystemType", to_native(system_name))
        for attribute in ("visible", "simulating", "emitting"):
            set_attr(element, attribute, True)
        transform = get_attr(element, "transform")
        if transform is not None and position is not None:
            set_attr(transform, "position", self.vs.Vector(*[float(c) for c in position]))
        return element

    def _scene(self, shot):
        scene = get_attr(shot, "scene")
        if scene is None:
            raise BridgeError("The current shot has no scene.")
        return scene

    # -- preview -----------------------------------------------------------

    def start_preview(self, shot=None):
        shot = shot if shot is not None else self.current_shot()
        if shot is None:
            raise BridgeError("No shot under the playhead; open a session first.")
        # Outside a script there is no undo scope, so keep preview edits out of undo.
        session = PreviewSession(shot, suspend_undo=not self.has_script_context())
        self.remove_stale_previews(shot, session.suspend_undo)
        return session

    def show_preview(self, session, system_name, position=None):
        """(Re)create the preview element so the effect restarts from scratch."""
        if position is not None:
            session.position = position
        with self.undo_suspended(session.suspend_undo):
            self._drop_preview_element(session)
            name = u"%s_%06d" % (PREVIEW_PREFIX, random.randint(0, 999999))
            element = self._new_particle_element(session.shot, name, system_name, session.position)
            self._scene(session.shot).AddChild(element)
            session.element_name = name
            session.system_name = system_name

    def restart_preview(self, session):
        if session.system_name:
            self.show_preview(session, session.system_name)

    def stop_preview(self, session):
        with self.undo_suspended(session.suspend_undo):
            self._drop_preview_element(session)
        session.system_name = None

    def _drop_preview_element(self, session):
        if session.element_name is None:
            return
        name = session.element_name
        session.element_name = None
        scene = get_attr(session.shot, "scene")
        if scene is not None and not remove_children_named(scene, lambda n: n == name):
            self.log(u"Could not remove preview element %s from the scene." % name)

    def remove_stale_previews(self, shot, suspend_undo=True):
        """Remove previews left behind by a crash or an interrupted session."""
        scene = get_attr(shot, "scene")
        if scene is None:
            return 0
        stale = [c for c in children_of(scene) if element_name(c).startswith(STALE_PREFIXES)]
        if not stale:
            return 0
        with self.undo_suspended(suspend_undo):
            return remove_children_named(scene, lambda n: n.startswith(STALE_PREFIXES))
