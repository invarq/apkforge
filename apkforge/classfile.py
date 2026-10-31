"""Class-file and DEX readers (standard library only) and the API guard.

The guard exists because `javac --release 17 -cp android.jar` silently ACCEPTS JDK-only APIs
(java.net.http.HttpClient compiled fine), which then crash on a device. Every field/method/class reference in the
app's own bytecode is resolved against android.jar, lazily, so no index has to be built or cached.
"""
import struct
import zipfile

# bootstrap owners that D8 desugars away (lambdas, string concat, records): not real runtime references
D8_DESUGARED_OWNERS = {
    "java/lang/invoke/LambdaMetafactory", "java/lang/invoke/StringConcatFactory", "java/lang/runtime/ObjectMethods",
    "java/lang/invoke/MethodHandles", "java/lang/invoke/MethodHandles$Lookup", "java/lang/invoke/MethodType",
    "java/lang/invoke/CallSite", "java/lang/invoke/MethodHandle", "java/lang/invoke/VarHandle",
    "java/lang/runtime/SwitchBootstraps", "java/lang/Record", "java/lang/Enum$EnumDesc"}


class ClassInfo:
    __slots__ = ("name", "super", "interfaces", "access", "members", "refs", "classes")


def _u2(b, p):
    return (b[p] << 8) | b[p + 1]


def parse_class(b, want_refs=True):
    """Constant pool + field/method tables. refs: {(owner, name, desc)} for Field/Method/InterfaceMethod refs."""
    if b[:4] != b"\xca\xfe\xba\xbe":
        raise ValueError("not a class file")
    n = _u2(b, 8)
    cp = [None] * n
    i, p = 1, 10
    while i < n:
        t = b[p]
        if t == 1:
            ln = _u2(b, p + 1)
            cp[i] = (1, b[p + 3:p + 3 + ln])
            p += 3 + ln
        elif t in (3, 4):
            p += 5
        elif t in (5, 6):
            p += 9
            i += 1
        elif t in (7, 8, 16, 19, 20):
            cp[i] = (t, _u2(b, p + 1))
            p += 3
        elif t in (9, 10, 11, 12, 17, 18):
            cp[i] = (t, _u2(b, p + 1), _u2(b, p + 3))
            p += 5
        elif t == 15:
            p += 4
        else:
            raise ValueError("bad constant pool tag %d" % t)
        i += 1

    def utf(ix):
        return cp[ix][1].decode("utf-8", "replace")

    def cname(ix):
        return utf(cp[ix][1]) if ix else None

    c = ClassInfo()
    c.access = _u2(b, p)
    c.name = cname(_u2(b, p + 2))
    c.super = cname(_u2(b, p + 4))
    ni = _u2(b, p + 6)
    p += 8
    c.interfaces = tuple(cname(_u2(b, p + 2 * k)) for k in range(ni))
    p += 2 * ni
    members = set()
    for kind in (0, 1):                       # fields, then methods
        cnt = _u2(b, p)
        p += 2
        for _ in range(cnt):
            name, desc, na = utf(_u2(b, p + 2)), utf(_u2(b, p + 4)), _u2(b, p + 6)
            members.add((name + ":" + desc) if kind == 0 else (name + desc))
            p += 8
            for _a in range(na):
                p += 6 + ((b[p + 2] << 24) | (b[p + 3] << 16) | (b[p + 4] << 8) | b[p + 5])
    c.members = members
    c.refs, c.classes = set(), set()
    if want_refs:
        for e in cp:
            if not e:
                continue
            if e[0] == 7:
                c.classes.add(utf(e[1]))
            elif e[0] in (9, 10, 11):
                nat = cp[e[2]]
                c.refs.add((cname(e[1]), utf(nat[1]), utf(nat[2]), e[0] == 9))
    return c


def jar_class_names(path):
    with zipfile.ZipFile(path) as z:
        return {n[:-6] for n in z.namelist() if n.endswith(".class") and not n.startswith("META-INF/")}


class Api:
    """android.jar as an API oracle. Lazy: a class is parsed the first time something refers to it."""

    def __init__(self, jar):
        self.z = zipfile.ZipFile(jar)
        self.names = {n[:-6] for n in self.z.namelist() if n.endswith(".class")}
        self.memo = {}

    def cls(self, name):
        if name not in self.names:
            return None
        c = self.memo.get(name)
        if c is None:
            c = self.memo[name] = parse_class(self.z.read(name + ".class"), want_refs=False)
        return c

    def has_member(self, owner, key):
        """True / False, or None when the hierarchy leaves android.jar (cannot decide)."""
        seen, stack = set(), [owner]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            c = self.cls(n)
            if c is None:
                return None
            if key in c.members:
                return True
            if c.super:
                stack.append(c.super)
            stack.extend(c.interfaces)
        return False


def check_api(program, api, others=(), min_api=None):
    """program: {class name: bytes}; others: names of classes that come from dependencies.
    -> (errors, newer): errors = references android.jar does not have; newer = present in android.jar but not in min_api's."""
    own = set(program)
    errors, newer, seen = [], [], set()
    for name, data in program.items():
        ci = parse_class(data)
        refs = [(o, None, None, False) for o in ci.classes] + list(ci.refs)
        for owner, mname, desc, is_field in refs:
            if not owner or owner[0] == "[" or owner in own or owner in others or owner in D8_DESUGARED_OWNERS:
                continue
            key = (owner, mname, desc)
            if key in seen:
                continue
            seen.add(key)
            what = owner.replace("/", ".") if mname is None else "%s.%s" % (owner.replace("/", "."), mname)
            if api.cls(owner) is None:
                if mname is None or owner not in api.names:
                    errors.append("%s: class %s is not in android.jar (a JDK-only API?)" % (name.replace("/", "."), owner.replace("/", ".")))
                continue
            if mname is None:
                if min_api is not None and min_api.cls(owner) is None:
                    newer.append("%s: class %s" % (name.replace("/", "."), what))
                continue
            k = (mname + ":" + desc) if is_field else (mname + desc)
            if api.has_member(owner, k) is False:
                errors.append("%s: %s%s is not in android.jar (a JDK-only API?)" % (name.replace("/", "."), what, "" if is_field else desc))
            elif min_api is not None and min_api.has_member(owner, k) is False and min_api.cls(owner) is not None:
                newer.append("%s: %s%s" % (name.replace("/", "."), what, "" if is_field else desc))
    return sorted(set(errors)), sorted(set(newer))


# ----------------------------------------------------------------------------------------------- DEX
def _uleb(b, p):
    r = s = 0
    while True:
        x = b[p]
        p += 1
        r |= (x & 0x7F) << s
        if not x & 0x80:
            return r, p
        s += 7


def dex_classes(b):
    """Class descriptors defined in a .dex file ('Lcom/x/Y;')."""
    if b[:4] != b"dex\n":
        raise ValueError("not a dex file")
    ssz, soff, tsz, toff = struct.unpack_from("<IIII", b, 0x38)
    csz, coff = struct.unpack_from("<II", b, 0x60)

    def string(i):
        off = struct.unpack_from("<I", b, soff + 4 * i)[0]
        _n, q = _uleb(b, off)
        return b[q:b.index(b"\0", q)].decode("utf-8", "replace")

    out = []
    for k in range(csz):
        tidx = struct.unpack_from("<I", b, coff + 32 * k)[0]
        out.append(string(struct.unpack_from("<I", b, toff + 4 * tidx)[0]))
    return out


def dex_version(b):
    return b[4:7].decode("ascii", "replace")
