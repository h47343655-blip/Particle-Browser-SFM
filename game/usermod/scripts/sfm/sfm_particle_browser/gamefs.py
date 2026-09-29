# -*- coding: utf-8 -*-
"""Find SFM's game folder, its mounted search paths and the .pcf files in them."""
from __future__ import absolute_import

import glob
import os
import shutil
import struct
from collections import OrderedDict

from .compat import PY2, read_text, to_text, write_bytes_atomic

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


# -- VPK archives ------------------------------------------------------------


# Read-only access to Valve VPK archives (directory format versions 1 and 2).
SIGNATURE = 0x55AA1234
EMBEDDED_ARCHIVE = 0x7FFF
_ENTRY = struct.Struct("<IHHIIH")  # crc, preload size, archive, offset, length, 0xffff


class VpkError(Exception):
    pass


class VpkEntry(object):
    __slots__ = ("path", "crc", "preload", "archive_index", "offset", "length")

    def __init__(self, path, crc, preload, archive_index, offset, length):
        self.path = path
        self.crc = crc
        self.preload = preload
        self.archive_index = archive_index
        self.offset = offset
        self.length = length


def _join(folder, name, ext):
    path = name if folder == b" " else folder + b"/" + name
    if ext != b" ":
        path = path + b"." + ext
    return to_text(path).replace(u"\\", u"/")


class VpkArchive(object):
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as handle:
            header = handle.read(12)
            if len(header) < 12:
                raise VpkError("file too small to be a VPK")
            signature, version, tree_size = struct.unpack("<III", header)
            if signature != SIGNATURE:
                raise VpkError("not a VPK directory file")
            if version == 1:
                header_size = 12
            elif version == 2:
                handle.read(16)
                header_size = 28
            else:
                raise VpkError("unsupported VPK version %d" % version)
            tree = handle.read(tree_size)
        if len(tree) < tree_size:
            raise VpkError("truncated VPK directory tree")
        self.version = version
        self.data_offset = header_size + tree_size
        lower = path.lower()
        self._archive_base = path[:-len("_dir.vpk")] if lower.endswith("_dir.vpk") else None
        self.entries = self._parse_tree(tree)

    @staticmethod
    def _parse_tree(tree):
        entries = []
        find = tree.find
        pos = 0

        def cstr(p):
            end = find(b"\0", p)
            if end < 0:
                raise VpkError("corrupt VPK directory tree")
            return tree[p:end], end + 1

        try:
            while True:
                ext, pos = cstr(pos)
                if not ext:
                    break
                while True:
                    folder, pos = cstr(pos)
                    if not folder:
                        break
                    while True:
                        name, pos = cstr(pos)
                        if not name:
                            break
                        crc, preload_size, archive, offset, length, _ = \
                            _ENTRY.unpack_from(tree, pos)
                        pos += _ENTRY.size
                        preload = tree[pos:pos + preload_size]
                        pos += preload_size
                        entries.append(VpkEntry(_join(folder, name, ext), crc, preload,
                                                archive, offset, length))
        except struct.error:
            raise VpkError("corrupt VPK directory tree")
        return entries

    def iter_files(self, prefix=u"", suffix=u""):
        prefix = prefix.lower()
        suffix = suffix.lower()
        for entry in self.entries:
            lower = entry.path.lower()
            if lower.startswith(prefix) and lower.endswith(suffix):
                yield entry

    def read(self, entry):
        if entry.length == 0:
            return entry.preload
        if entry.archive_index == EMBEDDED_ARCHIVE:
            path = self.path
            offset = self.data_offset + entry.offset
        else:
            if not self._archive_base:
                raise VpkError("%s references archive %d but is not a *_dir.vpk"
                               % (self.path, entry.archive_index))
            path = "%s_%03d.vpk" % (self._archive_base, entry.archive_index)
            offset = entry.offset
        with open(path, "rb") as handle:
            handle.seek(offset)
            data = handle.read(entry.length)
        if len(data) != entry.length:
            raise VpkError("truncated data for %s" % entry.path)
        return entry.preload + data


# -- scan folders ------------------------------------------------------------


# The browser's burger menu. A folder is the top-level game folder a record came from ("tf" for
# both tf/ and tf/tf2_misc_dir.vpk, shown as "tf/particles") or a folder the user picked, keyed by
# its full path (see ``custom_label``). Unticked folders are hidden from the results.
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
    label = custom_label(path)
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


# -- startup block -----------------------------------------------------------


# Add/remove the menu-hook block in ``<mod>/scripts/sfm/sfm_init.py``; works on bytes so the
# user's existing file is preserved exactly.
STARTUP_BEGIN = b"# >>> sfm_particle_browser startup >>>"
STARTUP_END = b"# <<< sfm_particle_browser startup <<<"
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


def startup_snippet(package_parent):
    lines = [
        STARTUP_BEGIN,
        b"try:",
        b"    import sys as _pp_sys",
        b"    _pp_path = " + _literal(package_parent),
        b"    if _pp_path not in _pp_sys.path:",
        b"        _pp_sys.path.append(_pp_path)",
        b"    import sfm_particle_browser as _pp",
        b"    _pp.install_menu_hook(deferred=True)",
        b"except Exception as _pp_error:",
        b"    print('[particle browser] startup hook failed: %r' % (_pp_error,))",
        STARTUP_END,
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


def strip_startup_block(data):
    """Remove this plugin's startup block (and the old Particle Picker one)."""
    removed = False
    for begin, end_marker in ((STARTUP_BEGIN, STARTUP_END),) + LEGACY_MARKERS:
        while True:
            data, found = _strip_one(data, begin, end_marker)
            if not found:
                break
            removed = True
    return data, removed


def _read_bytes(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as handle:
        return handle.read()


def startup_enabled(game_dir, mod=DEFAULT_MOD):
    data = _read_bytes(init_script_path(game_dir, mod))
    return data is not None and STARTUP_BEGIN in data


def enable_startup(game_dir, package_parent, mod=DEFAULT_MOD):
    path = init_script_path(game_dir, mod)
    original = _read_bytes(path)
    data, _ = strip_startup_block(original or b"")
    newline = b"\r\n" if b"\r\n" in data else b"\n"
    if data and not data.endswith(b"\n"):
        data += newline
    block = startup_snippet(package_parent).replace(b"\n", newline)
    had_backup = any(os.path.exists(path + suffix) for suffix in (BACKUP_SUFFIX,) + LEGACY_BACKUP_SUFFIXES)
    if original is not None and not had_backup:
        shutil.copyfile(path, path + BACKUP_SUFFIX)
    write_bytes_atomic(path, data + block)
    return path


def disable_startup(game_dir, mod=DEFAULT_MOD):
    path = init_script_path(game_dir, mod)
    original = _read_bytes(path)
    if original is None:
        return path, False
    data, removed = strip_startup_block(original)
    if removed:
        write_bytes_atomic(path, data)
    return path, removed
