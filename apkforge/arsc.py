"""A small reader for resources.arsc (the compiled resource table), standard library only.

It exists to check what `aapt2 optimize` did, with a second pair of eyes that is not aapt2: which resource files the table points at (and that every one
of them is in the APK), how many resource names the key pool still holds, and which type chunks are sparse or compact.  It reads what the tools
that matter write (aapt2's tables: one or more packages, the type and key pools, type specs and type chunks, plain and sparse and 16-bit-offset and
compact entries, plain and complex entries) and skips every other chunk by its size.  It does not interpret values beyond (data type, data); it never
follows a reference.  Anything that does not parse raises ArscError, which is a ValueError: callers decide what an unreadable table means.

Layout (frameworks/base/libs/androidfw/include/androidfw/ResourceTypes.h): every chunk starts with a header {u16 type, u16 headerSize, u32 size}.
"""
import struct

RES_STRING_POOL = 0x0001
RES_TABLE = 0x0002
RES_TABLE_PACKAGE = 0x0200
RES_TABLE_TYPE = 0x0201
RES_TABLE_TYPE_SPEC = 0x0202

UTF8_FLAG = 1 << 8
TYPE_FLAG_SPARSE = 0x01
TYPE_FLAG_OFFSET16 = 0x02
ENTRY_FLAG_COMPLEX = 0x0001
ENTRY_FLAG_COMPACT = 0x0008
NO_ENTRY32 = 0xFFFFFFFF
NO_ENTRY16 = 0xFFFF
TYPE_STRING = 0x03                 # Res_value.dataType of a string (an index into the table's global string pool)
MAX_CHUNKS = 1 << 20               # a table is built by a tool, but this reader is also pointed at files from anywhere: no endless loops


class ArscError(ValueError):
    pass


def _chunk(b, p, end):
    """-> (type, headerSize, size) of the chunk at p, checked against `end` (the end of the enclosing chunk)"""
    if p + 8 > end:
        raise ArscError("a chunk header runs past the end of its parent (offset %d)" % p)
    t, hs, size = struct.unpack_from("<HHI", b, p)
    if hs < 8 or size < hs or p + size > end:
        raise ArscError("chunk 0x%04x at %d has a bad size (header %d, size %d, parent ends at %d)" % (t, p, hs, size, end))
    return t, hs, size


def _length(b, p, wide):
    """the length prefix of a string-pool string: one unit, or two when the high bit of the first is set -> (length, bytes used)"""
    if wide:
        x = struct.unpack_from("<H", b, p)[0]
        return ((x & 0x7FFF) << 16 | struct.unpack_from("<H", b, p + 2)[0], 4) if x & 0x8000 else (x, 2)
    x = b[p]
    return (((x & 0x7F) << 8) | b[p + 1], 2) if x & 0x80 else (x, 1)


def string_pool(b, p):
    """The string pool chunk at p -> (list of str, size of the chunk).  Style spans are skipped: only the text is read."""
    t, hs, size = _chunk(b, p, len(b))
    if t != RES_STRING_POOL:
        raise ArscError("expected a string pool at %d, found chunk 0x%04x" % (p, t))
    count, _styles, flags, start = struct.unpack_from("<IIII", b, p + 8)
    if hs + 4 * count > size or start > size:
        raise ArscError("string pool at %d: %d strings do not fit in %d bytes" % (p, count, size))
    out = []
    offsets = struct.unpack_from("<%dI" % count, b, p + hs) if count else ()
    wide = not flags & UTF8_FLAG
    end = p + size
    for off in offsets:
        q = p + start + off
        if q >= end:
            raise ArscError("string pool at %d: a string starts outside the pool" % p)
        try:
            if wide:
                n, used = _length(b, q, True)
                raw = b[q + used:q + used + 2 * n]
                out.append(raw.decode("utf-16-le", "replace"))
            else:
                _n16, u1 = _length(b, q, False)
                nbytes, u2 = _length(b, q + u1, False)
                out.append(b[q + u1 + u2:q + u1 + u2 + nbytes].decode("utf-8", "replace"))
        except (struct.error, IndexError):
            raise ArscError("string pool at %d: a string runs past the end of the data" % p)
    return out, size


class Entry:
    """one entry of one configuration: its key (index into the package's key pool), whether it is complex, and for a simple entry its (dataType, data)"""
    __slots__ = ("key", "complex", "value")

    def __init__(self, key, complex_, value):
        self.key, self.complex, self.value = key, complex_, value


class Config:
    """one type chunk: the entries one configuration (of a type) defines"""
    __slots__ = ("type_id", "flags", "config", "entry_count", "entries", "compact")

    def __init__(self, type_id, flags, config, entry_count):
        self.type_id, self.flags, self.config, self.entry_count = type_id, flags, config, entry_count
        self.entries = {}                              # entry id -> Entry
        self.compact = 0                               # how many entries use the compact layout

    @property
    def sparse(self):
        return bool(self.flags & TYPE_FLAG_SPARSE)

    @property
    def offset16(self):
        return bool(self.flags & TYPE_FLAG_OFFSET16)


class Package:
    __slots__ = ("id", "name", "type_names", "keys", "configs", "spec_counts", "type_id_offset")

    def __init__(self, pid, name):
        self.id, self.name = pid, name
        self.type_id_offset = 0
        self.type_names, self.keys = [], []
        self.configs = []                              # [Config]
        self.spec_counts = {}                          # type id -> entry count of its type spec


class Table:
    __slots__ = ("strings", "packages")

    def __init__(self):
        self.strings, self.packages = [], []

    # ------------------------------------------------------------------------------------------------------------- questions
    def type_name(self, pkg, type_id):
        i = type_id - 1
        return pkg.type_names[i] if 0 <= i < len(pkg.type_names) and not pkg.type_id_offset else "?%d" % type_id

    def resource_ids(self):
        """{resource id} of every entry any configuration defines"""
        return {(p.id << 24) | (c.type_id << 16) | e for p in self.packages for c in p.configs for e in c.entries}

    def key_names(self):
        """the key pool strings of every package (the resource names; a collapsed table has one shared value and the names left exempt)"""
        return [k for p in self.packages for k in p.keys]

    def file_references(self):
        """{path}: the strings that simple, non-string entries hold and that start with `res/` (what the table says lives in the APK as a file).  A string
        resource whose text starts with `res/` is not a file, so entries of type `string` are left out."""
        out = set()
        for p in self.packages:
            for c in p.configs:
                if self.type_name(p, c.type_id) == "string":
                    continue
                for e in c.entries.values():
                    if not e.complex and e.value and e.value[0] == TYPE_STRING and 0 <= e.value[1] < len(self.strings):
                        s = self.strings[e.value[1]]
                        if s.startswith("res/"):
                            out.add(s)
        return out

    def value_of(self, e):
        """a comparable form of a simple entry's value: strings are replaced by their text, everything else stays (dataType, data); complex -> 'complex'"""
        if e.complex:
            return "complex"
        if e.value and e.value[0] == TYPE_STRING and 0 <= e.value[1] < len(self.strings):
            return ("string", self.strings[e.value[1]])
        return e.value

    def named_ids(self):
        """{resource id: 'type/entry'}: the first name any configuration gives the id (a collapsed table gives the one shared value)"""
        out = {}
        for p in self.packages:
            for c in p.configs:
                tn = self.type_name(p, c.type_id)
                for i, e in c.entries.items():
                    rid = (p.id << 24) | (c.type_id << 16) | i
                    if rid not in out:
                        out[rid] = "%s/%s" % (tn, p.keys[e.key] if 0 <= e.key < len(p.keys) else "?%d" % e.key)
        return out

    def values(self):
        """{(resource id, config bytes): comparable value} for every entry of every configuration (see value_of)"""
        return {((p.id << 24) | (c.type_id << 16) | i, c.config): self.value_of(e) for p in self.packages for c in p.configs for i, e in c.entries.items()}

    def summary(self):
        """counts for a report: configs (type chunks), sparse ones, compact entries, entries, distinct key names"""
        cfgs = [c for p in self.packages for c in p.configs]
        return {"packages": len(self.packages), "type_chunks": len(cfgs), "sparse_chunks": sum(1 for c in cfgs if c.sparse),
                "offset16_chunks": sum(1 for c in cfgs if c.offset16), "compact_entries": sum(c.compact for c in cfgs),
                "entries": sum(len(c.entries) for c in cfgs), "key_names": len(set(self.key_names())), "global_strings": len(self.strings)}


def _entry(b, p, limit):
    """the entry at p (an absolute offset) -> (Entry, compact?)"""
    if p + 8 > limit:
        raise ArscError("a table entry runs past its type chunk")
    size, flags, key = struct.unpack_from("<HHI", b, p)
    if flags & ENTRY_FLAG_COMPACT:
        # compact layout: u16 key | u16 flags (its high byte is the dataType) | u32 data
        return Entry(size, False, (flags >> 8, key)), True
    if flags & ENTRY_FLAG_COMPLEX:
        if p + 16 > limit:
            raise ArscError("a complex table entry runs past its type chunk")
        return Entry(key, True, None), False
    if p + size + 8 > limit:
        raise ArscError("a table entry runs past its type chunk")
    _vsize, _res0, dtype, data = struct.unpack_from("<HBBI", b, p + size)
    return Entry(key, False, (dtype, data)), False


def _type_chunk(b, p, size, hs):
    tid, flags, _res, count, entries_start = struct.unpack_from("<BBHII", b, p + 8)
    cfg_size = struct.unpack_from("<I", b, p + 20)[0]
    cfg = bytes(b[p + 20:p + 20 + min(cfg_size, hs - 20)])
    c = Config(tid, flags, cfg, count)
    end = p + size
    if flags & TYPE_FLAG_SPARSE:
        if p + hs + 4 * count > end:
            raise ArscError("a sparse type chunk lists %d entries that do not fit in %d bytes" % (count, size))
        for i in range(count):
            idx, off = struct.unpack_from("<HH", b, p + hs + 4 * i)
            e, compact = _entry(b, p + entries_start + off * 4, end)
            c.entries[idx] = e
            c.compact += compact
        return c
    wide = not flags & TYPE_FLAG_OFFSET16
    if p + hs + (4 if wide else 2) * count > end:
        raise ArscError("a type chunk lists %d entries that do not fit in %d bytes" % (count, size))
    for i in range(count):
        if wide:
            off = struct.unpack_from("<I", b, p + hs + 4 * i)[0]
            if off == NO_ENTRY32:
                continue
        else:
            off = struct.unpack_from("<H", b, p + hs + 2 * i)[0]
            if off == NO_ENTRY16:
                continue
            off *= 4
        e, compact = _entry(b, p + entries_start + off, end)
        c.entries[i] = e
        c.compact += compact
    return c


def _package(b, p, size, hs):
    pid = struct.unpack_from("<I", b, p + 8)[0]
    raw = b[p + 12:p + 12 + 256]
    name = raw.decode("utf-16-le", "replace").split("\0", 1)[0]
    type_strings, _lpt, key_strings, _lpk = struct.unpack_from("<IIII", b, p + 268)
    pkg = Package(pid, name)
    if hs >= 288:
        pkg.type_id_offset = struct.unpack_from("<I", b, p + 284)[0]
    end = p + size
    if type_strings:
        pkg.type_names, _ = string_pool(b, p + type_strings)
    if key_strings:
        pkg.keys, _ = string_pool(b, p + key_strings)
    q = p + hs
    for _ in range(MAX_CHUNKS):
        if q >= end:
            break
        t, chs, csize = _chunk(b, q, end)
        if t == RES_TABLE_TYPE:
            pkg.configs.append(_type_chunk(b, q, csize, chs))
        elif t == RES_TABLE_TYPE_SPEC:
            tid, _r0, _r1, count = struct.unpack_from("<BBHI", b, q + 8)
            pkg.spec_counts[tid] = count
        q += csize
    else:
        raise ArscError("too many chunks in a package")
    return pkg


def parse(b):
    """resources.arsc bytes -> Table"""
    b = bytes(b)
    if len(b) < 12:
        raise ArscError("too short to be a resource table")
    t, hs, size = _chunk(b, 0, len(b))
    if t != RES_TABLE:
        raise ArscError("not a resource table (the first chunk is 0x%04x)" % t)
    count = struct.unpack_from("<I", b, 8)[0]
    tab = Table()
    q, end = hs, size
    for _ in range(MAX_CHUNKS):
        if q >= end:
            break
        ct, chs, csize = _chunk(b, q, end)
        if ct == RES_STRING_POOL and not tab.strings:
            tab.strings, _ = string_pool(b, q)
        elif ct == RES_TABLE_PACKAGE:
            tab.packages.append(_package(b, q, csize, chs))
        q += csize
    else:
        raise ArscError("too many chunks in the table")
    if len(tab.packages) != count:
        raise ArscError("the table header says %d package(s), found %d" % (count, len(tab.packages)))
    return tab
