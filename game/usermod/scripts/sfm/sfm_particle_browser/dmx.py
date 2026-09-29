# -*- coding: utf-8 -*-
"""Minimal read-only DMX parser (binary encodings 1-5 and keyvalues2).

Only what is needed to list particle systems in ``.pcf`` files: element
types/names and selected attributes.  Unwanted attribute payloads are skipped
without being decoded, which keeps scanning hundreds of files fast enough for
SFM's embedded Python 2.7.
"""
from __future__ import absolute_import

import re
import struct

from .compat import to_text, xrange


class DmxError(Exception):
    pass


class DmxColor(tuple):
    """RGBA colour, four ints 0-255."""
    __slots__ = ()
    kind = "color"


class DmxVector(tuple):
    __slots__ = ()
    kind = "vector"


class DmxVector2(DmxVector):
    __slots__ = ()
    kind = "vector2"


class DmxVector3(DmxVector):
    __slots__ = ()
    kind = "vector3"


class DmxVector4(DmxVector):
    __slots__ = ()
    kind = "vector4"


class DmxQAngle(DmxVector):
    __slots__ = ()
    kind = "qangle"


class DmxQuaternion(DmxVector):
    __slots__ = ()
    kind = "quaternion"


class DmxMatrix(DmxVector):
    __slots__ = ()
    kind = "matrix"


class DmxElement(object):
    __slots__ = ("type", "name", "id", "attributes")

    def __init__(self, type_name, name=u"", element_id=None):
        self.type = type_name
        self.name = name
        self.id = element_id
        self.attributes = {}

    def get(self, key, default=None):
        return self.attributes.get(key, default)

    def __repr__(self):
        return "<DmxElement %s %r>" % (self.type, self.name)


class DmxDocument(object):
    def __init__(self, encoding, encoding_version, fmt, fmt_version, elements):
        self.encoding = encoding
        self.encoding_version = encoding_version
        self.format = fmt
        self.format_version = fmt_version
        self.elements = elements

    @property
    def root(self):
        return self.elements[0] if self.elements else None


_HEADER_RE = re.compile(
    br"<!--\s*dmx\s+encoding\s+(\S+)\s+(\d+)\s+format\s+(\S+)\s+(\d+)\s*-->")


def read_header(data):
    """Return ``(encoding, version, format, format_version, body_offset)``."""
    start = 3 if data.startswith(b"\xef\xbb\xbf") else 0
    head = data[start:start + 512]
    match = _HEADER_RE.match(head.lstrip())
    if not match:
        raise DmxError("not a DMX file (missing <!-- dmx ... --> header)")
    offset = start + len(head) - len(head.lstrip()) + match.end()
    # Binary files follow the header with "\n" and the header's NUL terminator.
    if data[offset:offset + 2] == b"\r\n":
        offset += 2
    elif data[offset:offset + 1] == b"\n":
        offset += 1
    encoding = to_text(match.group(1))
    if encoding.startswith("binary") and data[offset:offset + 1] == b"\0":
        offset += 1
    return (encoding, int(match.group(2)), to_text(match.group(3)),
            int(match.group(4)), offset)


def load(data, wanted=None):
    """Parse DMX ``data`` (bytes).

    ``wanted`` is an optional iterable of attribute names to decode; all other
    attributes are skipped.  ``None`` decodes every supported attribute.
    """
    encoding, version, fmt, fmt_version, offset = read_header(data)
    wanted_set = None if wanted is None else set(to_text(w) for w in wanted)
    if encoding == "binary":
        elements = _parse_binary(data, offset, version, wanted_set)
    elif encoding.startswith("keyvalues2"):
        elements = _parse_kv2(to_text(data[offset:]), wanted_set)
    else:
        raise DmxError("unsupported DMX encoding %r" % encoding)
    return DmxDocument(encoding, version, fmt, fmt_version, elements)


# ---------------------------------------------------------------------------
# Binary
# ---------------------------------------------------------------------------

(K_NONE, K_ELEMENT, K_INT, K_FLOAT, K_BOOL, K_STRING, K_BINARY, K_TIME,
 K_OBJECTID, K_COLOR, K_VEC2, K_VEC3, K_VEC4, K_QANGLE, K_QUAT, K_MATRIX) = range(16)

_SIZE = {K_INT: 4, K_FLOAT: 4, K_BOOL: 1, K_TIME: 4, K_OBJECTID: 16, K_COLOR: 4,
         K_VEC2: 8, K_VEC3: 12, K_VEC4: 16, K_QANGLE: 12, K_QUAT: 16, K_MATRIX: 64}

# Encodings 1-2 store an object id at type 7; 3-5 replaced it with time.
_SCALARS_V1 = (K_NONE, K_ELEMENT, K_INT, K_FLOAT, K_BOOL, K_STRING, K_BINARY, K_OBJECTID,
               K_COLOR, K_VEC2, K_VEC3, K_VEC4, K_QANGLE, K_QUAT, K_MATRIX)
_SCALARS_V3 = (K_NONE, K_ELEMENT, K_INT, K_FLOAT, K_BOOL, K_STRING, K_BINARY, K_TIME,
               K_COLOR, K_VEC2, K_VEC3, K_VEC4, K_QANGLE, K_QUAT, K_MATRIX)
_ARRAY_OFFSET = len(_SCALARS_V1) - 1  # array type ids follow the 14 scalar ids

_I32 = struct.Struct("<i")
_I16 = struct.Struct("<h")
_U8 = struct.Struct("<B")
_F32 = struct.Struct("<f")
_TYPED = {
    K_COLOR: (struct.Struct("<4B"), DmxColor),
    K_VEC2: (struct.Struct("<2f"), DmxVector2),
    K_VEC3: (struct.Struct("<3f"), DmxVector3),
    K_VEC4: (struct.Struct("<4f"), DmxVector4),
    K_QANGLE: (struct.Struct("<3f"), DmxQAngle),
    K_QUAT: (struct.Struct("<4f"), DmxQuaternion),
    K_MATRIX: (struct.Struct("<16f"), DmxMatrix),
}


def _type_table(version):
    scalars = _SCALARS_V1 if version <= 2 else _SCALARS_V3
    table = {}
    for type_id, kind in enumerate(scalars):
        if kind != K_NONE:
            table[type_id] = (kind, False)
            table[type_id + _ARRAY_OFFSET] = (kind, True)
    return table


def _parse_binary(data, pos, version, wanted):
    if version < 1 or version > 5:
        raise DmxError("unsupported binary DMX version %d" % version)
    try:
        return _parse_binary_unchecked(data, pos, version, wanted)
    except (struct.error, IndexError) as exc:
        raise DmxError("truncated or corrupt binary DMX: %s" % exc)


def _parse_binary_unchecked(data, pos, version, wanted):
    i32 = _I32.unpack_from
    i16 = _I16.unpack_from
    find = data.find

    def cstr(p):
        end = find(b"\0", p)
        if end < 0:
            raise DmxError("unterminated string at offset %d" % p)
        return data[p:end], end + 1

    strings = None
    num_strings = 0
    wide_index = version >= 5
    if version >= 2:
        if version >= 4:
            count = i32(data, pos)[0]
            pos += 4
        else:
            count = i16(data, pos)[0]
            pos += 2
        if count < 0:
            raise DmxError("negative string table size")
        strings = []
        for _ in xrange(count):
            value, pos = cstr(pos)
            strings.append(value)
        num_strings = len(strings)

    def table_str(p):
        if wide_index:
            index = i32(data, p)[0]
            p += 4
        else:
            index = i16(data, p)[0]
            p += 2
        if 0 <= index < num_strings:
            return strings[index], p
        if index < 0:
            return None, p
        raise DmxError("string table index %d out of range" % index)

    count = i32(data, pos)[0]
    pos += 4
    if count < 0:
        raise DmxError("negative element count")
    elements = []
    for _ in xrange(count):
        if strings is not None:
            type_name, pos = table_str(pos)
        else:
            type_name, pos = cstr(pos)
        if version >= 4:
            name, pos = table_str(pos)
        else:
            name, pos = cstr(pos)
        elements.append(DmxElement(to_text(type_name) or u"", to_text(name) or u"",
                                   data[pos:pos + 16]))
        pos += 16

    num_elements = len(elements)
    types = _type_table(version)
    inline_strings = version < 4
    wanted_raw = None
    if wanted is not None:
        wanted_raw = set(w.encode("utf-8") for w in wanted)

    def element_ref(p):
        index = i32(data, p)[0]
        p += 4
        if index == -2:  # external reference: GUID string follows
            _, p = cstr(p)
            return None, p
        if 0 <= index < num_elements:
            return elements[index], p
        return None, p

    def scalar(kind, p, from_array):
        if kind == K_ELEMENT:
            return element_ref(p)
        if kind == K_STRING:
            if inline_strings or from_array:
                raw, p = cstr(p)
            else:
                raw, p = table_str(p)
            return to_text(raw), p
        if kind == K_BINARY:
            length = i32(data, p)[0]
            return data[p + 4:p + 4 + length], p + 4 + length
        if kind == K_INT:
            return i32(data, p)[0], p + 4
        if kind == K_FLOAT:
            return _F32.unpack_from(data, p)[0], p + 4
        if kind == K_BOOL:
            return _U8.unpack_from(data, p)[0] != 0, p + 1
        if kind == K_TIME:
            return i32(data, p)[0] / 10000.0, p + 4
        size = _SIZE[kind]
        typed = _TYPED.get(kind)
        if typed is not None:
            return typed[1](typed[0].unpack_from(data, p)), p + size
        return data[p:p + size], p + size

    for element in elements:
        num_attrs = i32(data, pos)[0]
        pos += 4
        for _ in xrange(num_attrs):
            if strings is not None:
                raw_name, pos = table_str(pos)
            else:
                raw_name, pos = cstr(pos)
            type_id = _U8.unpack_from(data, pos)[0]
            pos += 1
            try:
                kind, is_array = types[type_id]
            except KeyError:
                raise DmxError("unknown attribute type %d in %r" % (type_id, raw_name))
            want = wanted_raw is None or raw_name in wanted_raw
            if not is_array:
                if want:
                    value, pos = scalar(kind, pos, False)
                    element.attributes[to_text(raw_name)] = value
                elif kind in _SIZE:
                    pos += _SIZE[kind]
                else:
                    _, pos = scalar(kind, pos, False)
                continue
            length = i32(data, pos)[0]
            pos += 4
            if length < 0:
                raise DmxError("negative array length in %r" % raw_name)
            if not want and kind in _SIZE:
                pos += length * _SIZE[kind]
                continue
            values = [] if want else None
            for _ in xrange(length):
                value, pos = scalar(kind, pos, True)
                if want:
                    values.append(value)
            if want:
                element.attributes[to_text(raw_name)] = values
        if pos > len(data):
            raise DmxError("attribute data runs past end of file")
    return elements


# ---------------------------------------------------------------------------
# keyvalues2 (text)
# ---------------------------------------------------------------------------

_KV2_SCALARS = {
    "element", "int", "float", "bool", "string", "binary", "elementid", "time",
    "color", "vector2", "vector3", "vector4", "qangle", "angle", "quaternion",
    "matrix", "uint64", "uint8", "objectid",
}
_KV2_TYPED = {
    "vector2": DmxVector2, "vector3": DmxVector3, "vector4": DmxVector4, "qangle": DmxQAngle,
    "angle": DmxQAngle, "quaternion": DmxQuaternion, "matrix": DmxMatrix,
}
_ESCAPES = {"n": u"\n", "t": u"\t", "\\": u"\\", "\"": u"\"", "'": u"'"}


class _Ref(object):
    __slots__ = ("id",)

    def __init__(self, element_id):
        self.id = element_id


def _kv2_tokens(text):
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif ch == "/" and text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end + 1
        elif ch in "{}[],":
            yield ch
            i += 1
        elif ch == "\"":
            i += 1
            out = []
            while i < n and text[i] != "\"":
                if text[i] == "\\" and i + 1 < n:
                    out.append(_ESCAPES.get(text[i + 1], text[i + 1]))
                    i += 2
                else:
                    out.append(text[i])
                    i += 1
            if i >= n:
                raise DmxError("unterminated string in keyvalues2 data")
            i += 1
            yield _Str(u"".join(out))
        else:
            start = i
            while i < n and not text[i].isspace() and text[i] not in "{}[],\"":
                i += 1
            yield _Str(text[start:i])


class _Str(type(u"")):
    """Marks quoted/bare words so they can't be confused with punctuation."""
    __slots__ = ()


def _convert_kv2(type_name, raw):
    try:
        if type_name in ("int", "uint8", "uint64"):
            return int(raw)
        if type_name in ("float", "time"):
            return float(raw)
        if type_name == "bool":
            return raw.strip().lower() in ("1", "true")
        if type_name == "color":
            return DmxColor(max(0, min(255, int(float(p)))) for p in raw.split())
        if type_name in _KV2_TYPED:
            return _KV2_TYPED[type_name](float(p) for p in raw.split())
    except ValueError:
        return None
    return raw


def _parse_kv2(text, wanted):
    tokens = _kv2_tokens(text)
    elements = []
    by_id = {}
    lookahead = []

    def next_token():
        if lookahead:
            return lookahead.pop()
        try:
            return next(tokens)
        except StopIteration:
            return None

    def expect(symbol):
        token = next_token()
        if token != symbol or isinstance(token, _Str):
            raise DmxError("expected %r, got %r" % (symbol, token))

    def word():
        token = next_token()
        if not isinstance(token, _Str):
            raise DmxError("expected a string, got %r" % (token,))
        return token

    def element_body(type_name):
        element = DmxElement(type_name)
        elements.append(element)
        expect("{")
        while True:
            token = next_token()
            if token is None:
                raise DmxError("unexpected end of keyvalues2 data")
            if token == "}" and not isinstance(token, _Str):
                break
            name = token
            attr_type = word()
            if attr_type == "elementid":
                element.id = word()
                continue
            if attr_type == "element":
                value = _Ref(word())
            elif attr_type == "element_array":
                value = []
                expect("[")
                while True:
                    token = next_token()
                    if token is None:
                        raise DmxError("unterminated element_array")
                    if not isinstance(token, _Str):
                        if token == "]":
                            break
                        if token == ",":
                            continue
                        raise DmxError("unexpected %r in element_array" % token)
                    if token == "element":
                        value.append(_Ref(word()))
                    else:
                        value.append(element_body(token))
            elif attr_type.endswith("_array"):
                base = attr_type[:-len("_array")]
                value = []
                expect("[")
                while True:
                    token = next_token()
                    if token is None:
                        raise DmxError("unterminated array")
                    if not isinstance(token, _Str):
                        if token == "]":
                            break
                        continue
                    value.append(_convert_kv2(base, token))
            elif attr_type in _KV2_SCALARS:
                value = _convert_kv2(attr_type, word())
            else:
                value = element_body(attr_type)  # inline element
            if name == "name" and attr_type == "string":
                element.name = value
            elif wanted is None or name in wanted:
                element.attributes[name] = value
        if element.id:
            by_id[element.id] = element
        return element

    roots = []
    while True:
        token = next_token()
        if token is None:
            break
        if not isinstance(token, _Str):
            raise DmxError("unexpected %r at top level" % token)
        roots.append(element_body(token))

    def resolve(value):
        if isinstance(value, _Ref):
            return by_id.get(value.id)
        if isinstance(value, list):
            return [resolve(v) for v in value]
        return value

    for element in elements:
        for key, value in list(element.attributes.items()):
            element.attributes[key] = resolve(value)
    # Keep the first top-level element first: it is the document root.
    if roots:
        rest = [e for e in elements if e is not roots[0]]
        elements = [roots[0]] + rest
    return elements


# -- particle systems in a .pcf file -----------------------------------------


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
    return systems_from_document(load(data, wanted=WANTED_ATTRIBUTES))
