# -*- coding: utf-8 -*-
"""List particle systems defined in a ``.pcf`` file."""
from __future__ import absolute_import

from . import dmx

DEFINITION_TYPE = u"DmeParticleSystemDefinition"
WANTED_ATTRIBUTES = ("particleSystemDefinitions", "children", "child",
                     "preventNameBasedLookup")

FLAG_CHILD = 1
FLAG_NO_LOOKUP = 2


class SystemInfo(object):
    __slots__ = ("name", "flags", "children")

    def __init__(self, name, flags=0, children=()):
        self.name = name
        self.flags = flags
        self.children = list(children)

    @property
    def is_child(self):
        return bool(self.flags & FLAG_CHILD)

    @property
    def no_lookup(self):
        return bool(self.flags & FLAG_NO_LOOKUP)

    def to_json(self):
        return [self.name, self.flags, self.children]

    @classmethod
    def from_json(cls, item):
        return cls(item[0], int(item[1]), item[2] if len(item) > 2 else ())

    def __repr__(self):
        return "<SystemInfo %r flags=%d>" % (self.name, self.flags)


def systems_from_document(doc):
    root = doc.root
    definitions = []
    if root is not None:
        listed = root.get("particleSystemDefinitions")
        if isinstance(listed, list):
            definitions = [d for d in listed
                           if d is not None and d.type == DEFINITION_TYPE]
    if not definitions:
        definitions = [e for e in doc.elements if e.type == DEFINITION_TYPE]

    referenced = set()
    children_names = {}
    for definition in definitions:
        names = []
        for child_ref in definition.get("children") or ():
            target = child_ref.get("child") if child_ref is not None else None
            if target is not None:
                referenced.add(id(target))
                names.append(target.name)
        children_names[id(definition)] = names

    systems = []
    seen = set()
    for definition in definitions:
        if definition.name in seen:
            continue
        seen.add(definition.name)
        flags = 0
        if id(definition) in referenced:
            flags |= FLAG_CHILD
        if definition.get("preventNameBasedLookup"):
            flags |= FLAG_NO_LOOKUP
        systems.append(SystemInfo(definition.name, flags, children_names[id(definition)]))
    return systems


def scan_pcf(data):
    """Return ``[SystemInfo]`` for the PCF bytes ``data``."""
    return systems_from_document(dmx.load(data, wanted=WANTED_ATTRIBUTES))
