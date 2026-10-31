"""`aapt2 optimize` for release APKs (opt-in): shorter res/ paths, sparse resource tables and, only when asked for, collapsed resource names.

What the real aapt2 35.0.2 does (measured: tools/optimize_e2e.sh and CHECKLIST Part B, "aapt2 optimize"):
  --shorten-resource-paths     res/layout/a.xml -> res/1k.xml.  Safe for everything that reaches a resource through its id.
  --enable-sparse-encoding     only has an effect from minSdk 32 (its help text says Android O; the binary does not do that).
  --collapse-resource-names    every name becomes one shared value, so Resources.getIdentifier(name, ...) finds nothing for it.
  --deduplicate-entry-values   does nothing unless the names are collapsed.
  --enable-compact-entries     is a `link` flag (minSdk 34+); `optimize` rejects it and writes the plain layout again.
The file behind --resources-config-path is a trap, and the reason this module validates before it writes: `string/*#no_collapse`, `string/x # no_collapse`,
`string/x#typo` and a name that does not exist are all ACCEPTED AND DO NOTHING (the resource is collapsed anyway); `pkg:string/x` and a `# comment` line
are errors; `string/x#remove` DELETES the resource.  So a "keep" name must be type/name in plain characters, must exist, and only the one directive this
module writes (no_collapse) ever reaches the file.

After aapt2 ran, `check` reads the input and the output table with arsc.py (not aapt2) and refuses the result unless every resource id, every value (file paths
read back through the map) and every kept name survived.  The ids are what R.java was compiled against: a table that lost one would crash at run time.
"""
import difflib
import json
import os
import re
import zipfile

from . import arsc
from .util import ForgeError

SPARSE_MIN_SDK = 32               # --enable-sparse-encoding changes nothing below this (measured with aapt2 35.0.2)
COMPACT_MIN_SDK = 34              # --enable-compact-entries (a `link` flag) changes nothing below this
KEYS = ("shortenPaths", "sparseEncoding", "collapseNames", "dedupe", "keep")
KEEP_RE = re.compile(r"^[a-z][a-z0-9]*/[A-Za-z0-9_.]+$")        # type/name, the one shape aapt2's resources config takes
MAP_SUFFIX = "-resources-map"                                    # build/outputs/<applicationId>-<variant>-resources-map.txt and .pb
RESOURCES_CLASS = b"android/content/res/Resources"


class Options:
    __slots__ = ("shorten", "sparse", "collapse", "dedupe", "keep")

    def __init__(self, shorten=True, sparse=True, collapse=False, dedupe=True, keep=()):
        self.shorten, self.sparse, self.collapse, self.dedupe, self.keep = shorten, sparse, collapse, dedupe, tuple(keep)


# ------------------------------------------------------------------------------------------------------------ settings
def validate_keep(keep, where="optimizeResources"):
    """The "keep" list -> sorted unique ['type/name'].  Every refusal says what aapt2 would have done with the line."""
    if not isinstance(keep, (list, tuple)):
        raise ForgeError('%s: "keep" must be a list of "type/name" strings, not %s' % (where, json.dumps(keep)))
    out = set()
    for k in keep:
        if not isinstance(k, str) or not k:
            raise ForgeError('%s: "keep" holds %s; each entry is a "type/name" string such as "string/google_app_id"' % (where, json.dumps(k)))
        if "*" in k:
            raise ForgeError('%s: "keep" entry %s: aapt2 has no wildcards (it accepts the line and keeps nothing); list each resource' % (where, json.dumps(k)))
        if ":" in k:
            raise ForgeError('%s: "keep" entry %s: aapt2 rejects a package here; write type/name' % (where, json.dumps(k)))
        if not KEEP_RE.fullmatch(k):                       # fullmatch: `$` alone would let "string/x\n" through (it matches before a trailing newline)
            raise ForgeError('%s: "keep" entry %s is not a resource name of the form type/name (letters, digits, _ and . only: # and , are aapt2 directive syntax '
                             'and a #remove would delete the resource)' % (where, json.dumps(k)))
        out.add(k)
    return sorted(out)


def parse_option(value, where="optimizeResources"):
    """The "optimizeResources" setting -> Options, or None when it is off.  true = the safe set (shorter paths; sparse tables from minSdk 32); an object adjusts it:
    shortenPaths, sparseEncoding, collapseNames (needs "keep"), dedupe, keep."""
    if value is False or value is None:
        return None
    if value is True:
        value = {}
    if not isinstance(value, dict):
        raise ForgeError("%s: must be true, false or an object, not %s" % (where, json.dumps(value)))
    unknown = sorted(set(value) - set(KEYS))
    if unknown:
        raise ForgeError("%s: unknown key %s (known: %s)" % (where, ", ".join(json.dumps(k) for k in unknown), ", ".join(KEYS)))
    flags = {}
    for k, default in (("shortenPaths", True), ("sparseEncoding", True), ("collapseNames", False), ("dedupe", True)):
        v = value.get(k, default)
        if not isinstance(v, bool):
            raise ForgeError("%s: \"%s\" must be true or false, not %s" % (where, k, json.dumps(v)))
        flags[k] = v
    keep = validate_keep(value["keep"], where) if "keep" in value else None
    if flags["collapseNames"] and keep is None:
        raise ForgeError('%s: "collapseNames" renames every resource, so code that looks one up by name (Resources.getIdentifier) stops finding it.  '
                         'Say what must stay: "keep": ["string/google_app_id", ...], or "keep": [] when nothing is looked up by name' % where)
    return Options(flags["shortenPaths"], flags["sparseEncoding"], flags["collapseNames"], flags["dedupe"], keep or ())


# ---------------------------------------------------------------------------------------------------------------- plan
def table_names(table):
    """{'type/entry'}: every resource of the table, in the form `keep` takes"""
    out = set()
    for p in table.packages:
        for c in p.configs:
            tn = table.type_name(p, c.type_id)
            for e in c.entries.values():
                if 0 <= e.key < len(p.keys):
                    out.add("%s/%s" % (tn, p.keys[e.key]))
    return out


class Plan:
    """what to run: the aapt2 flags, the resources config text, and the notes to print"""

    def __init__(self, flags, cfg, keep, notes, collapse, shorten):
        self.flags, self.cfg, self.keep, self.notes, self.collapse, self.shorten = list(flags), cfg, list(keep), list(notes), collapse, shorten

    @property
    def noop(self):
        return not self.flags

    @property
    def wants_map(self):
        return self.collapse or self.shorten

    def command(self, aapt2, src, out, cfg_path=None, map_path=None):
        cmd = [aapt2, "optimize", "-o", out] + self.flags
        if cfg_path and self.cfg:
            cmd += ["--resources-config-path", cfg_path]
        if map_path and self.wants_map:
            cmd += ["--save-obfuscation-map", map_path]
        return cmd + [src]


def _has_flag(help_text, flag):
    return re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", help_text) is not None


def check_keep(keep, table):
    """Every "keep" name must be a resource of the table.  Raises ForgeError for one that is not.  (plan() asks this for the APK, make_aab() for the bundle.)"""
    have = table_names(table)
    bad = []
    for k in keep:
        if k not in have:
            close = difflib.get_close_matches(k, sorted(have), n=3, cutoff=0.7)
            bad.append(k + (" (did you mean %s?)" % ", ".join(close) if close else ""))
    if bad:
        raise ForgeError('optimizeResources: "keep" names no such resource: %s.  aapt2 accepts a name that does not exist and then collapses the real resource '
                         'anyway, so a typo here would ship a broken lookup' % "; ".join(bad))


def plan(opt, table, min_sdk, help_text):
    """Options + the linked table + minSdk + `aapt2 optimize -h` -> Plan.  Raises ForgeError for a keep name that is not a resource, or for an aapt2 too old for a flag."""
    notes, flags = [], []
    keep = list(opt.keep) if opt.collapse else []
    if opt.keep and not opt.collapse:
        notes.append('optimizeResources: "keep" has no effect without "collapseNames" (nothing is renamed)')
    if opt.collapse:
        check_keep(keep, table)
    if opt.shorten:
        flags.append("--shorten-resource-paths")
    if opt.sparse:
        if min_sdk >= SPARSE_MIN_SDK:
            flags.append("--enable-sparse-encoding")
        else:
            notes.append("optimizeResources: sparse encoding skipped (aapt2 applies it from minSdk %d; this app's minSdk is %d)" % (SPARSE_MIN_SDK, min_sdk))
    if opt.collapse:
        flags.append("--collapse-resource-names")
        if opt.dedupe:
            flags.append("--deduplicate-entry-values")
    p = Plan(flags, "".join("%s#no_collapse\n" % k for k in sorted(keep)), keep, notes, opt.collapse, opt.shorten)
    need = flags + (["--save-obfuscation-map"] if p.wants_map and flags else []) + (["--resources-config-path"] if p.cfg else [])
    lacking = [f for f in need if not _has_flag(help_text, f)]
    if lacking:
        raise ForgeError("this aapt2 does not offer %s in `aapt2 optimize` (needed by optimizeResources); use aapt2 from build-tools 33 or newer "
                         "(APKFORGE_AAPT2=/path/to/aapt2) or switch that setting off" % ", ".join(lacking))
    return p


# ------------------------------------------------------------------------------------------------- the obfuscation map
def _varint(b, p):
    r = s = 0
    while True:
        if p >= len(b) or s > 63:
            raise ValueError("truncated or oversized varint")
        x = b[p]
        p += 1
        r |= (x & 0x7F) << s
        if not x & 0x80:
            return r, p
        s += 7


def _fields(b):
    p = 0
    while p < len(b):
        k, p = _varint(b, p)
        f, w = k >> 3, k & 7
        if w == 0:
            v, p = _varint(b, p)
        elif w == 2:
            n, p = _varint(b, p)
            if p + n > len(b):
                raise ValueError("truncated length-delimited field")
            v, p = b[p:p + n], p + n
        elif w in (1, 5):
            n = 8 if w == 1 else 4
            if p + n > len(b):
                raise ValueError("truncated fixed-width field")
            v, p = b[p:p + n], p + n
        else:
            raise ValueError("unsupported protobuf wire type %d" % w)
        yield f, w, v


def decode_map(blob):
    """`aapt2 optimize --save-obfuscation-map` (aapt.pb.ResourceMappings: shortened_paths = 1, collapsed_names = 2) -> {"paths": {short: original}, "names": {id: entry name}}"""
    paths, names = {}, {}
    for f, w, v in _fields(blob):
        if w != 2 or f not in (1, 2):
            continue
        for f2, w2, v2 in _fields(v):
            if f2 != 1 or w2 != 2:
                continue
            d = {f3: v3 for f3, _w3, v3 in _fields(v2)}
            if f == 1:
                paths[bytes(d.get(1, b"")).decode("utf-8")] = bytes(d.get(2, b"")).decode("utf-8")
            else:
                names[d.get(1, 0)] = bytes(d.get(2, b"")).decode("utf-8")
    return {"paths": paths, "names": names}


def render_map(decoded, kinds, appid, variant):
    """the readable form of the map (sorted, so equal builds write equal files).  kinds = Table.named_ids() of the table BEFORE collapsing."""
    lines = ["# apkforge: what `aapt2 optimize` renamed in %s (%s).  The APK does not need this file; keep it with the build to read a shortened path or a collapsed name back." % (appid, variant),
             "# paths: shortened <- original"]
    lines += ["path %s <- %s" % (s, o) for s, o in sorted(decoded["paths"].items(), key=lambda kv: (kv[1], kv[0]))]
    lines.append("# names: id = type/name before collapsing")
    lines += ["name 0x%08x = %s" % (i, kinds.get(i, "?/" + n)) for i, n in sorted(decoded["names"].items())]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------------------ check the result
def _read(apk):
    with zipfile.ZipFile(apk) as z:
        return arsc.parse(z.read("resources.arsc")), set(z.namelist()), z.getinfo("resources.arsc").file_size


def check(before_apk, after_apk, p, map_blob):
    """Compare the linked APK with the optimized one using arsc.py.  Raises ForgeError when optimize lost or changed anything it should not have;
    returns the numbers the build prints and records."""
    try:
        t0, names0, arsc0 = _read(before_apk)
        t1, names1, arsc1 = _read(after_apk)
        decoded = decode_map(map_blob) if map_blob else {"paths": {}, "names": {}}
    except (arsc.ArscError, ValueError, KeyError, zipfile.BadZipFile) as e:
        raise ForgeError("aapt2 optimize wrote something apkforge cannot read back (%s); the unoptimized resources are not used" % e)
    ids0, ids1 = t0.resource_ids(), t1.resource_ids()
    if ids0 != ids1:
        lost = sorted(ids0 - ids1)[:5]
        raise ForgeError("aapt2 optimize changed the set of resource ids (%d before, %d after; first lost: %s).  R.java was compiled against the ids before, "
                         "so the result is not used" % (len(ids0), len(ids1), ", ".join("0x%08x" % i for i in lost) or "none, ids were added"))
    missing = sorted(t1.file_references() - names1)
    if missing:
        raise ForgeError("the optimized resource table points at %d file(s) that are not in the APK (%s)" % (len(missing), ", ".join(missing[:3])))
    kinds = t0.named_ids()
    v0, v1, paths = t0.values(), t1.values(), decoded["paths"]
    if set(v0) != set(v1):
        raise ForgeError("aapt2 optimize changed which configurations define resources (%d entries before, %d after)" % (len(v0), len(v1)))
    for key, was in v0.items():
        now = v1[key]
        if isinstance(now, tuple) and now[0] == "string" and now[1] in paths and not kinds.get(key[0], "").startswith("string/"):
            now = ("string", paths[now[1]])                              # a shortened file path, read back through the map
        if now != was:
            raise ForgeError("aapt2 optimize changed the value of resource 0x%08x (%s): %r before, %r after" % (key[0], kinds.get(key[0], "?"), was, now))
    for short, orig in paths.items():
        if short not in names1 or orig not in names0:
            raise ForgeError("the resource map says %s was shortened to %s, but one of them is not in the APK" % (orig, short))
    after_keys = set(t1.key_names())
    by_name = {v: k for k, v in kinds.items()}
    for k in p.keep:
        if k.split("/", 1)[1] not in after_keys or by_name.get(k) in decoded["names"]:
            raise ForgeError("%s was collapsed although it is in \"keep\"" % k)
    for rid, n in decoded["names"].items():
        if kinds.get(rid, "").split("/", 1)[-1] != n:
            raise ForgeError("the resource map names 0x%08x %s, the table said %s" % (rid, n, kinds.get(rid, "nothing")))
    s1 = t1.summary()
    return {"apk_before": _size(before_apk), "apk_after": _size(after_apk), "arsc_before": arsc0, "arsc_after": arsc1, "entries": len(v0), "ids": len(ids0),
            "names_before": t0.summary()["key_names"], "names_after": s1["key_names"], "names_collapsed": len(decoded["names"]),
            "paths_shortened": len(paths), "sparse_chunks": s1["sparse_chunks"], "kept": list(p.keep)}


def _size(path):
    return os.path.getsize(path)


# ---------------------------------------------------------------------------------------- the final APK: verify's claim
_DUMP_ID = re.compile(r"^\s*resource (0x[0-9a-fA-F]{8}) ", re.M)


def dump_ids(text):
    """the resource ids `aapt2 dump resources` lists (the second reader of the table)"""
    return {int(m, 16) for m in _DUMP_ID.findall(text)}


def final_table(apk):
    """-> (resource ids, file references, files missing from the APK, entries) of the finished APK's table, read with arsc.py"""
    t, names, _ = _read(apk)
    refs = t.file_references()
    return t.resource_ids(), refs, sorted(refs - names), t.summary()["entries"]


# ------------------------------------------------------------------------------------- the getIdentifier guard (collapse)
def scan_class(b):
    """-> (the String constants of one class file, does it call android.content.res.Resources.getIdentifier?).  CONSTANT_String is what a lookup by name
    passes; the field names of an R class are Utf8 entries of other kinds and are not counted."""
    if b[:4] != b"\xca\xfe\xba\xbe":
        raise ValueError("not a class file")
    n = (b[8] << 8) | b[9]
    utf, strs, meths, nat, cls = {}, [], [], {}, {}
    i, p = 1, 10
    while i < n:
        t = b[p]
        if t == 1:
            ln = (b[p + 1] << 8) | b[p + 2]
            utf[i] = bytes(b[p + 3:p + 3 + ln])
            p += 3 + ln
        elif t in (3, 4):
            p += 5
        elif t in (5, 6):
            p += 9
            i += 1
        elif t in (7, 8, 16, 19, 20):
            ref = (b[p + 1] << 8) | b[p + 2]
            if t == 8:
                strs.append(ref)
            elif t == 7:
                cls[i] = ref
            p += 3
        elif t in (9, 10, 11, 12, 17, 18):
            a, c = (b[p + 1] << 8) | b[p + 2], (b[p + 3] << 8) | b[p + 4]
            if t in (10, 11):
                meths.append((a, c))
            elif t == 12:
                nat[i] = a
            p += 5
        elif t == 15:
            p += 4
        else:
            raise ValueError("bad constant pool tag %d" % t)
        i += 1
    strings = {utf[r].decode("utf-8", "replace") for r in strs if r in utf}
    uses = any(utf.get(cls.get(a), b"") == RESOURCES_CLASS and utf.get(nat.get(c), b"") == b"getIdentifier" for a, c in meths)
    return strings, uses


def scan_jars(jars):
    """-> (String constants of every class in the jars, does any call Resources.getIdentifier?)"""
    strings, uses = set(), False
    for j in jars:
        try:
            with zipfile.ZipFile(j) as z:
                for n in z.namelist():
                    if not n.endswith(".class") or n.startswith("META-INF/") or n.endswith("module-info.class"):
                        continue
                    try:
                        s, u = scan_class(z.read(n))
                    except (ValueError, IndexError):
                        continue
                    strings |= s
                    uses = uses or u
        except (OSError, zipfile.BadZipFile):
            continue
    return strings, uses


def guard(strings, uses, table, keep):
    """Collapsing renames every resource not in `keep`.  -> (errors, warnings).  Nothing when no class calls Resources.getIdentifier.  When one does: an ERROR for
    each resource whose name is also a String constant in the code (a lookup by name, most likely; keeping a name costs a few bytes), a WARNING for names built at run time."""
    if not uses:
        return [], []
    kept = set(keep)
    hits = sorted(n for n in table_names(table) if n not in kept and n.split("/", 1)[1] in strings)
    if hits:
        shown = ", ".join(hits[:15]) + (" ... and %d more" % (len(hits) - 15) if len(hits) > 15 else "")
        return ['the code calls Resources.getIdentifier and also holds the name of %d resource(s) that collapsing would rename: %s.  Add the ones it looks up to "keep" '
                '(a name costs a few bytes), or leave "collapseNames" off.  A match can be a coincidence: a name that is only text in the code is safe to keep too' % (len(hits), shown)], []
    return [], ["the code calls Resources.getIdentifier: a name it builds at run time cannot be checked here, and a resource that is not in \"keep\" will resolve to 0 after collapsing"]
