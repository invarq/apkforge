"""Deterministic ZIP writer for APKs.

Same input -> same bytes (one timestamp for every entry, stable order). The timestamp is 1980-01-01 00:00:00, the ZIP format's zero, unless
SOURCE_DATE_EPOCH asks for another (see stamp_from_env). Stored entries can be aligned the way zipalign does it
(an 0xD935 extra field), so no separate zipalign pass is needed - and none is allowed after signing.
The writer exposes the three ZIP sections separately, because APK Signature Scheme v2/v3 digests them separately and
the signing block goes between the entries and the central directory.
"""
import calendar
import os
import re
import struct
import time
import zlib

from .util import ForgeError

DOS_TIME = 0
DOS_DATE = (1 << 5) | 1          # 1980-01-01, the earliest representable date
DEFAULT_STAMP = (DOS_TIME, DOS_DATE)
PAGE_16K = 16384

# SOURCE_DATE_EPOCH (https://reproducible-builds.org/specs/source-date-epoch/) is how a build system hands "the" time of a build to the tools in it.
# Unset, apkforge keeps its own fixed stamp: the same input gives the same bytes on every machine and on every day.
EPOCH_ENV = "SOURCE_DATE_EPOCH"
_EPOCH_MIN = calendar.timegm((1980, 1, 1, 0, 0, 0))
_EPOCH_MAX = calendar.timegm((2107, 12, 31, 23, 59, 58))      # the last second a DOS date/time can hold (it counts in steps of 2)


def dos_stamp(epoch):
    """(dos_time, dos_date) for a Unix timestamp: read in UTC (a DOS stamp has no time zone), rounded DOWN to the format's 2-second step (so never
    later than the source date) and clamped to 1980-01-01 00:00:00 .. 2107-12-31 23:59:58, the range a DOS date can hold."""
    t = time.gmtime(min(max(int(epoch), _EPOCH_MIN), _EPOCH_MAX))
    return (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2), ((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday


def stamp_from_env(environ=None):
    """The (dos_time, dos_date) the environment asks for: SOURCE_DATE_EPOCH when it is set, else DEFAULT_STAMP (1980-01-01 00:00:00).
    An empty value counts as unset (CI systems often define the variable blank). Anything else that is not a non-negative integer (seconds
    since 1970-01-01 00:00:00 UTC, digits only) is an error: the specification says a malformed value must stop the build, not be guessed at."""
    raw = (os.environ if environ is None else environ).get(EPOCH_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_STAMP
    s = raw.strip()
    if not re.fullmatch(r"[0-9]+", s):
        raise ForgeError("%s must be a non-negative integer (seconds since 1970-01-01 00:00:00 UTC), not %r" % (EPOCH_ENV, raw))
    return dos_stamp(int(s) if len(s) <= 18 else _EPOCH_MAX)          # 18+ digits is far past 2107 anyway: clamp without a huge int


def stamp_label(stamp):
    """'2023-11-14 22:13:20' for a (dos_time, dos_date) pair"""
    t, d = stamp
    return "%04d-%02d-%02d %02d:%02d:%02d" % ((d >> 9) + 1980, (d >> 5) & 15, d & 31, t >> 11, (t >> 5) & 63, (t & 31) * 2)


def stamp_cache_key(environ=None):
    """None for the default stamp, else its label. The build mixes it into the cache key of every action, because some actions store zip bytes
    (classes.jar) that carry the stamp. The default adds nothing, so a cache written without SOURCE_DATE_EPOCH (also by 1.1.0) stays valid."""
    st = stamp_from_env(environ)
    return None if st == DEFAULT_STAMP else stamp_label(st)


class ZipBuilder:
    def __init__(self, stamp=None):
        """stamp: a (dos_time, dos_date) pair for every entry; None = whatever the environment asks for (SOURCE_DATE_EPOCH, see stamp_from_env)."""
        self.dos_time, self.dos_date = stamp or stamp_from_env()
        self.buf = bytearray()
        self.cd = []              # (name_bytes, flags, method, crc, csize, usize, offset)
        self.names = set()

    # ------------------------------------------------------------------------------------------ entries
    def add(self, name, data, store=False, align=0, level=9):
        """Add one file. store=True keeps it uncompressed. Every entry that ends up STORED (store=True, or deflate could not shrink it)
        is aligned to `align` bytes, or to 4 when align is 0/1; compressed entries are never padded."""
        if name in self.names:
            raise ValueError("duplicate zip entry: " + name)
        self.names.add(name)
        nb = name.encode("utf-8")
        flags = 0x0800 if any(c > 127 for c in nb) else 0
        crc = zlib.crc32(data) & 0xFFFFFFFF
        method, payload = 0, data
        if not store and len(data) > 0:
            co = zlib.compressobj(level, zlib.DEFLATED, -15)
            comp = co.compress(data) + co.flush()
            if len(comp) < len(data):
                method, payload = 8, comp
        extra = b""
        if method == 0:
            # EVERY stored entry is aligned: store=True and the fallback above (deflate could not shrink it) both end up here.
            # `align` <= 1 means the default, 4 (what zipalign -c checks); an explicit larger alignment (16 KB for .so) is honoured.
            al = align if align > 1 else 4
            if al % 4:
                raise ValueError("align must be a multiple of 4, got %d" % align)
            pad = (-(len(self.buf) + 30 + len(nb))) % al
            if pad:
                if pad < 4:                       # an extra field needs a 4-byte header: skip to the next boundary instead
                    pad += al
                extra = struct.pack("<HH", 0xD935, pad - 4) + (struct.pack("<H", al & 0xFFFF) + b"\0" * pad)[:pad - 4]
        off = len(self.buf)
        ver = 20 if method == 8 else 10
        self.buf += struct.pack("<IHHHHHIIIHH", 0x04034B50, ver, flags, method, self.dos_time, self.dos_date, crc,
                                len(payload), len(data), len(nb), len(extra))
        self.buf += nb + extra + payload
        self.cd.append((nb, flags, method, crc, len(payload), len(data), off))

    def add_stored_aligned(self, name, data, align=4):
        self.add(name, data, store=True, align=align)

    def items(self):
        """(name, uncompressed bytes) for every entry added so far, in order."""
        for nb, flags, method, crc, cs, us, off in self.cd:
            hl = 30 + len(nb) + struct.unpack_from("<H", self.buf, off + 28)[0]
            raw = bytes(self.buf[off + hl:off + hl + cs])
            yield nb.decode("utf-8"), (raw if method == 0 else zlib.decompress(raw, -15))

    # ------------------------------------------------------------------------------------------ sections
    def entries_bytes(self):
        return bytes(self.buf)

    def central_directory(self):
        out = bytearray()
        for nb, flags, method, crc, cs, us, off in self.cd:
            ver = 20 if method == 8 else 10
            out += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, ver, flags, method, self.dos_time, self.dos_date, crc,
                               cs, us, len(nb), 0, 0, 0, 0, 0, off)
            out += nb
        return bytes(out)

    def eocd(self, cd_offset, cd_size):
        n = len(self.cd)
        if n > 0xFFFF or cd_offset > 0xFFFFFFFF:
            raise ValueError("zip64 is not supported (more than 65535 entries or a file over 4 GB)")
        return struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, n, n, cd_size, cd_offset, 0)

    def finish(self, signing_block=b""):
        """The complete zip. A signing block (APK) is inserted between the entries and the central directory."""
        cd = self.central_directory()
        return bytes(self.buf) + signing_block + cd + self.eocd(len(self.buf) + len(signing_block), len(cd))


# ---------------------------------------------------------------------------------------------- reading helpers
def parse_sections(apk_bytes):
    """Locate (entries_end, cd_offset, cd_size, eocd_offset) in a zip held in memory. Handles an inserted signing block."""
    i = apk_bytes.rfind(b"PK\x05\x06")
    if i < 0:
        raise ValueError("not a zip: end of central directory not found")
    (_, _, _, n, _, cd_size, cd_off, clen) = struct.unpack("<IHHHHIIH", apk_bytes[i:i + 22])
    return cd_off, cd_size, i, n
