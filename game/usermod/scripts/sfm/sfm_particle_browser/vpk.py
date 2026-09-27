# -*- coding: utf-8 -*-
"""Read-only access to Valve VPK archives (directory format versions 1 and 2)."""
from __future__ import absolute_import

import struct

from .compat import to_text

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
