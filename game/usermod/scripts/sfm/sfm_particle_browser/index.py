# -*- coding: utf-8 -*-
"""Particle system index built from PCF sources, with an on-disk cache."""
from __future__ import absolute_import

from collections import OrderedDict

from . import dmx
from .compat import load_json, save_json, to_text

CACHE_VERSION = 1


class ScanCancelled(Exception):
    pass


class SystemRecord(object):
    __slots__ = ("name", "pcf", "origin", "flags", "children", "search_key")

    def __init__(self, name, pcf_path, origin, flags=0, children=()):
        self.name = name
        self.pcf = pcf_path
        self.origin = origin
        self.flags = flags
        self.children = list(children)
        self.search_key = (name + u" " + pcf_path).lower()

    @property
    def is_child(self):
        return bool(self.flags & dmx.FLAG_CHILD)

    @property
    def no_lookup(self):
        return bool(self.flags & dmx.FLAG_NO_LOOKUP)

    def __repr__(self):
        return "<SystemRecord %r in %r>" % (self.name, self.pcf)


class ParticleIndex(object):
    def __init__(self, cache_path=None, scanner=None):
        self.cache_path = cache_path
        self.scanner = scanner or dmx.scan_pcf
        self.records = []
        self.errors = []
        self.file_count = 0
        self.origin_files = OrderedDict()  # mount label -> indexed files, in mount order
        self.name_counts = {}
        self.built = False

    def build(self, sources, progress=None):
        """Index ``sources``; returns the number of files that had to be parsed.

        ``progress(done, total, label)`` may return ``False`` to cancel.
        """
        cached_files = {}
        if self.cache_path:
            cache = load_json(self.cache_path, {}) or {}
            if cache.get("version") == CACHE_VERSION:
                cached_files = cache.get("files") or {}

        files = {}
        origin_files = OrderedDict()
        records = []
        errors = []
        parsed = 0
        total = len(sources)
        for done, source in enumerate(sources):
            if progress is not None and progress(done, total, source.relpath) is False:
                raise ScanCancelled()
            key = source.key()
            try:
                signature = source.signature()
            except (IOError, OSError) as exc:
                errors.append((source.relpath, u"%s" % exc))
                continue
            cached = cached_files.get(key)
            if cached is not None and cached.get("sig") == signature:
                systems = [dmx.SystemInfo.from_json(item) for item in cached.get("systems") or ()]
                error = cached.get("error")
            else:
                parsed += 1
                try:
                    systems = self.scanner(source.read())
                    error = None
                except Exception as exc:  # a single broken file must not stop the scan
                    systems = []
                    error = u"%s: %s" % (type(exc).__name__, to_text(str(exc)))
            files[key] = {"sig": signature, "error": error,
                          "systems": [s.to_json() for s in systems]}
            origin_files[source.origin] = origin_files.get(source.origin, 0) + 1
            if error:
                errors.append((source.relpath, error))
            for system in systems:
                records.append(SystemRecord(to_text(system.name), source.relpath, source.origin,
                                            system.flags, system.children))
        if progress is not None:
            progress(total, total, u"")
        if self.cache_path and (parsed or len(files) != len(cached_files)):
            try:
                save_json(self.cache_path, {"version": CACHE_VERSION, "files": files})
            except (IOError, OSError) as exc:
                errors.append((u"<cache>", u"%s" % exc))

        counts = {}
        for record in records:
            counts[record.name] = counts.get(record.name, 0) + 1
        self.records = records
        self.errors = errors
        self.file_count = len(files)
        self.origin_files = origin_files
        self.name_counts = counts
        self.built = True
        return parsed

    def search(self, query=u"", hide_children=True, favorites=None, favorites_only=False):
        tokens = [t for t in to_text(query or u"").lower().split() if t]
        favorites = favorites or set()
        result = []
        for record in self.records:
            favorite = record.name in favorites
            if favorites_only and not favorite:
                continue
            if hide_children and record.flags and not favorite:
                continue
            if tokens and not all(t in record.search_key for t in tokens):
                continue
            result.append(record)

        if tokens:
            joined = u" ".join(tokens)
            first = tokens[0]

            def rank(record):
                name = record.name.lower()
                if name == joined:
                    score = 0
                elif name.startswith(first):
                    score = 1
                elif first in name:
                    score = 2
                else:
                    score = 3  # matched only through the file path
                return (score, name, record.pcf.lower())
        else:
            def rank(record):
                return (record.name not in favorites, record.name.lower(), record.pcf.lower())
        result.sort(key=rank)
        return result
