# -*- coding: utf-8 -*-
"""Find SFM's game folder, its mounted search paths and the .pcf files in them."""
from __future__ import absolute_import

import glob
import os

from .compat import read_text, to_text
from .vpk import VpkArchive

DEFAULT_MOD = "usermod"
_NON_CONTENT_DIRS = {"bin", "sdktools", "platform"}


# ---------------------------------------------------------------------------
# KeyValues (gameinfo.txt)
# ---------------------------------------------------------------------------

class _Word(type(u"")):
    __slots__ = ()


def _kv_tokens(text):
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end + 1
        elif ch in "{}":
            yield ch
            i += 1
        elif ch == "[":  # platform conditional such as [$WIN32]
            end = text.find("]", i)
            i = n if end < 0 else end + 1
        elif ch == "\"":
            i += 1
            out = []
            while i < n and text[i] != "\"":
                if text[i] == "\\" and i + 1 < n and text[i + 1] in "\"\\":
                    i += 1
                out.append(text[i])
                i += 1
            i += 1
            yield _Word(u"".join(out))
        else:
            start = i
            while i < n and not text[i].isspace() and text[i] not in "{}\"[":
                i += 1
            yield _Word(text[start:i])


def parse_keyvalues(text):
    """Parse KeyValues text into nested ``[(key, value_or_list), ...]``."""
    tokens = list(_kv_tokens(to_text(text)))
    state = [0]

    def is_symbol(token, symbol):
        return token == symbol and not isinstance(token, _Word)

    def block():
        items = []
        while state[0] < len(tokens):
            token = tokens[state[0]]
            state[0] += 1
            if is_symbol(token, "}"):
                return items
            if state[0] >= len(tokens):
                break
            value = tokens[state[0]]
            state[0] += 1
            if is_symbol(value, "{"):
                items.append((token, block()))
            else:
                items.append((token, value))
        return items

    return block()


def find_block(items, *names):
    current = items
    for name in names:
        for key, value in current:
            if isinstance(value, list) and key.lower() == name.lower():
                current = value
                break
        else:
            return None
    return current


# ---------------------------------------------------------------------------
# Mounts
# ---------------------------------------------------------------------------

class Mount(object):
    def __init__(self, kind, path, label, mounted=True, scan_root=None):
        self.kind = kind  # "dir" or "vpk"
        self.path = path
        self.label = label
        self.mounted = mounted
        self.scan_root = scan_root  # dir mounts: folder searched for .pcf files (default <path>/particles)
        self._archive = None

    def archive(self):
        if self._archive is None:
            self._archive = VpkArchive(self.path)
        return self._archive

    def __repr__(self):
        return "<Mount %s %s>" % (self.kind, self.path)


def find_game_dir(extra_candidates=()):
    """Return SFM's ``game`` folder (SFM runs scripts with it as the cwd)."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = list(extra_candidates) + [
        os.getcwd(),
        # game/<mod>/scripts/sfm/sfm_particle_browser -> game
        os.path.normpath(os.path.join(here, os.pardir, os.pardir, os.pardir, os.pardir)),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(os.path.join(candidate, DEFAULT_MOD, "gameinfo.txt")):
            return os.path.abspath(candidate)
    return None


def _label(game_dir, path):
    rel = os.path.relpath(path, game_dir).replace("\\", "/")
    if rel.lower().endswith("_dir.vpk"):
        rel = rel[:-len("_dir.vpk")]
    return to_text(rel)


def _vpk_dir_file(path):
    if path.lower().endswith("_dir.vpk"):
        return path if os.path.isfile(path) else None
    candidate = path[:-4] + "_dir.vpk"
    if os.path.isfile(candidate):
        return candidate
    return path if os.path.isfile(path) else None


def _auto_vpks(folder):
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [os.path.join(folder, n) for n in names if n.lower().endswith("_dir.vpk")]


def read_search_paths(game_dir, mod=DEFAULT_MOD):
    """Return raw ``(key, value)`` pairs of ``gameinfo.txt``'s SearchPaths block."""
    gameinfo = os.path.join(game_dir, mod, "gameinfo.txt")
    block = find_block(parse_keyvalues(read_text(gameinfo)),
                       "GameInfo", "FileSystem", "SearchPaths")
    if block is None:
        raise ValueError("%s has no GameInfo/FileSystem/SearchPaths block" % gameinfo)
    return [(k, v) for k, v in block if not isinstance(v, list)]


def _resolve(game_dir, mod_dir, value):
    value = value.replace("\\", "/")
    lower = value.lower()
    for token, base in (("|gameinfo_path|", mod_dir), ("|all_source_engine_paths|", game_dir)):
        if lower.startswith(token):
            return os.path.normpath(os.path.join(base, value[len(token):] or "."))
    return os.path.normpath(os.path.join(game_dir, value))


def mounts_from_search_paths(game_dir, entries, mod=DEFAULT_MOD):
    mod_dir = os.path.join(game_dir, mod)
    mounts = []
    for key, value in entries:
        if "game" not in [k.strip().lower() for k in key.split("+")]:
            continue
        path = _resolve(game_dir, mod_dir, value)
        if path.lower().endswith(".vpk"):
            matches = sorted(glob.glob(path)) if "*" in path else [path]
            for match in matches:
                dir_file = _vpk_dir_file(match)
                if dir_file:
                    mounts.append(Mount("vpk", dir_file, _label(game_dir, dir_file)))
        elif os.path.isdir(path):
            mounts.append(Mount("dir", path, _label(game_dir, path)))
            for vpk_path in _auto_vpks(path):
                mounts.append(Mount("vpk", vpk_path, _label(game_dir, vpk_path)))
    return _dedupe(mounts)


def _dedupe(mounts):
    seen = set()
    result = []
    for mount in mounts:
        key = os.path.normcase(os.path.abspath(mount.path))
        if key not in seen:
            seen.add(key)
            result.append(mount)
    return result


def _all_mod_dirs(game_dir, mod):
    try:
        names = sorted(os.listdir(game_dir))
    except OSError:
        return []
    names = [n for n in names if os.path.isdir(os.path.join(game_dir, n))
             and n.lower() not in _NON_CONTENT_DIRS]
    names.sort(key=lambda n: (n.lower() != mod.lower(), n.lower()))
    return [os.path.join(game_dir, n) for n in names]


def discover_mounts(game_dir, mod=DEFAULT_MOD, include_unmounted=False):
    """Return ``(mounts, warning)``; falls back to scanning every mod folder."""
    warning = None
    try:
        mounts = mounts_from_search_paths(game_dir, read_search_paths(game_dir, mod), mod)
    except (IOError, OSError, ValueError) as exc:
        warning = "Could not read search paths (%s); scanning all mod folders." % exc
        mounts = []
        for folder in _all_mod_dirs(game_dir, mod):
            mounts.append(Mount("dir", folder, _label(game_dir, folder), mounted=None))
            for vpk_path in _auto_vpks(folder):
                mounts.append(Mount("vpk", vpk_path, _label(game_dir, vpk_path), mounted=None))
        return _dedupe(mounts), warning
    if include_unmounted:
        for folder in _all_mod_dirs(game_dir, mod):
            mounts.append(Mount("dir", folder, _label(game_dir, folder), mounted=False))
            for vpk_path in _auto_vpks(folder):
                mounts.append(Mount("vpk", vpk_path, _label(game_dir, vpk_path), mounted=False))
    return _dedupe(mounts), warning


# ---------------------------------------------------------------------------
# User-picked scan folders
# ---------------------------------------------------------------------------

def custom_label(path):
    """Stable text for a user-picked folder; also the label (origin) of its mount."""
    return to_text(os.path.normpath(os.path.abspath(to_text(path)))).replace(u"\\", u"/")


def custom_mount(path):
    """Mount for a user-picked folder, or None when it is not a folder.

    ``.../particles`` and folders holding a ``particles`` folder mount like a mod folder, so the
    files keep their ``particles/...`` names; any other folder is searched for .pcf files as is.
    """
    path = os.path.normpath(os.path.abspath(to_text(path)))
    if not os.path.isdir(path):
        return None
    label = custom_label(path)
    if os.path.basename(path).lower() == "particles":
        return Mount("dir", os.path.dirname(path), label)
    if os.path.isdir(os.path.join(path, "particles")):
        return Mount("dir", path, label)
    return Mount("dir", path, label, scan_root=path)


def _scan_key(mount):
    root = getattr(mount, "scan_root", None)
    return (os.path.normcase(os.path.abspath(mount.path)),
            os.path.normcase(os.path.abspath(root)) if root else None)


def with_custom_mounts(mounts, paths):
    """``mounts`` plus mounts for the user-picked ``paths``.

    They come last, so a file that also exists in a game folder keeps the game's copy, the way
    the engine would resolve it. Folders already scanned as game folders are skipped.
    """
    result = list(mounts)
    seen = set(_scan_key(m) for m in result if m.kind == "dir")
    for path in paths:
        mount = custom_mount(path)
        if mount is not None and _scan_key(mount) not in seen:
            seen.add(_scan_key(mount))
            result.append(mount)
    return result


# ---------------------------------------------------------------------------
# PCF sources
# ---------------------------------------------------------------------------

class PcfSource(object):
    __slots__ = ("relpath", "mount", "entry", "abspath")

    def __init__(self, relpath, mount, entry=None, abspath=None):
        self.relpath = relpath
        self.mount = mount
        self.entry = entry
        self.abspath = abspath

    @property
    def origin(self):
        return self.mount.label

    def key(self):
        if self.entry is not None:
            return u"vpk|%s|%s" % (to_text(self.mount.path), self.relpath)
        return u"dir|%s" % to_text(self.abspath)

    def signature(self):
        if self.entry is not None:
            return [self.entry.crc, self.entry.length]
        stat = os.stat(self.abspath)
        return [int(stat.st_size), int(stat.st_mtime)]

    def read(self):
        if self.entry is not None:
            return self.mount.archive().read(self.entry)
        with open(self.abspath, "rb") as handle:
            return handle.read()


def enumerate_sources(mounts):
    """Return ``(sources, errors)``; earlier mounts shadow later ones like the engine does."""
    seen = set()
    sources = []
    errors = []
    for mount in mounts:
        found = []
        if mount.kind == "dir":
            root = getattr(mount, "scan_root", None) or os.path.join(mount.path, "particles")
            for folder, dirs, files in os.walk(root):
                dirs.sort()
                for name in sorted(files):
                    if name.lower().endswith(".pcf"):
                        abspath = os.path.join(folder, name)
                        rel = os.path.relpath(abspath, mount.path).replace("\\", "/")
                        found.append(PcfSource(to_text(rel), mount, abspath=abspath))
        else:
            try:
                archive = mount.archive()
            except (IOError, OSError, ValueError) as exc:
                errors.append((to_text(mount.path), u"%s" % exc))
                continue
            for entry in archive.iter_files(u"particles/", u".pcf"):
                found.append(PcfSource(entry.path, mount, entry=entry))
        for source in found:
            key = source.relpath.lower()
            if key not in seen:
                seen.add(key)
                sources.append(source)
    return sources, errors


# ---------------------------------------------------------------------------
# File access across mounts (loose files and VPKs)
# ---------------------------------------------------------------------------

def normalize_relpath(path):
    path = to_text(path).replace(u"\\", u"/").strip()
    while path.startswith(u"./"):
        path = path[2:]
    return path.lstrip(u"/")


class FileSystem(object):
    """Reads game files the way the engine resolves them: first mount wins."""

    def __init__(self, mounts):
        self.mounts = list(mounts)
        self._vpk_entries = {}

    def _entries(self, mount):
        key = mount.path
        entries = self._vpk_entries.get(key)
        if entries is None:
            entries = {}
            try:
                for entry in mount.archive().entries:
                    entries.setdefault(entry.path.lower(), entry)
            except (IOError, OSError, ValueError):
                pass
            self._vpk_entries[key] = entries
        return entries

    def locate(self, relpath):
        """Return ``(mount, entry_or_abspath)`` or ``None``."""
        rel = normalize_relpath(relpath)
        if not rel:
            return None
        lower = rel.lower()
        for mount in self.mounts:
            if mount.kind == "dir":
                for candidate in (rel, lower):
                    path = os.path.join(mount.path, *candidate.split(u"/"))
                    if os.path.isfile(path):
                        return mount, path
            else:
                entry = self._entries(mount).get(lower)
                if entry is not None:
                    return mount, entry
        return None

    def read(self, relpath):
        found = self.locate(relpath)
        if found is None:
            return None
        mount, target = found
        try:
            if mount.kind == "dir":
                with open(target, "rb") as handle:
                    return handle.read()
            return mount.archive().read(target)
        except (IOError, OSError, ValueError):
            return None
