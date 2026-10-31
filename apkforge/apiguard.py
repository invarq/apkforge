"""Min-API guard: platform APIs newer than minSdk that nothing protects.

`javac -cp android.jar` accepts every API in the compile-time jar. A device older than the API throws NoSuchMethodError or
NoClassDefFoundError when the code runs. This module reads the app's own class files and reports every reference to a platform
class, method or field that android.jar for minSdk does not have, unless the code around the reference is protected.

Protection that is recognised (Lint's vocabulary):
  * Build.VERSION.SDK_INT compared with a constant (>=, >, ==, <, <=, != and the operands swapped). The analysis follows the
    bytecode's control flow, so && and || come out right: where two paths meet it keeps the WEAKER guarantee.
  * a local variable that holds SDK_INT
  * @RequiresApi(n) / @TargetApi(n) on the method or on the class (and the classes it is nested in)
  * @SuppressLint("NewApi") (an explicit opt-out)
  * lambdas: the synthetic body inherits the weakest guard of the places that create it
  * methods D8 backports at minSdk (R8's BackportedMethodList) are not reported
  * calls TO an app method annotated @RequiresApi(n) are checked like a platform API of level n

Not recognised, so REPORTED (the safe direction): a guard kept in a boolean local, field or helper method; a class created inside a
guard (anonymous / Kotlin lambda classes: the guard is not visible across classes); reflection; AndroidX BuildCompat helpers.
Not checked: references the compile-time jar itself lacks (the separate apiCheck reports those), inlined constants, API use inside dependencies.

Soundness over precision: if the analysis of a method fails for any reason, every reference in it is judged at the method's base level.
"""
import struct
import zipfile

from . import classfile

VERSION = "1"
INF = 1 << 20

SDK_OWNER, SDK_NAME = "android/os/Build$VERSION", "SDK_INT"
REQ_ANNOS = {"Landroidx/annotation/RequiresApi;": ("api", "value"), "Landroid/support/annotation/RequiresApi;": ("api", "value")}
TGT_ANNOS = {"Landroid/annotation/TargetApi;": ("value",)}
SUPPRESS_ANNO = "Landroid/annotation/SuppressLint;"
SUPPRESS_IDS = ("NewApi", "all")
ANNO_ATTRS = ("RuntimeVisibleAnnotations", "RuntimeInvisibleAnnotations")
ACC_SYNTHETIC = 0x1000


class Degrade(Exception):
    """the analysis of one method cannot be trusted; the caller falls back to the pessimistic reading"""


class Unavailable(Exception):
    """an android.jar for some API level cannot be had (offline, network)"""


# ---------------------------------------------------------------------------------------------------- class file reader
def _u2(b, p):
    return (b[p] << 8) | b[p + 1]


def _u4(b, p):
    return (b[p] << 24) | (b[p + 1] << 16) | (b[p + 2] << 8) | b[p + 3]


class Meth:
    __slots__ = ("name", "desc", "access", "code", "req", "tgt", "sup")


class Cls:
    __slots__ = ("name", "super", "interfaces", "access", "meths", "members", "req", "tgt", "sup", "cp", "bsm")

    def utf(self, ix):
        return self.cp[ix][1].decode("utf-8", "replace")

    def cname(self, ix):
        return self.utf(self.cp[ix][1]) if ix else None

    def member(self, ix):
        e = self.cp[ix]
        nat = self.cp[e[2]]
        return (self.cname(e[1]), self.utf(nat[1]), self.utf(nat[2]), e[0] == 9)


def _elem(c, b, p):
    tag = chr(b[p])
    p += 1
    if tag in "BCISZ":
        e = c.cp[_u2(b, p)]
        return (e[1] if e is not None and e[0] == 3 else None), p + 2
    if tag == "s":
        return c.utf(_u2(b, p)), p + 2
    if tag in "DFJ":
        return None, p + 2
    if tag == "e":
        return None, p + 4
    if tag == "c":
        return None, p + 2
    if tag == "@":
        return None, _anno(c, b, p)[2]
    if tag == "[":
        n = _u2(b, p)
        p += 2
        vs = []
        for _ in range(n):
            v, p = _elem(c, b, p)
            vs.append(v)
        return vs, p
    raise ValueError("bad annotation element tag %r" % tag)


def _anno(c, b, p):
    """-> (type descriptor, {element: value}, new position)"""
    t = c.utf(_u2(b, p))
    n = _u2(b, p + 2)
    p += 4
    vals = {}
    for _ in range(n):
        name = c.utf(_u2(b, p))
        v, p = _elem(c, b, p + 2)
        vals[name] = v
    return t, vals, p


def _apply_annos(obj, c, b, q):
    n = _u2(b, q)
    q += 2
    for _ in range(n):
        t, vals, q = _anno(c, b, q)
        keys = REQ_ANNOS.get(t)
        if keys:
            for k in keys:
                if isinstance(vals.get(k), int):
                    obj.req = max(obj.req, vals[k])
                    break
            continue
        keys = TGT_ANNOS.get(t)
        if keys:
            for k in keys:
                if isinstance(vals.get(k), int):
                    obj.tgt = max(obj.tgt, vals[k])
                    break
            continue
        if t == SUPPRESS_ANNO:
            v = vals.get("value")
            if any(x in SUPPRESS_IDS for x in (v if isinstance(v, list) else [v])):
                obj.sup = True


def _read_code(b, q):
    ms, ml, cl = _u2(b, q), _u2(b, q + 2), _u4(b, q + 4)
    code = b[q + 8:q + 8 + cl]
    r = q + 8 + cl
    nh = _u2(b, r)
    r += 2
    hs = [(_u2(b, r + 8 * k), _u2(b, r + 8 * k + 2), _u2(b, r + 8 * k + 4), _u2(b, r + 8 * k + 6)) for k in range(nh)]
    return (ms, ml, code, hs)


def read(b):
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
        elif t == 3:
            cp[i] = (3, struct.unpack_from(">i", b, p + 1)[0])
            p += 5
        elif t == 4:
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
            cp[i] = (15, b[p + 1], _u2(b, p + 2))
            p += 4
        else:
            raise ValueError("bad constant pool tag %d" % t)
        i += 1
    c = Cls()
    c.cp = cp
    c.access = _u2(b, p)
    c.name = c.cname(_u2(b, p + 2))
    c.super = c.cname(_u2(b, p + 4))
    ni = _u2(b, p + 6)
    p += 8
    c.interfaces = tuple(c.cname(_u2(b, p + 2 * k)) for k in range(ni))
    p += 2 * ni
    c.members, c.meths, c.bsm = set(), [], []
    c.req = c.tgt = 0
    c.sup = False
    for kind in (0, 1):                                   # fields, then methods
        cnt = _u2(b, p)
        p += 2
        for _ in range(cnt):
            acc, name, desc, na = _u2(b, p), c.utf(_u2(b, p + 2)), c.utf(_u2(b, p + 4)), _u2(b, p + 6)
            p += 8
            m = None
            if kind == 1:
                m = Meth()
                m.name, m.desc, m.access, m.code, m.req, m.tgt, m.sup = name, desc, acc, None, 0, 0, False
                c.meths.append(m)
            c.members.add((name + ":" + desc) if kind == 0 else (name + desc))
            for _a in range(na):
                an, ln, q = c.utf(_u2(b, p)), _u4(b, p + 2), p + 6
                if m is not None:
                    if an == "Code":
                        m.code = _read_code(b, q)
                    elif an in ANNO_ATTRS:
                        _apply_annos(m, c, b, q)
                p = q + ln
    na = _u2(b, p)                                        # class attributes
    p += 2
    for _a in range(na):
        an, ln, q = c.utf(_u2(b, p)), _u4(b, p + 2), p + 6
        if an == "BootstrapMethods":
            k = _u2(b, q)
            r = q + 2
            for _ in range(k):
                ref, nargs = _u2(b, r), _u2(b, r + 2)
                c.bsm.append((ref, [_u2(b, r + 4 + 2 * j) for j in range(nargs)]))
                r += 4 + 2 * nargs
        elif an in ANNO_ATTRS:
            _apply_annos(c, c, b, q)
        p = q + ln
    return c


# ---------------------------------------------------------------------------------------------------- opcode tables
# instruction length in bytes; 0 = unsupported (jsr/ret and the unused opcodes), -1 = variable (tableswitch, lookupswitch, wide)
LEN = [0] * 256
for _r in ((0, 15), (26, 131), (133, 152), (172, 177), (190, 191), (194, 195)):
    for _o in range(_r[0], _r[1] + 1):
        LEN[_o] = 1
for _o in (16, 18, 21, 22, 23, 24, 25, 54, 55, 56, 57, 58, 188):
    LEN[_o] = 2
for _o in [17, 19, 20, 132, 187, 189, 192, 193, 198, 199] + list(range(153, 168)) + list(range(178, 185)):
    LEN[_o] = 3
LEN[197] = 4
for _o in (185, 186, 200):
    LEN[_o] = 5
LEN[170] = LEN[171] = LEN[196] = -1
LEN[168] = LEN[169] = LEN[201] = 0                        # jsr, ret, jsr_w: only in class files older than Java 6

# pure stack effects (words popped, words pushed); every pushed value is OTHER. long and double take two words.
SIMPLE = {}
for _o in range(46, 54):                                  # array loads
    SIMPLE[_o] = (2, 2 if _o in (47, 49) else 1)
for _o in range(79, 87):                                  # array stores
    SIMPLE[_o] = (4 if _o in (80, 82) else 3, 0)
SIMPLE[87], SIMPLE[88] = (1, 0), (2, 0)                   # pop, pop2
for _o in range(96, 116):                                 # add sub mul div rem: i l f d
    _w = 2 if (_o - 96) % 4 in (1, 3) else 1
    SIMPLE[_o] = (2 * _w, _w)
for _o in range(116, 120):                                # neg
    _w = 2 if (_o - 116) in (1, 3) else 1
    SIMPLE[_o] = (_w, _w)
for _o in (120, 122, 124):                                # int shifts
    SIMPLE[_o] = (2, 1)
for _o in (121, 123, 125):                                # long shifts (long + int count)
    SIMPLE[_o] = (3, 2)
for _o in (126, 128, 130):
    SIMPLE[_o] = (2, 1)
for _o in (127, 129, 131):
    SIMPLE[_o] = (4, 2)
SIMPLE.update({133: (1, 2), 134: (1, 1), 135: (1, 2), 136: (2, 1), 137: (2, 1), 138: (2, 2), 139: (1, 1), 140: (1, 2), 141: (1, 2),
               142: (2, 1), 143: (2, 2), 144: (2, 1), 145: (1, 1), 146: (1, 1), 147: (1, 1),
               148: (4, 1), 149: (2, 1), 150: (2, 1), 151: (4, 1), 152: (4, 1),
               187: (0, 1), 188: (1, 1), 189: (1, 1), 190: (1, 1), 192: (1, 1), 193: (1, 1), 194: (1, 0), 195: (1, 0)})

CMP = {159: "eq", 160: "ne", 161: "lt", 162: "ge", 163: "gt", 164: "le"}
NEG = {"eq": "ne", "ne": "eq", "lt": "ge", "ge": "lt", "gt": "le", "le": "gt"}
MIRROR = {"eq": "eq", "ne": "ne", "lt": "gt", "gt": "lt", "le": "ge", "ge": "le"}
_DESC = {}


def desc_words(desc):
    """-> (words taken by the arguments, words left by the result) of a method descriptor"""
    r = _DESC.get(desc)
    if r is not None:
        return r
    i, n = 1, 0
    while desc[i] != ")":
        c = desc[i]
        if c in "JD":
            n += 2
            i += 1
        elif c == "L":
            n += 1
            i = desc.index(";", i) + 1
        elif c == "[":
            while desc[i] == "[":
                i += 1
            i = desc.index(";", i) + 1 if desc[i] == "L" else i + 1
            n += 1
        else:
            n += 1
            i += 1
    ret = desc[i + 1]
    r = _DESC[desc] = (n, 0 if ret == "V" else 2 if ret in "JD" else 1)
    return r


def decode(code):
    """-> (instruction start offsets, {offset: length}); raises Degrade for anything that is not plain Java 6+ bytecode"""
    pcs, ln = [], {}
    p, n = 0, len(code)
    while p < n:
        op = code[p]
        length = LEN[op]
        try:
            if op == 170:
                q = (p + 4) & ~3
                lo, hi = struct.unpack_from(">ii", code, q + 4)
                length = q + 12 + 4 * (hi - lo + 1) - p
            elif op == 171:
                q = (p + 4) & ~3
                length = q + 8 + 8 * struct.unpack_from(">i", code, q + 4)[0] - p
            elif op == 196:
                length = 6 if code[p + 1] == 132 else 4
        except (IndexError, struct.error):
            raise Degrade("truncated instruction at %d" % p)
        if length <= 0:
            raise Degrade("unsupported opcode %d at %d" % (op, p))
        pcs.append(p)
        ln[p] = length
        p += length
    if p != n:
        raise Degrade("instruction stream overruns the code")
    return pcs, ln


def _s2(code, p):
    v = (code[p] << 8) | code[p + 1]
    return v - 65536 if v > 32767 else v


def _s4(code, p):
    return struct.unpack_from(">i", code, p)[0]


def _popn(stack, n):
    if len(stack) < n:
        raise Degrade("stack underflow")
    if n:
        del stack[-n:]


def _refine(lb, rel):
    """lb after learning `SDK_INT <cmp> c` is true"""
    if rel is None:
        return lb
    cmp_, c = rel
    if cmp_ in ("ge", "eq"):
        return max(lb, c)
    if cmp_ == "gt":
        return max(lb, c + 1)
    return lb


def _join(old, new):
    s1, l1, b1 = old
    s2, l2, b2 = new
    if len(s1) != len(s2):
        raise Degrade("inconsistent stack depth at a join")
    return (tuple(x if x == y else None for x, y in zip(s1, s2)),
            tuple(x if x == y else None for x, y in zip(l1, l2)),
            min(b1, b2))                                  # JOIN-LB: at a merge only the weaker guarantee survives


def dataflow(c, code, ms, ml, handlers, pcs, ln, start):
    """Abstract interpretation. State before an instruction = (stack, locals, lb). Values: 'S' = SDK_INT, an int = that constant, None = anything.
    -> {pc: state}; unreached instructions have no entry."""
    known = set(pcs)
    cp = c.cp
    st = {0: ((), (None,) * ml, start)}
    work = [0]
    mem = {}

    def member(ix):
        r = mem.get(ix)
        if r is None:
            r = mem[ix] = c.member(ix)
        return r

    def merge(pc, new):
        if pc not in known:
            raise Degrade("jump into the middle of an instruction or off the end")
        old = st.get(pc)
        if old is None:
            st[pc] = new
            work.append(pc)
        else:
            j = _join(old, new)
            if j != old:
                st[pc] = j
                work.append(pc)

    while work:
        pc = work.pop()
        stack0, loc0, lb = st[pc]
        stack, loc = list(stack0), list(loc0)
        op = code[pc]
        nxt = pc + ln[pc]
        outs = None                                       # None = fall through to nxt, lb unchanged
        try:
            if op == 0:
                pass
            elif op == 1 or 11 <= op <= 13:
                stack.append(None)
            elif 2 <= op <= 8:
                stack.append(op - 3)
            elif op in (9, 10, 14, 15, 20):
                stack += [None, None]
            elif op == 16:
                stack.append(code[pc + 1] - 256 if code[pc + 1] > 127 else code[pc + 1])
            elif op == 17:
                stack.append(_s2(code, pc + 1))
            elif op in (18, 19):
                e = cp[code[pc + 1] if op == 18 else _u2(code, pc + 1)]
                stack.append(e[1] if e is not None and e[0] == 3 else None)
            elif 21 <= op <= 45:                          # loads
                if op <= 25:
                    k, idx = op - 21, code[pc + 1]
                else:
                    k, idx = (op - 26) // 4, (op - 26) % 4
                if k == 0:
                    stack.append(loc[idx])
                elif k in (1, 3):
                    if idx + 1 >= len(loc):
                        raise Degrade("local index out of range")
                    stack += [None, None]
                else:
                    stack.append(None)
            elif 54 <= op <= 78:                          # stores
                if op <= 58:
                    k, idx = op - 54, code[pc + 1]
                else:
                    k, idx = (op - 59) // 4, (op - 59) % 4
                if k == 0:
                    if not stack:
                        raise Degrade("stack underflow")
                    loc[idx] = stack.pop()
                elif k in (1, 3):
                    _popn(stack, 2)
                    loc[idx] = loc[idx + 1] = None
                else:
                    _popn(stack, 1)
                    loc[idx] = None
            elif op in SIMPLE:
                pops, pushes = SIMPLE[op]
                _popn(stack, pops)
                stack += [None] * pushes
            elif 89 <= op <= 95:                          # dup family and swap, in words
                if op == 89:
                    a = stack[-1]
                    stack.append(a)
                elif op == 90:
                    a, b = stack.pop(), stack.pop()
                    stack += [a, b, a]
                elif op == 91:
                    a, b, cc = stack.pop(), stack.pop(), stack.pop()
                    stack += [a, cc, b, a]
                elif op == 92:
                    a, b = stack.pop(), stack.pop()
                    stack += [b, a, b, a]
                elif op == 93:
                    a, b, cc = stack.pop(), stack.pop(), stack.pop()
                    stack += [b, a, cc, b, a]
                elif op == 94:
                    a, b, cc, d = stack.pop(), stack.pop(), stack.pop(), stack.pop()
                    stack += [b, a, d, cc, b, a]
                else:
                    a, b = stack.pop(), stack.pop()
                    stack += [a, b]
            elif op == 132:
                loc[code[pc + 1]] = None
            elif 153 <= op <= 166 or op in (198, 199):    # conditional branches
                tgt = pc + _s2(code, pc + 1)
                if op in CMP:
                    if len(stack) < 2:
                        raise Degrade("stack underflow")
                    b, a = stack.pop(), stack.pop()
                    rel = None
                    if a == "S" and isinstance(b, int):
                        rel = (CMP[op], b)
                    elif b == "S" and isinstance(a, int):
                        rel = (MIRROR[CMP[op]], a)
                    if rel is None:
                        outs = [(tgt, lb), (nxt, lb)]
                    else:
                        taken = _refine(lb, rel)                              # EDGE-TAKEN
                        fall = _refine(lb, (NEG[rel[0]], rel[1]))             # EDGE-FALL
                        outs = [(tgt, taken), (nxt, fall)]
                else:
                    _popn(stack, 2 if op in (165, 166) else 1)
                    outs = [(tgt, lb), (nxt, lb)]
            elif op == 167:
                outs = [(pc + _s2(code, pc + 1), lb)]
            elif op == 200:
                outs = [(pc + _s4(code, pc + 1), lb)]
            elif 172 <= op <= 177 or op == 191:
                outs = []
            elif op == 170 or op == 171:
                _popn(stack, 1)
                q = (pc + 4) & ~3
                tg = [pc + _s4(code, q)]
                if op == 170:
                    lo, hi = _s4(code, q + 4), _s4(code, q + 8)
                    tg += [pc + _s4(code, q + 12 + 4 * k) for k in range(hi - lo + 1)]
                else:
                    tg += [pc + _s4(code, q + 8 + 8 * k + 4) for k in range(_s4(code, q + 4))]
                outs = [(t, lb) for t in tg]
            elif 178 <= op <= 181:
                o, n, d, _f = member(_u2(code, pc + 1))
                w = 2 if d[0] in "JD" else 1
                if op == 178:
                    stack += ["S"] if (w == 1 and o == SDK_OWNER and n == SDK_NAME) else [None] * w
                elif op == 179:
                    _popn(stack, w)
                elif op == 180:
                    _popn(stack, 1)
                    stack += [None] * w
                else:
                    _popn(stack, 1 + w)
            elif 182 <= op <= 185:
                d = member(_u2(code, pc + 1))[2]
                pa, pr = desc_words(d)
                _popn(stack, pa + (0 if op == 184 else 1))
                stack += [None] * pr
            elif op == 186:
                e = cp[_u2(code, pc + 1)]
                d = c.utf(cp[e[2]][2])
                pa, pr = desc_words(d)
                _popn(stack, pa)
                stack += [None] * pr
            elif op == 196:                               # wide
                op2 = code[pc + 1]
                idx = _u2(code, pc + 2)
                if op2 == 132:
                    loc[idx] = None
                elif 21 <= op2 <= 25:
                    k = op2 - 21
                    if k == 0:
                        stack.append(loc[idx])
                    elif k in (1, 3):
                        if idx + 1 >= len(loc):
                            raise Degrade("local index out of range")
                        stack += [None, None]
                    else:
                        stack.append(None)
                elif 54 <= op2 <= 58:
                    k = op2 - 54
                    if k == 0:
                        if not stack:
                            raise Degrade("stack underflow")
                        loc[idx] = stack.pop()
                    elif k in (1, 3):
                        _popn(stack, 2)
                        loc[idx] = loc[idx + 1] = None
                    else:
                        _popn(stack, 1)
                        loc[idx] = None
                else:
                    raise Degrade("unsupported wide opcode %d" % op2)
            elif op == 197:
                _popn(stack, code[pc + 3])
                stack.append(None)
            else:
                raise Degrade("unsupported opcode %d at %d" % (op, pc))
        except (IndexError, struct.error, TypeError, KeyError, ValueError):
            raise Degrade("malformed code at %d (opcode %d)" % (pc, op))
        if len(stack) > ms:
            raise Degrade("stack deeper than max_stack at %d" % pc)
        if outs is None:
            outs = [(nxt, lb)]
        for t, l2 in outs:
            merge(t, (tuple(stack), tuple(loc), l2))
        for hs, he, hp, _ct in handlers:
            if hs <= pc < he:
                merge(hp, ((None,), loc0, lb))                # HANDLER: entered from anywhere in its range; merge() keeps the MIN over all of them
    return st


# ---------------------------------------------------------------------------------------------------- references of a method
def _elem_class(name):
    """class name of a reference, looking through array types; None for primitive arrays"""
    if name and name[0] == "[":
        name = name.lstrip("[")
        return name[1:-1] if name[:1] == "L" else None
    return name


class Analysis:
    __slots__ = ("refs", "sites", "degraded")


def collect(c, code, pcs, st, start, handlers):
    """-> (refs, sites): refs = [(kind, owner, name, desc, is_field, lb)] with kind 'm' (member) or 'c' (class);
    sites = [((owner, name, desc), lb)] for invokedynamic targets that are app methods"""
    refs, sites = [], []
    cp = c.cp
    for pc in pcs:
        op = code[pc]
        if not (178 <= op <= 186 or op in (187, 189, 192, 193, 197, 18, 19)):
            continue
        s = st.get(pc)
        lb = s[2] if s is not None else start
        if op in (18, 19):
            e = cp[code[pc + 1] if op == 18 else _u2(code, pc + 1)]
            if e is not None and e[0] == 7:
                refs.append(("c", c.utf(e[1]), None, None, False, lb))
            elif e is not None and e[0] == 15 and cp[e[2]] is not None and cp[e[2]][0] in (9, 10, 11):
                o, n, d, f = c.member(e[2])
                refs.append(("m", o, n, d, f, lb))
        elif op in (187, 189, 192, 193, 197):
            o = _elem_class(c.cname(_u2(code, pc + 1)))
            if o:
                refs.append(("c", o, None, None, False, lb))
        elif op == 186:
            e = cp[_u2(code, pc + 1)]
            ref, args = c.bsm[e[1]]
            for a in args:
                h = cp[a]
                if h is not None and h[0] == 15 and cp[h[2]] is not None and cp[h[2]][0] in (9, 10, 11):
                    o, n, d, f = c.member(h[2])
                    sites.append(((o, n, d), lb))
                    refs.append(("m", o, n, d, f, lb))
        else:
            o, n, d, f = c.member(_u2(code, pc + 1))
            o = _elem_class(o)
            if o:
                refs.append(("m", o, n, d, f, lb))
    for hs, he, hp, ct in handlers:
        if ct:
            s = st.get(hp)
            refs.append(("c", c.cname(ct), None, None, False, s[2] if s is not None else start))
    return refs, sites


def pool_refs(c, start):
    """pessimistic fallback: every class, field and method reference in the constant pool, judged at the base level"""
    refs = []
    for ix, e in enumerate(c.cp):
        if e is None:
            continue
        if e[0] == 7:
            o = _elem_class(c.utf(e[1]))
            if o:
                refs.append(("c", o, None, None, False, start))
        elif e[0] in (9, 10, 11):
            o, n, d, f = c.member(ix)
            o = _elem_class(o)
            if o:
                refs.append(("m", o, n, d, f, start))
    return refs


def analyse(c, m, start):
    a = Analysis()
    ms, ml, code, handlers = m.code
    a.degraded, st, pcs = None, {}, None
    try:
        pcs, ln = decode(code)
        st = dataflow(c, code, ms, ml, handlers, pcs, ln, start)
    except Degrade as e:
        a.degraded = str(e)
    if pcs is None:
        a.refs, a.sites = pool_refs(c, start), []
    else:
        a.refs, a.sites = collect(c, code, pcs, st if a.degraded is None else {}, start, handlers)
    return a


# ---------------------------------------------------------------------------------------------------- judging
class JarIndex:
    """lazy view of the dependencies' class files (for resolving a call on an app class up through a dependency's superclass)"""

    def __init__(self, jars):
        self.jars = list(jars)
        self._map = None
        self._zip = {}
        self._memo = {}

    def _load(self):
        self._map = {}
        for j in self.jars:
            try:
                with zipfile.ZipFile(j) as z:
                    for n in z.namelist():
                        if n.endswith(".class") and not n.startswith("META-INF/"):
                            self._map.setdefault(n[:-6], j)
            except (OSError, zipfile.BadZipFile):
                continue

    def get(self, name):
        if name in self._memo:
            return self._memo[name]
        if self._map is None:
            self._load()
        j = self._map.get(name)
        ci = None
        if j is not None:
            try:
                z = self._zip.get(j)
                if z is None:
                    z = self._zip[j] = zipfile.ZipFile(j)
                ci = classfile.parse_class(z.read(name + ".class"), want_refs=False)
            except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                ci = None
        self._memo[name] = ci
        return ci


_PRIM = {"B": "byte", "C": "char", "D": "double", "F": "float", "I": "int", "J": "long", "S": "short", "Z": "boolean", "V": "void"}


def pretty_params(desc):
    out, i = [], 1
    while desc[i] != ")":
        dims = 0
        while desc[i] == "[":
            dims += 1
            i += 1
        if desc[i] == "L":
            j = desc.index(";", i)
            t = desc[i + 1:j].replace("/", ".").rsplit(".", 1)[-1]
            i = j + 1
        else:
            t = _PRIM[desc[i]]
            i += 1
        out.append(t + "[]" * dims)
    return "(" + ", ".join(out) + ")"


class Result:
    def __init__(self):
        self.findings = []        # (class, method, what, status, level); status 'newer' | 'unverified'
        self.skipped = None       # reason the whole check did not run
        self.degraded = []        # "class.method: why" for methods judged pessimistically
        self.unreadable = []
        self.skipped_levels = {}  # level -> why its android.jar could not be had
        self.methods = 0
        self.refs = 0


class _Guard:
    def __init__(self, own, api, min_level, compile_level, jar_for, deps, backports, others, res):
        self.own, self.api, self.min, self.compile = own, api, min_level, compile_level
        self.jar_for, self.deps, self.backports, self.others, self.res = jar_for, deps, backports, others, res
        self._jars, self._res, self._bp, self._chain = {}, {}, None, {}
        self.own_meth = {(c.name, m.name + m.desc): m for c in own.values() for m in c.meths}

    # ------------------------------------------------------------ annotations
    def chain(self, name):
        """(req, tgt, sup) of a class and the classes it is nested in"""
        r = self._chain.get(name)
        if r is None:
            req, tgt, sup = 0, 0, False
            parts = name.split("$")
            for k in range(1, len(parts) + 1):
                c = self.own.get("$".join(parts[:k]))
                if c is not None:
                    req, tgt, sup = max(req, c.req), max(tgt, c.tgt), sup or c.sup
            r = self._chain[name] = (req, tgt, sup)
        return r

    def base(self, c, m):
        req, tgt, sup = self.chain(c.name)
        if sup or m.sup:
            return INF
        return max(self.min, req, tgt, m.req, m.tgt)

    # ------------------------------------------------------------ jars
    def jar(self, level):
        if level in self._jars:
            return self._jars[level]
        try:
            j = self.jar_for(level)
        except Unavailable as e:
            j = None
            self.res.skipped_levels[level] = str(e)
        self._jars[level] = j
        return j

    def avail(self, owner, key, level):
        """True / False / None (no android.jar for that level) : does the platform at `level` have the class (key None) or the member?"""
        if level >= self.compile:
            return True
        j = self.jar(level)
        if j is None:
            return None
        if key is None:
            return j.cls(owner) is not None
        return j.has_member(owner, key) is True

    def is_backport(self, owner, plat, key):
        if self._bp is None:
            try:
                self._bp = set(self.backports(self.min)) if self.backports else set()
            except Exception:
                self._bp = set()
        return (owner + "#" + key) in self._bp or (plat + "#" + key) in self._bp

    # ------------------------------------------------------------ resolution
    def resolve(self, owner, key):
        mk = (owner, key)
        r = self._res.get(mk)
        if r is None:
            r = self._res[mk] = self._resolve(owner, key)
        return r

    def _resolve(self, owner, key):
        seen, stack = set(), [owner]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            c = self.own.get(n)
            if c is not None:
                if key in c.members:
                    return ("own", n)
                sup = ([c.super] if c.super else []) + list(c.interfaces)
            elif n in self.others:
                ci = self.deps.get(n) if self.deps is not None else None
                if ci is None:
                    continue
                if key in ci.members:
                    return (None, None)
                sup = ([ci.super] if ci.super else []) + list(ci.interfaces)
            else:
                return ("plat", n)
            stack.extend(reversed(sup))                   # the superclass is searched before the interfaces
        return (None, None)

    # ------------------------------------------------------------ the verdict for one reference
    def newer(self, what_owner, key, L, member, plat):
        """None when fine at level L, else (status, level the jar was consulted at)"""
        if L >= self.compile:
            return None
        if self.avail(plat, key, self.min) is not False:
            return None
        if member and self.is_backport(what_owner, plat, key):
            return None
        if L <= self.min:
            return ("newer", self.min)
        ok = self.avail(plat, key, L)
        if ok is True:
            return None
        return ("newer", L) if ok is False else ("unverified", L)

    def judge(self, kind, owner, name, desc, is_field, L):
        """-> None | (status, level, extra text)"""
        if owner in D8_OWNERS:
            return None
        if kind == "c":
            c = self.own.get(owner)
            if c is not None:
                req = self.chain(owner)[0]
                return ("newer", req, "app class annotated @RequiresApi(%d)" % req) if req > L else None
            if owner in self.others or self.api.cls(owner) is None:
                return None
            r = self.newer(owner, None, L, False, owner)
            return None if r is None else (r[0], r[1], "")
        key = (name + ":" + desc) if is_field else (name + desc)
        who, tgt = self.resolve(owner, key)
        if who == "own":
            m = self.own_meth.get((tgt, key))
            if m is None:
                return None
            req = max(m.req, self.chain(tgt)[0])
            return ("newer", req, "app method annotated @RequiresApi(%d)" % req) if req > L else None
        if who != "plat" or self.api.has_member(tgt, key) is not True:
            return None
        r = self.newer(owner, key, L, not is_field, tgt)
        return None if r is None else (r[0], r[1], "")

    # ------------------------------------------------------------ driver
    def run(self):
        res, own = self.res, self.own
        jobs = [(c, m) for c in own.values() for m in c.meths if m.code is not None]
        res.methods = len(jobs)
        start = {(c.name, m.name, m.desc): self.base(c, m) for c, m in jobs}
        ana = {(c.name, m.name, m.desc): analyse(c, m, start[(c.name, m.name, m.desc)]) for c, m in jobs}
        meth = {(c.name, m.name, m.desc): (c, m) for c, m in jobs}
        for _ in range(40):                               # lambda bodies inherit the weakest guard of the places that create them
            site_min = {}
            for a in ana.values():
                for t, lb in a.sites:
                    site_min[t] = min(site_min.get(t, INF + 1), lb)
            changed = False
            for t, lb in site_min.items():
                cm = meth.get(t)
                if cm is None or not (cm[1].access & ACC_SYNTHETIC):
                    continue
                want = max(self.base(*cm), lb)
                if want != start[t]:
                    start[t] = want
                    ana[t] = analyse(cm[0], cm[1], want)
                    changed = True
            if not changed:
                break
        seen = set()
        for k, a in ana.items():
            c, m = meth[k]
            cname = c.name.replace("/", ".")
            if a.degraded:
                res.degraded.append("%s.%s: %s" % (cname, m.name, a.degraded))
            for kind, owner, name, desc, isf, lb in a.refs:
                res.refs += 1
                r = self.judge(kind, owner, name, desc, isf, lb)
                if r is None:
                    continue
                dotted = owner.replace("/", ".")
                if kind == "c":
                    what = "class " + dotted
                else:
                    what = "%s.%s%s" % (dotted, name, "" if isf else pretty_params(desc))
                f = (cname, m.name, what + (("  [" + r[2] + "]") if r[2] else ""), r[0], r[1], owner, kind)
                if f not in seen:
                    seen.add(f)
        for c in own.values():                            # a superclass or interface newer than minSdk cannot be guarded at all
            req, tgt, sup = self.chain(c.name)
            L = INF if sup else max(self.min, req, tgt)
            for o in ([c.super] if c.super else []) + list(c.interfaces):
                r = self.judge("c", o, None, None, False, L)
                if r is not None:
                    f = (c.name.replace("/", "."), "<class>", "class " + o.replace("/", "."), r[0], r[1], o, "c")
                    seen.add(f)
        classes_hit = {(f[0], f[1], f[5]) for f in seen if f[6] == "c"}
        for f in sorted(seen):                            # a newer class is reported once, not again for each of its members
            if f[6] == "m" and (f[0], f[1], f[5]) in classes_hit:
                continue
            res.findings.append((f[0], f[1], f[2], f[3], f[4]))
        res.degraded.sort()
        return res


D8_OWNERS = classfile.D8_DESUGARED_OWNERS


def check(program, api, min_level, compile_level, jar_for, deps=None, backports=None, others=()):
    """program: {class name: bytes} of the app's own classes. api: classfile.Api of the compile-time android.jar.
    jar_for(level) -> classfile.Api of android.jar for that level, or raises Unavailable. deps: JarIndex (or anything with .get(name)).
    backports(min_level) -> set of 'owner#name(desc)' that D8 rewrites at that minSdk. others: class names that come from dependencies.
    -> Result"""
    res = Result()
    if min_level >= compile_level:
        return res
    try:
        jar_for(min_level)
    except Unavailable as e:
        res.skipped = "android.jar for API %d is not available (%s)" % (min_level, str(e).splitlines()[0] if str(e) else "no reason given")
        return res
    own = {}
    for name, data in program.items():
        try:
            own[name] = read(data)
        except (ValueError, IndexError, struct.error) as e:
            res.unreadable.append("%s: %s" % (name, e))
    _Guard(own, api, min_level, compile_level, jar_for, deps, backports, set(others), res).run()
    return res


def render(res, min_level, mode):
    """-> (message about references that are newer, message about unverified ones, other notes); each None when empty"""
    newer = [f for f in res.findings if f[3] == "newer"]
    unver = [f for f in res.findings if f[3] == "unverified"]

    def line(f):
        extra = ""
        if f[3] == "newer" and f[4] > min_level:
            extra = "   (the SDK_INT >= %d check here is too low: android.jar %d does not have it)" % (f[4], f[4])
        elif f[3] == "unverified":
            extra = "   (needs android.jar %d to decide)" % f[4]
        return "%s.%s: %s%s" % (f[0], f[1], f[2], extra)

    def block(head, fs, tail):
        lines = [line(f) for f in fs]
        return "%s\n    %s%s%s" % (head, "\n    ".join(lines[:12]), ("\n    ... %d more" % (len(lines) - 12)) if len(lines) > 12 else "", tail)

    msg = None
    if newer:
        msg = block("min-API guard: %d reference(s) to platform APIs newer than minSdk %d, with no SDK_INT check, @RequiresApi or @TargetApi around them "
                    "(devices below the API throw NoSuchMethodError / NoClassDefFoundError):" % (len(newer), min_level), newer,
                    "\n  (guard each with Build.VERSION.SDK_INT >= N or annotate it, or raise minSdk; \"minApiCheck\": \"off\" silences this, \"error\" fails the build)")
    um = None
    if unver:
        um = block("min-API guard: %d reference(s) could not be verified because an android.jar they need is not available:" % len(unver), unver, "")
    notes = []
    if res.skipped:
        notes.append("min-API guard skipped: %s. Run `apkforge build` once with network access, or install platform android-%d under ANDROID_HOME, to enable it." % (res.skipped, min_level))
    if res.degraded:
        notes.append("min-API guard: %d method(s) could not be analysed precisely and were judged without SDK_INT knowledge (first: %s)" % (len(res.degraded), res.degraded[0]))
    if res.unreadable:
        notes.append("min-API guard: %d class file(s) could not be read (first: %s)" % (len(res.unreadable), res.unreadable[0]))
    return msg, um, notes
