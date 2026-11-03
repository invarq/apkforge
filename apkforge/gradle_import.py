"""import-gradle: read an existing Gradle project WITHOUT running Gradle and write apkforge.json.

Gradle build scripts are programs. This module does not run them: it tokenizes Groovy and Kotlin DSL scripts, builds a tree of statements and
reads the part of the Android Gradle plugin DSL that apkforge can build. The contract that matters more than any single mapping:

  * every statement lands in exactly ONE class: MAPPED (it ended up in apkforge.json), IGNORED ON PURPOSE (it does not change what apkforge builds,
    with the reason), NOT MAPPED (it matters and apkforge cannot do it: reported with file:line), or STRUCTURAL (a container or a variable);
    stats["total"] is counted independently of the handlers and must equal the sum, otherwise a note says so;
  * a value that is not a literal after resolution is never written; a required key that is missing is reported as DEFAULT APPLIES;
  * secrets are never copied (signing passwords become storePasswordEnv / keyPasswordEnv).

No network, no JDK, no toolchain. Standard library only. Entry point: import_project(path, write=True, force=False).
"""
import json
import os
import re
import xml.etree.ElementTree as ET

from .project import DEFAULTS
from .util import ForgeError

VERSION = "1"


# ====================================================================================================== tokens
class Tok(object):
    __slots__ = ("kind", "val", "line", "pos", "end")

    def __init__(self, kind, val, line, pos, end):
        self.kind, self.val, self.line, self.pos, self.end = kind, val, line, pos, end

    def __repr__(self):
        return "Tok(%s, %r, L%d)" % (self.kind, self.val, self.line)


_ID = re.compile(r"[^\W\d]\w*", re.U)
_NUM = re.compile(r"0[xX][0-9a-fA-F_]+[lL]?|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?[lLfFdD]?")
_OPS2 = ("?.", "?:", "!!", "==", "!=", "<=", ">=", "+=", "-=", "*=", "/=", "->", "::", "..", "&&", "||", "*.", "<<", ">>")
_ESC = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "0": "\0"}


def _read_string(src, i, line, kts):
    """-> (token, next index, next line). A string token's value is a list of parts: (False, text) or (True, expression text) for $x / ${x}."""
    q = src[i]
    triple = src.startswith(q * 3, i)
    start, start_line, n = i, line, len(src)
    i += 3 if triple else 1
    interp = q == '"'
    parts, buf = [], []
    while i < n:
        c = src[i]
        if triple and src.startswith(q * 3, i):
            i += 3
            break
        if not triple and c == q:
            i += 1
            break
        if not triple and c == "\n":                          # unterminated: stop at the line end instead of swallowing the file
            break
        if c == "\\" and not (triple and kts):
            if i + 1 < n:
                d = src[i + 1]
                if d == "u" and re.match(r"[0-9a-fA-F]{4}", src[i + 2:i + 6]):
                    buf.append(chr(int(src[i + 2:i + 6], 16)))
                    i += 6
                    continue
                buf.append(_ESC.get(d, d))
                if d == "\n":
                    line += 1
                i += 2
                continue
        if interp and c == "$":
            if src.startswith("${", i):
                j, depth = i + 2, 1
                while j < n and depth:
                    if src[j] == "{":
                        depth += 1
                    elif src[j] == "}":
                        depth -= 1
                    elif src[j] in "\"'" and depth:
                        k = j + 1
                        while k < n and src[k] != src[j] and src[k] != "\n":
                            k += 2 if src[k] == "\\" else 1
                        j = k
                    j += 1
                if buf:
                    parts.append((False, "".join(buf)))
                    buf = []
                parts.append((True, src[i + 2:j - 1]))
                i = j
                continue
            m = _ID.match(src, i + 1)
            if m:
                name, j = m.group(), m.end()
                if not kts:                                   # Groovy: "$a.b.c" reads a property chain
                    while j + 1 < n and src[j] == "." and _ID.match(src, j + 1):
                        m2 = _ID.match(src, j + 1)
                        name += "." + m2.group()
                        j = m2.end()
                if buf:
                    parts.append((False, "".join(buf)))
                    buf = []
                parts.append((True, name))
                i = j
                continue
        if c == "\n":
            line += 1
        buf.append(c)
        i += 1
    if buf or not parts:
        parts.append((False, "".join(buf)))
    return Tok("str", parts, start_line, start, i), i, line


def tokenize(src, kts=False):
    toks, i, n, line = [], 0, len(src), 1
    while i < n:
        c = src[i]
        if c == "\n":
            toks.append(Tok("nl", "\n", line, i, i + 1))
            line += 1
            i += 1
        elif c in " \t\r\f\ufeff":
            i += 1
        elif c == "/" and src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif c == "/" and src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += src.count("\n", i, j)
            i = j
        elif c == "#" and i == 0 and src.startswith("#!"):
            j = src.find("\n")
            i = n if j < 0 else j
        elif c in "\"'":
            tok, i, line = _read_string(src, i, line, kts)
            toks.append(tok)
        elif c == "`":
            j = src.find("`", i + 1)
            j = n if j < 0 else j
            toks.append(Tok("id", src[i + 1:j], line, i, j + 1))
            i = j + 1
        elif c.isdigit():
            m = _NUM.match(src, i)
            toks.append(Tok("num", m.group(), line, i, m.end()))
            i = m.end()
        else:
            m = _ID.match(src, i)
            if m:
                toks.append(Tok("id", m.group(), line, i, m.end()))
                i = m.end()
            elif src[i:i + 2] in _OPS2:
                toks.append(Tok("op", src[i:i + 2], line, i, i + 2))
                i += 2
            else:
                toks.append(Tok("op", c, line, i, i + 1))
                i += 1
    return toks


def plain(tok):
    """the text of a string token that has no $references, else None"""
    if tok.kind != "str" or any(r for r, _t in tok.val):
        return None
    return "".join(t for _r, t in tok.val)


# ====================================================================================================== statements
class Stmt(object):
    __slots__ = ("toks", "block", "line", "kind", "name", "op", "rest", "sub", "after")

    def __init__(self, toks, block, line):
        self.toks, self.block, self.line = toks, block, line
        self.kind, self.name, self.op, self.rest, self.sub, self.after = "stmt", "", None, [], None, []
        _analyze(self)


_CONT_END = frozenset([",", "=", "+", "-", "*", "/", ".", "?.", "&&", "||", "?", ":", "?:", "+=", "-=", "->", "<<", "==", "!=", "(", "["])
_CONT_START = frozenset([".", "?.", "&&", "||", "?:"])
_DECL = frozenset(["def", "val", "var", "final", "const", "String", "Integer", "int", "boolean", "Boolean", "long", "Long", "List", "Map", "Object", "float", "double"])
_OPEN, _CLOSE = "([{", ")]}"


def _is(t, kind, val=None):
    return t is not None and t.kind == kind and (val is None or t.val == val)


def _continues(head, toks, i):
    if head and head[-1].kind == "op" and head[-1].val in _CONT_END:
        return True
    j = i
    while j < len(toks) and toks[j].kind == "nl":
        j += 1
    return j < len(toks) and toks[j].kind == "op" and toks[j].val in _CONT_START


def _parse_block(toks, i):
    """statements up to the matching '}' (or the end) -> (statements, next index)"""
    stmts, n = [], len(toks)
    while i < n:
        t = toks[i]
        if t.kind == "nl" or _is(t, "op", ";"):
            i += 1
            continue
        if _is(t, "op", "}"):
            return stmts, i + 1
        head, block, depth, line = [], None, 0, t.line
        while i < n:
            t = toks[i]
            if depth == 0:
                if _is(t, "op", ";") or _is(t, "op", "}"):
                    break
                if t.kind == "nl":
                    if _continues(head, toks, i):
                        i += 1
                        continue
                    break
                if _is(t, "op", "{"):
                    inner, i = _parse_block(toks, i + 1)
                    block = (block or []) + inner
                    continue
            if t.kind == "op":
                if t.val in "([":
                    depth += 1
                elif t.val in ")]":
                    depth = max(0, depth - 1)
                elif t.val == "{":                            # a closure inside parentheses: keep its tokens flat
                    j, d = i, 0
                    while j < n:
                        if _is(toks[j], "op", "{"):
                            d += 1
                        elif _is(toks[j], "op", "}"):
                            d -= 1
                            if d == 0:
                                break
                        j += 1
                    head.extend(x for x in toks[i:j + 1] if x.kind != "nl")
                    i = j + 1
                    continue
            if t.kind != "nl":
                head.append(t)
            i += 1
        if head or block is not None:
            stmts.append(Stmt(head, block, line))
    return stmts, i


def parse(src, kts=False):
    return _parse_block(tokenize(src, kts), 0)[0]


def _match(t, j):
    """index of the closer that matches the opener at t[j]"""
    d = 0
    for k in range(j, len(t)):
        if t[k].kind == "op":
            if t[k].val in _OPEN:
                d += 1
            elif t[k].val in _CLOSE:
                d -= 1
                if d == 0:
                    return k
    return len(t) - 1


def _analyze(s):
    t = s.toks
    if not t:
        return
    if t[0].kind == "id" and t[0].val in _DECL and len(t) >= 3 and t[1].kind == "id":
        j = 2
        if _is(t[j], "op", ":"):                              # Kotlin: val x: String = ... / val x: String by extra(...)
            while j < len(t) and not _is(t[j], "op", "=") and not _is(t[j], "id", "by"):
                j += 1
        if j < len(t) and (_is(t[j], "op", "=") or _is(t[j], "id", "by")):
            s.kind, s.name, s.op, s.rest = "decl", t[1].val, t[j].val, t[j + 1:]
            return
    if t[0].kind == "str" and len(t) > 1 and _is(t[1], "op", "(") and plain(t[0]) is not None:      # Kotlin: "kapt"("x")
        k = _match(t, 1)
        s.name, s.op, s.rest, s.after = plain(t[0]), "(", t[2:k], t[k + 1:]
        return
    j, parts = 0, []
    while j < len(t) and t[j].kind == "id":
        parts.append(t[j].val)
        j += 1
        if j + 1 < len(t) and t[j].kind == "op" and t[j].val in (".", "?.") and t[j + 1].kind == "id":
            j += 1
            continue
        break
    if not parts:
        s.rest = t
        return
    s.name = ".".join(parts)
    if j < len(t) and _is(t[j], "op", "["):
        k = _match(t, j)
        s.sub = t[j + 1:k]
        j = k + 1
    if j < len(t) and _is(t[j], "op", "<"):                   # Kotlin: register<Delete>("clean")
        k = j
        while k < len(t) and not _is(t[k], "op", ">"):
            k += 1
        if k + 1 < len(t) and _is(t[k + 1], "op", "("):
            j = k + 1
    if j < len(t) and t[j].kind == "op" and t[j].val in ("=", "+=", "-="):
        s.op, s.rest = t[j].val, t[j + 1:]
    elif j < len(t) and _is(t[j], "op", "("):
        k = _match(t, j)
        s.op, s.rest, s.after = "(", t[j + 1:k], t[k + 1:]
    else:
        s.rest = t[j:]


def count(stmts):
    return sum(1 + count(s.block or []) for s in stmts)


def split_commas(toks):
    """tokens -> list of token lists, split at top-level commas"""
    out, cur, d = [], [], 0
    for t in toks:
        if t.kind == "op":
            if t.val in _OPEN:
                d += 1
            elif t.val in _CLOSE:
                d -= 1
            elif t.val == "," and d == 0:
                out.append(cur)
                cur = []
                continue
        cur.append(t)
    if cur:
        out.append(cur)
    return out


def named_args(parts):
    """list of token lists -> (positional token lists, {name: token list}) for `name: x` and `name = x` arguments"""
    pos, named = [], {}
    for p in parts:
        if len(p) >= 2 and p[0].kind == "id" and p[1].kind == "op" and p[1].val in (":", "="):
            named[p[0].val] = p[2:]
        else:
            pos.append(p)
    return pos, named


# ====================================================================================================== TOML (the subset version catalogs use)
class TomlError(ValueError):
    pass


def parse_toml(text):
    """A small TOML reader: tables, dotted and quoted keys, strings, numbers, booleans, arrays, inline tables. Checked against tomllib by the harness."""
    n = len(text)
    root = {}
    pos = [0]

    def err(msg):
        raise TomlError("line %d: %s" % (text.count("\n", 0, pos[0]) + 1, msg))

    def ws(newlines=False):
        while pos[0] < n:
            c = text[pos[0]]
            if c in " \t\r" or (newlines and c == "\n"):
                pos[0] += 1
            elif c == "#":
                while pos[0] < n and text[pos[0]] != "\n":
                    pos[0] += 1
            else:
                break

    def basic_string():
        i = pos[0]
        if text.startswith('"""', i):
            j = i + 3
            if text[j:j + 1] == "\n":
                j += 1
            out = []
            while j < n and not text.startswith('"""', j):
                if text[j] == "\\":
                    d = text[j + 1:j + 2]
                    if d in ("\n", " ", "\t"):
                        j += 1
                        while j < n and text[j] in " \t\n":
                            j += 1
                        continue
                    out.append(esc(text, j))
                    j += 2 if d != "u" and d != "U" else (6 if d == "u" else 10)
                    continue
                out.append(text[j])
                j += 1
            pos[0] = j + 3
            return "".join(out)
        j, out = i + 1, []
        while j < n and text[j] != '"':
            if text[j] == "\n":
                err("unterminated string")
            if text[j] == "\\":
                out.append(esc(text, j))
                d = text[j + 1:j + 2]
                j += 2 if d not in ("u", "U") else (6 if d == "u" else 10)
                continue
            out.append(text[j])
            j += 1
        pos[0] = j + 1
        return "".join(out)

    def esc(s, j):
        d = s[j + 1:j + 2]
        m = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}
        if d in m:
            return m[d]
        if d == "u":
            return chr(int(s[j + 2:j + 6], 16))
        if d == "U":
            return chr(int(s[j + 2:j + 10], 16))
        err("bad escape \\%s" % d)

    def literal_string():
        i = pos[0]
        if text.startswith("'''", i):
            j = text.find("'''", i + 3)
            if j < 0:
                err("unterminated string")
            s = text[i + 3:j]
            pos[0] = j + 3
            return s[1:] if s.startswith("\n") else s
        j = text.find("'", i + 1)
        if j < 0 or "\n" in text[i:j]:
            err("unterminated string")
        pos[0] = j + 1
        return text[i + 1:j]

    def key_path():
        path = []
        while True:
            ws()
            c = text[pos[0]:pos[0] + 1]
            if c == '"':
                path.append(basic_string())
            elif c == "'":
                path.append(literal_string())
            else:
                m = re.compile(r"[A-Za-z0-9_-]+").match(text, pos[0])
                if not m:
                    err("expected a key")
                path.append(m.group())
                pos[0] = m.end()
            ws()
            if text[pos[0]:pos[0] + 1] == ".":
                pos[0] += 1
                continue
            return path

    def value():
        ws()
        c = text[pos[0]:pos[0] + 1]
        if c == '"':
            return basic_string()
        if c == "'":
            return literal_string()
        if c == "[":
            pos[0] += 1
            arr = []
            while True:
                ws(True)
                if text[pos[0]:pos[0] + 1] == "]":
                    pos[0] += 1
                    return arr
                arr.append(value())
                ws(True)
                if text[pos[0]:pos[0] + 1] == ",":
                    pos[0] += 1
                elif text[pos[0]:pos[0] + 1] != "]":
                    err("expected , or ] in an array")
        if c == "{":
            pos[0] += 1
            tbl = {}
            while True:
                ws(True)
                if text[pos[0]:pos[0] + 1] == "}":
                    pos[0] += 1
                    return tbl
                kp = key_path()
                ws()
                if text[pos[0]:pos[0] + 1] != "=":
                    err("expected =")
                pos[0] += 1
                put(tbl, kp, value())
                ws(True)
                if text[pos[0]:pos[0] + 1] == ",":
                    pos[0] += 1
                elif text[pos[0]:pos[0] + 1] != "}":
                    err("expected , or } in an inline table")
        m = re.compile(r"[^\s,\]}#]+").match(text, pos[0])
        if not m:
            err("expected a value")
        tok = m.group()
        pos[0] = m.end()
        if tok == "true":
            return True
        if tok == "false":
            return False
        t2 = tok.replace("_", "")
        try:
            if re.match(r"^[+-]?(0|[1-9]\d*)$", t2):
                return int(t2)
            if re.match(r"^0x[0-9a-fA-F]+$", t2):
                return int(t2, 16)
            if re.match(r"^[+-]?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?$", t2):
                return float(t2)
        except ValueError:
            pass
        err("unsupported value %r" % tok)

    def put(tbl, kp, v):
        for k in kp[:-1]:
            tbl = tbl.setdefault(k, {})
            if not isinstance(tbl, dict):
                err("key %s is not a table" % k)
        if kp[-1] in tbl:
            err("duplicate key %s" % kp[-1])
        tbl[kp[-1]] = v

    cur = root
    while True:
        ws(True)
        if pos[0] >= n:
            return root
        if text[pos[0]] == "[":
            if text.startswith("[[", pos[0]):
                err("arrays of tables are not supported")
            pos[0] += 1
            kp = key_path()
            if text[pos[0]:pos[0] + 1] != "]":
                err("expected ]")
            pos[0] += 1
            cur = root
            for k in kp:
                cur = cur.setdefault(k, {})
        else:
            kp = key_path()
            ws()
            if text[pos[0]:pos[0] + 1] != "=":
                err("expected =")
            pos[0] += 1
            put(cur, kp, value())
        ws()
        if pos[0] < n and text[pos[0]] != "\n":
            err("unexpected text after a value")


# ====================================================================================================== values
class Unres(str):
    """a value that could not be resolved to a literal; the text is its source"""


class Sp(object):
    """a special value: file, proj, plat, files, filetree, env, kotlindep, proguard, signingref, plugin, bundle"""
    __slots__ = ("kind", "v", "v2")

    def __init__(self, kind, v=None, v2=None):
        self.kind, self.v, self.v2 = kind, v, v2


class _Stop(Exception):
    pass


def src_text(imp, toks, limit=110):
    toks = [t for t in toks if t.kind != "nl"]
    if not toks:
        return ""
    s = " ".join(imp.cur.src[toks[0].pos:toks[-1].end].split())
    return s if len(s) <= limit else s[:limit - 3] + "..."


def _num(text):
    t = text.replace("_", "")
    if t[-1:] in "lL" and not t.lower().startswith("0x"):
        t = t[:-1]
    try:
        if t.lower().startswith("0x"):
            return int(t.rstrip("lL"), 16)
        if re.match(r"^\d+$", t):
            return int(t)
        return float(t.rstrip("fFdD"))
    except ValueError:
        return Unres(text)


_PASS = frozenset(["get", "orNull", "asProvider", "toString", "trim", "intValue", "toInt", "toInteger", "toBoolean", "getOrNull", "toLong", "stripIndent"])


class Ev(object):
    """Evaluate the tokens of one expression to a Python value, a Sp or an Unres. Anything it does not understand is Unres, never a guess."""

    def __init__(self, imp, toks):
        self.imp, self.toks = imp, [t for t in toks if t.kind != "nl"]
        self.t, self.i = self.toks, 0

    def run(self):
        if not self.t:
            return Unres("")
        try:
            v = self.expr()
            if self.i < len(self.t):
                return Unres(src_text(self.imp, self.toks))
            return v
        except (_Stop, IndexError):
            return Unres(src_text(self.imp, self.toks))

    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else None

    def op(self, *vals):
        t = self.peek()
        return t is not None and t.kind == "op" and t.val in vals

    def expr(self):
        a = self.add()
        while self.op("?:"):
            self.i += 1
            b = self.add()
            a = b if (a is None or isinstance(a, Unres)) else a
        return a

    def add(self):
        a = self.post()
        while self.op("+"):
            self.i += 1
            a = self.concat(a, self.post())
        return a

    @staticmethod
    def concat(a, b):
        if isinstance(a, Unres) or isinstance(b, Unres):
            return Unres("%s + %s" % (a, b))
        if isinstance(a, (list, dict)) and type(a) is type(b):
            return (a + b) if isinstance(a, list) else dict(list(a.items()) + list(b.items()))
        if isinstance(a, (str, int, float)) and isinstance(b, (str, int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
            if isinstance(a, str) or isinstance(b, str):
                return str(a) + str(b)
            return a + b
        raise _Stop()

    def post(self):
        v = self.primary()
        while True:
            if self.op(".", "?."):
                nt = self.peek(1)
                if nt is None or nt.kind != "id":
                    raise _Stop()
                name = nt.val
                self.i += 2
                if self.op("("):
                    v = self.method(v, name, self.call_args())
                else:
                    v = self.prop(v, name)
            elif self.op("["):
                j = _match(self.t, self.i)
                key = self.imp.ev(self.t[self.i + 1:j])
                self.i = j + 1
                v = self.index(v, key)
            elif self.peek() is not None and self.peek().kind == "id" and self.peek().val == "as":
                self.i += 1
                while self.peek() is not None and (self.peek().kind == "id" or self.op(".", "?", "<", ">")):
                    self.i += 1
            elif self.op("!!"):
                self.i += 1
            else:
                return v

    def call_args(self):
        j = _match(self.t, self.i)
        args = split_commas(self.t[self.i + 1:j])
        self.i = j + 1
        return args

    def primary(self):
        t = self.peek()
        if t is None:
            raise _Stop()
        if t.kind == "str":
            self.i += 1
            return self.imp.str_value(t)
        if t.kind == "num":
            self.i += 1
            return _num(t.val)
        if t.kind == "op":
            if t.val == "(":
                j = _match(self.t, self.i)
                v = self.imp.ev(self.t[self.i + 1:j])
                self.i = j + 1
                return v
            if t.val == "[":
                return self.bracket()
            if t.val == "-":
                self.i += 1
                v = self.primary()
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    return -v
                raise _Stop()
            raise _Stop()
        if t.kind == "id":
            if t.val in ("true", "false"):
                self.i += 1
                return t.val == "true"
            if t.val == "null":
                self.i += 1
                return None
            if t.val == "new":
                self.i += 1
                return self.primary()
            parts = [t.val]
            self.i += 1
            while self.op(".", "?.") and self.peek(1) is not None and self.peek(1).kind == "id":
                parts.append(self.peek(1).val)
                self.i += 2
            if self.op("("):
                return self.imp.call(parts, self.call_args())
            return self.imp.lookup(parts)
        raise _Stop()

    def bracket(self):
        j = _match(self.t, self.i)
        inner = self.t[self.i + 1:j]
        self.i = j + 1
        if len(inner) == 1 and _is(inner[0], "op", ":"):
            return {}
        items = split_commas(inner)
        if items and len(items[0]) >= 2 and items[0][0].kind in ("id", "str") and _is(items[0][1], "op", ":"):
            out = {}
            for it in items:
                if len(it) < 3 or it[0].kind not in ("id", "str") or not _is(it[1], "op", ":"):
                    raise _Stop()
                k = it[0].val if it[0].kind == "id" else plain(it[0])
                if k is None:
                    raise _Stop()
                out[k] = self.imp.ev(it[2:])
            return out
        return [self.imp.ev(it) for it in items]

    def method(self, v, name, args):
        if isinstance(v, Unres):
            return v
        if name in _PASS:
            if name in ("toInt", "toInteger", "intValue", "toLong"):
                if isinstance(v, str) and re.match(r"^-?\d+$", v.strip()):
                    return int(v.strip())
                return v
            if name == "toBoolean":
                return str(v).strip().lower() == "true"
            if name == "toString":
                return str(v) if isinstance(v, (str, int, float)) else v
            return v
        if name == "getOrElse" and args:
            return v if v is not None else self.imp.ev(args[0])
        if name == "set" or name == "add":
            raise _Stop()
        raise _Stop()

    def prop(self, v, name):
        if isinstance(v, Unres):
            return v
        if isinstance(v, dict) and name in v:
            return v[name]
        if name in _PASS:
            return v
        raise _Stop()

    def index(self, v, key):
        if isinstance(v, dict) and isinstance(key, str) and key in v:
            return v[key]
        if isinstance(v, list) and isinstance(key, int) and 0 <= key < len(v):
            return v[key]
        if isinstance(v, Unres):
            return v
        raise _Stop()


# ====================================================================================================== the importer
PROJECT_DIR_TOKEN = "${projectDir}"                    # what "$projectDir" becomes inside kapt / ksp argument values: the builder expands it to the module directory
PLUGIN_APP = frozenset(["com.android.application"])
PLUGIN_LIB = frozenset(["com.android.library"])
PLUGIN_KOTLIN = frozenset(["kotlin-android", "org.jetbrains.kotlin.android"])
PLUGIN_IGNORED = {
    "maven-publish": "publishing does not change the build (use `apkforge publish`)", "signing": "Gradle's signing plugin is for publishing",
    "jacoco": "code coverage", "idea": "IDE metadata", "base": "lifecycle tasks only", "com.diffplug.spotless": "code formatting",
    "io.gitlab.arturbosch.detekt": "static analysis", "org.jlleitschuh.gradle.ktlint": "code style", "com.github.ben-manes.versions": "dependency updates report",
    "org.jetbrains.dokka": "documentation", "org.gradle.android.cache-fix": "build cache fix for Gradle itself", "com.android.lint": "lint",
}
# plugin id -> the apkforge.json key it switches on (None: no key; the builder runs kapt / KSP when the module has such dependencies)
PLUGIN_MAPPED = {
    "kotlin-kapt": None, "org.jetbrains.kotlin.kapt": None, "com.google.devtools.ksp": None,
    "org.jetbrains.kotlin.plugin.compose": "compose", "org.jetbrains.kotlin.plugin.serialization": "serialization", "kotlinx-serialization": "serialization",
    "kotlin-parcelize": "parcelize", "org.jetbrains.kotlin.plugin.parcelize": "parcelize",
    "com.google.gms.google-services": "googleServices", "com.google.firebase.crashlytics": "crashlytics",          # gms.py: google-services.json as string resources, and the Crashlytics mapping id
}
HILT_WHY = ("Hilt's Gradle plugin rewrites the base class of @AndroidEntryPoint classes in bytecode and apkforge has no such step "
            "(hilt-compiler itself runs on kapt / ksp)")
PLUGIN_UNSUPPORTED = {
    "org.jetbrains.compose": "Compose Multiplatform is not supported", "kotlin-android-extensions": "Kotlin synthetics need a compiler plugin",
    "dagger.hilt.android.plugin": HILT_WHY, "com.google.dagger.hilt.android": HILT_WHY,
    "androidx.navigation.safeargs": "Safe Args generates classes", "androidx.navigation.safeargs.kotlin": "Safe Args generates classes",
    "com.android.dynamic-feature": "dynamic feature modules are not supported", "com.android.test": "test modules are not supported",
    "org.jetbrains.kotlin.plugin.allopen": "Kotlin compiler plugin", "org.jetbrains.kotlin.plugin.noarg": "Kotlin compiler plugin",
    "org.jetbrains.kotlin.plugin.jpa": "Kotlin compiler plugin", "kotlin-allopen": "Kotlin compiler plugin", "kotlin-noarg": "Kotlin compiler plugin",
}
IGNORED_ANDROID = {
    "buildToolsVersion": "apkforge uses its own aapt2 / D8", "ndkVersion": "ndkVersion is mapped only as a literal string (native.ndkVersion)",
    "lint": "lint is not run by apkforge", "lintOptions": "lint is not run by apkforge", "testOptions": "tests are not built by apkforge", "testBuildType": "tests are not built by apkforge",
    "testCoverage": "tests are not built by apkforge", "dependenciesInfo": "store metadata block", "installation": "adb install options", "adbOptions": "adb install options",
    "dexOptions": "removed from AGP; D8 settings are apkforge's", "compileSdkPreview": None, "useLibrary": "optional shared libraries are declared in the manifest",
    "namespace_unused": None,
}
UNMAPPED_ANDROID = {
    "productFlavors": "apkforge has no product flavors: every flavor-specific setting, source set and dependency is lost", "flavorDimensions": "apkforge has no product flavors",
    "splits": "ABI / density splits are not supported", "externalNativeBuild": "only its cmake { } block is mapped (apkforge builds CMake projects; ndk-build is not supported)",
    "packagingOptions": "apkforge's resource merging rules are built in (excludes / pickFirst are not configurable)", "packaging": "apkforge's resource merging rules are built in (excludes / pickFirst are not configurable)",
    "dataBinding": "data binding generates classes that apkforge does not",
    "variantFilter": "variant API: apkforge has debug and release only", "applicationVariants": "variant API: apkforge has debug and release only", "libraryVariants": "variant API: apkforge has debug and release only",
    "androidComponents": "variant API: apkforge has debug and release only", "bundle": None, "experimentalProperties": "experimental AGP options", "defaultPublishConfig": "library variant publishing",
    "publishing": "AAR publishing options (use `apkforge publish`)", "aaptOptions_unused": None, "buildFeatures_unused": None, "sourceSets_other": None,
    "renderscriptTargetApi": "RenderScript is not supported", "flavorDimensions_unused": None, "kotlinOptions_unused": None, "resourcePrefix": "lint-only naming rule",
}
_TEST_CONF = re.compile(r"^(test|androidTest|testFixtures)|^(lintChecks|lintPublish)$|^(kapt|ksp|annotationProcessor)(Test|AndroidTest|TestFixtures)([A-Z]\w*)?$")
_VARIANT_CONF = re.compile(r"^(debug|release|staging|beta|alpha|qa|prod|dev)(Api|Implementation|CompileOnly|RuntimeOnly|AnnotationProcessor|Compile)$")
_PROC_CONF = frozenset(["annotationProcessor", "kapt", "ksp"])         # the builder resolves these from Maven coordinates only (build.py: pre.processors / pre.kapt / pre.ksp)
_VARIANT_PROC = re.compile(r"^(kapt|ksp|annotationProcessor)[A-Z]")    # kaptDebug, kspRelease, kaptFreeDebug, annotationProcessorStaging
CONF_MAP = {"api": "api", "implementation": "implementation", "compileOnly": "compileOnly", "runtimeOnly": "runtimeOnly", "annotationProcessor": "annotationProcessor",
            "kapt": "kapt", "ksp": "ksp", "compile": "api", "provided": "compileOnly", "apk": "runtimeOnly"}
CONF_LEGACY = {"compile": "api", "provided": "compileOnly", "apk": "runtimeOnly"}
KOTLIN_STDLIB = frozenset(["stdlib", "stdlib-jdk7", "stdlib-jdk8", "stdlib-common"])
_DYNAMIC = re.compile(r"(\+|^\[|^\(|^latest\.|,)")


class File(object):
    def __init__(self, path, rel, kts, src, stmts):
        self.path, self.rel, self.kts, self.src, self.stmts = path, rel, kts, src, stmts


class Module(object):
    def __init__(self, key, rel_dir, mdir):
        self.key, self.rel_dir, self.dir = key, rel_dir, mdir
        self.cfg = {}
        self.deps = {k: [] for k in ("api", "implementation", "compileOnly", "runtimeOnly", "annotationProcessor", "kapt", "ksp", "platforms", "projects", "files")}
        self.kind = None                                       # "app" | "library"
        self.vars = {}
        self.signing_configs = {}
        self.signing_ref = None
        self.java = {}
        self.src_dirs = {"java": None, "kotlin": None, "res": None, "assets": None, "manifest": None}
        self.rel = ""


def _camel(seg):
    p = re.split(r"[-_.]", seg)
    return p[0] + "".join(x[:1].upper() + x[1:] for x in p[1:])


class Importer(object):
    def __init__(self, root):
        self.root = root
        self.notes, self.ignored = [], []
        self.stats = {"total": 0, "mapped": 0, "ignored": 0, "unmapped": 0, "structural": 0, "covered": 0}
        self.props, self.catalogs, self.vars = {}, {}, {}
        self.repos = []
        self.cur = None
        self.mod = None
        self.module_paths = []
        self.root_blocks = []                                      # the root script's android { } / dependencies { } statements
        self.extra_notes = 0
        self.proj_dir_token = False                            # True only while the value of a kapt / ksp argument is read: "$projectDir" then becomes PROJECT_DIR_TOKEN

    # ---------------------------------------------------------------- reporting
    def where(self, s):
        return "%s:%d" % (self.cur.rel, s.line)

    def snip(self, s, limit=100):
        t = src_text(self, s.toks, limit)
        return t + (" { ... }" if s.block else "") if t else "{ ... }"

    def mark(self, s, cls, reason=None, cover=False):
        self.stats[cls] += 1
        if cover and s.block:
            self.stats["covered"] += count(s.block)
        if cls == "unmapped":
            self.notes.append("NOT MAPPED %s: %s -- %s" % (self.where(s), self.snip(s), reason or "apkforge cannot express this"))
        elif cls == "ignored":
            self.ignored.append("%s: %s -- %s" % (self.where(s), self.snip(s), reason))

    def snapshot(self):
        """the reporting state, to roll back a pass that only decides something (flat mode)"""
        st = dict((k, self.stats[k]) for k in ("mapped", "ignored", "unmapped", "structural", "covered"))
        return (st, len(self.notes), len(self.ignored), self.extra_notes, list(self.repos))

    def restore(self, snap):
        st, n_notes, n_ignored, extra, repos = snap
        self.stats.update(st)
        del self.notes[n_notes:]
        del self.ignored[n_ignored:]
        self.extra_notes = extra
        self.repos[:] = repos

    def warn(self, s, msg):
        """an additional remark about a statement that was mapped (does not change the classes)"""
        self.extra_notes += 1
        self.notes.append("NOTE %s: %s -- %s" % (self.where(s), self.snip(s, 80), msg))

    def default_note(self, m, key, why=""):
        self.notes.append("DEFAULT APPLIES %s: %s not found%s; apkforge's default (%s) will apply" % (m.rel, key, why, json.dumps(DEFAULTS.get(key))))

    # ---------------------------------------------------------------- values
    def ev(self, toks):
        return Ev(self, toks).run()

    def ev_text(self, text):
        saved = self.cur
        toks = tokenize(text, self.cur.kts if self.cur else False)
        self.cur = File(saved.path, saved.rel, saved.kts, text, [])
        try:
            return Ev(self, toks).run()
        finally:
            self.cur = saved

    def str_value(self, tok):
        out = []
        for is_ref, text in tok.val:
            if not is_ref:
                out.append(text)
                continue
            if self.proj_dir_token and text.strip() in ("projectDir", "project.projectDir") and "projectDir" not in self.scoped_vars() and "projectDir" not in self.props:
                out.append(PROJECT_DIR_TOKEN)                  # never resolvable anywhere else: a path outside the project would be written into apkforge.json
                continue
            v = self.ev_text(text)
            if isinstance(v, Unres) or isinstance(v, (Sp, list, dict)) or v is None:
                return Unres(src_text(self, [tok]))
            out.append(("true" if v else "false") if isinstance(v, bool) else str(v))
        return "".join(out)

    def prop(self, key):
        if isinstance(key, str) and key in self.props:
            return self.props[key]
        return Unres("property %s" % key)

    def scoped_vars(self):
        v = dict(self.vars)
        if self.mod is not None:
            v.update(self.mod.vars)
        return v

    def lookup(self, parts):
        txt = ".".join(parts)
        first = parts[0]
        if first == "JavaVersion" and len(parts) == 2:
            m = re.match(r"VERSION_(\d+)(?:_(\d+))?$", parts[1])
            if m:
                return int(m.group(2)) if m.group(1) == "1" and m.group(2) else int(m.group(1))
        if first == "JvmTarget" and len(parts) == 2:
            m = re.match(r"JVM_(\d+)(?:_(\d+))?$", parts[1])
            if m:
                return str(int(m.group(2))) if m.group(1) == "1" and m.group(2) else m.group(1)
        if first in self.catalogs:
            return self.catalog_lookup(parts)
        if first == "signingConfigs" and len(parts) == 2:
            return Sp("signingref", parts[1])
        env = self.scoped_vars()
        if first == "projects" and "projects" not in env and len(parts) > 1:
            return self.project_accessor(parts[1:])
        p = list(parts)
        while len(p) > 1 and p[0] in ("rootProject", "project", "this", "ext", "extra", "gradle", "parent", "settings"):
            p = p[1:]
        if p[0] == "properties" and len(p) == 2:
            return self.prop(p[1])
        if p[0] in env:
            v = env[p[0]]
        elif p[0] in self.props:
            v = self.props[p[0]]
        else:
            return Unres(txt)
        for key in p[1:]:
            if isinstance(v, dict) and key in v:
                v = v[key]
            else:
                return Unres(txt)
        return v

    def project_accessor(self, parts):
        for path in self.module_paths:
            segs = [_camel(x) for x in path.strip(":").split(":")]
            if segs == parts:
                return Sp("proj", path)
        return Unres("projects." + ".".join(parts))

    def catalog_lookup(self, parts):
        cat = self.catalogs[parts[0]]
        rest = list(parts[1:])
        while rest and rest[-1] in ("get", "asProvider", "orNull", "toString"):
            rest.pop()
        if not rest:
            return Unres(".".join(parts))
        kind, alias = rest[0], ".".join(rest[1:])
        if kind == "versions" and alias in cat["versions"]:
            return cat["versions"][alias]
        if kind == "bundles" and alias in cat["bundles"]:
            raw = cat["bundle_raw"].get(alias) or cat["bundles"][alias]                    # the member names as the user wrote them
            return Sp("bundle", [cat["libs"].get(a) or Unres("bundle member %s is not declared in [libraries]" % r) for a, r in zip(cat["bundles"][alias], raw)], cat["bundles"][alias])
        if kind == "plugins" and alias in cat["plugins"]:
            return Sp("plugin", cat["plugins"][alias])
        a = ".".join(rest)
        if a in cat["libs"]:
            return cat["libs"][a]
        return Unres(".".join(parts))

    def call(self, parts, args):
        name, recv = parts[-1], parts[:-1]
        pos, named = named_args(args)
        first = lambda: self.ev(pos[0]) if pos else (self.ev(named[list(named)[0]]) if named else Unres(""))
        plain_recv = recv in ([], ["project"], ["rootProject"], ["settings"], ["this"])
        if recv == ["System"] and name == "getenv":
            v = first()
            return Sp("env", v if isinstance(v, str) and not isinstance(v, Unres) else None)
        if name in ("findProperty", "property", "getProperty") and (plain_recv or recv == ["providers"]):
            return self.prop(first())
        if name == "gradleProperty" and recv == ["providers"]:
            return self.prop(first())
        if name == "file" and plain_recv:
            v = first()
            return v if isinstance(v, Unres) else Sp("file", str(v), "root" if recv == ["rootProject"] else "module")
        if name == "uri" and plain_recv:
            return first()
        if name in ("listOf", "arrayOf", "mutableListOf", "setOf", "mutableSetOf", "arrayListOf", "listOfNotNull") and plain_recv:
            return [self.ev(a) for a in pos]
        if name in ("mapOf", "mutableMapOf", "hashMapOf", "linkedMapOf") and plain_recv:
            out = {}
            for a in pos:
                k = next((i for i, t in enumerate(a) if t.kind == "id" and t.val == "to"), None)
                if k is None:
                    return Unres(src_text(self, [t for a2 in args for t in a2]))
                kv = self.ev(a[:k])
                if not isinstance(kv, str) or isinstance(kv, Unres):
                    return Unres(src_text(self, [t for a2 in args for t in a2]))
                out[kv] = self.ev(a[k + 1:])
            return out
        if name in ("platform", "enforcedPlatform") and plain_recv:
            return Sp("plat", first(), name)
        if name == "project" and plain_recv:
            v = self.ev(named["path"]) if "path" in named else first()
            return v if isinstance(v, Unres) else Sp("proj", str(v))
        if name == "files" and plain_recv:
            return Sp("files", [self.ev(a) for a in pos])
        if name == "fileTree" and plain_recv:
            d = self.ev(named["dir"]) if "dir" in named else (self.ev(pos[0]) if pos else ".")
            inc = self.ev(named["include"]) if "include" in named else None
            return Sp("filetree", d, inc)
        if name == "kotlin" and plain_recv:
            return Sp("kotlindep", first())
        if name == "getDefaultProguardFile" and plain_recv:
            return Sp("proguard", first())
        if name == "toVersion" and recv == ["JavaVersion"]:
            return self.java_version(first())
        if name == "of" and recv == ["JavaLanguageVersion"]:
            return first()
        if name == "getByName" and recv[-1:] == ["signingConfigs"]:
            return Sp("signingref", first())
        if recv:                                               # a method on a variable / property chain: minSdkProp.toInteger(), libs.versions.x.get()
            base = self.lookup(recv)
            if not isinstance(base, Unres):                    # an unresolved receiver stays unresolved, with its full source text
                return Ev(self, []).method(base, name, args)
        raise _Stop()

    @staticmethod
    def java_version(v):
        if isinstance(v, Unres):
            return v
        m = re.match(r"^(?:1\.)?(\d+)(?:\.0)?$", str(v).strip())
        return int(m.group(1)) if m else Unres(str(v))

    # ---------------------------------------------------------------- files
    def read_file(self, path, kts=None):
        rel = os.path.relpath(path, self.root).replace(os.sep, "/")
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError as e:
            raise ForgeError("cannot read %s: %s" % (path, e))
        kts = path.endswith(".kts") if kts is None else kts
        stmts = parse(src, kts)
        self.stats["total"] += count(stmts)
        return File(path, rel, kts, src, stmts)

    def load_properties(self):
        p = os.path.join(self.root, "gradle.properties")
        if not os.path.isfile(p):
            return
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line[0] in "#!":
                    continue
                m = re.match(r"^([^=:\s]+)\s*[=:]\s*(.*)$", line)
                if m:
                    self.props[m.group(1)] = m.group(2).strip()

    def load_catalog(self, name, path):
        rel = os.path.relpath(path, self.root).replace(os.sep, "/")
        try:
            with open(path, encoding="utf-8") as f:
                data = parse_toml(f.read())
        except (OSError, UnicodeDecodeError, TomlError) as e:
            self.notes.append("NOT MAPPED %s: version catalog '%s' could not be read (%s); every libs.* reference stays unresolved" % (rel, name, e))
            return
        norm = lambda k: re.sub(r"[-_.]", ".", k)
        cat = {"versions": {}, "libs": {}, "bundles": {}, "bundle_raw": {}, "plugins": {}}

        def version_of(v):
            if isinstance(v, str):
                return v
            if isinstance(v, dict):
                if "ref" in v:
                    return cat["versions"].get(norm(v["ref"]))
                for k in ("strictly", "require", "prefer"):
                    if k in v:
                        return v[k] if isinstance(v[k], str) else None
            return None

        for k, v in (data.get("versions") or {}).items():
            cat["versions"][norm(k)] = v if isinstance(v, str) else version_of(v)
        for k, spec in (data.get("libraries") or {}).items():
            g = a = ver = None
            if isinstance(spec, str):
                bits = spec.split(":")
                g, a, ver = bits[0], bits[1] if len(bits) > 1 else None, bits[2] if len(bits) > 2 else None
            elif isinstance(spec, dict):
                if "module" in spec and isinstance(spec["module"], str):
                    bits = spec["module"].split(":")
                    g, a = bits[0], bits[1] if len(bits) > 1 else None
                else:
                    g, a = spec.get("group"), spec.get("name")
                ver = version_of(spec.get("version"))
            if g and a:
                cat["libs"][norm(k)] = "%s:%s" % (g, a) + (":" + ver if ver else "")
        for k, members in (data.get("bundles") or {}).items():
            if isinstance(members, list):
                cat["bundles"][norm(k)] = [norm(x) for x in members if isinstance(x, str)]
                cat["bundle_raw"][norm(k)] = [x for x in members if isinstance(x, str)]      # as written in the catalog: the note names this spelling
        for k, spec in (data.get("plugins") or {}).items():
            if isinstance(spec, str):
                bits = spec.split(":")
                cat["plugins"][norm(k)] = (bits[0], bits[1] if len(bits) > 1 else None)
            elif isinstance(spec, dict) and isinstance(spec.get("id"), str):
                cat["plugins"][norm(k)] = (spec["id"], version_of(spec.get("version")))
        self.catalogs[name] = cat

    # ---------------------------------------------------------------- variables
    def collect_vars(self, stmts, into, top=True):
        """evaluate ext / extra / def / val assignments in order; they only feed value resolution (the handlers classify the statements later)"""
        for s in stmts:
            n = s.name
            if s.kind == "decl":
                if s.op == "=":
                    into[s.name] = self.ev(s.rest)
                else:                                          # Kotlin: val x by extra("1") / val x: T by rootProject.extra
                    r = s.rest
                    if r and r[0].kind == "id" and r[0].val == "extra" and len(r) > 1 and _is(r[1], "op", "("):
                        into[s.name] = self.ev(r[2:_match(r, 1)])
                    elif s.name in self.vars:
                        into[s.name] = self.vars[s.name]
            elif s.op == "=" and not s.block:
                parts = n.split(".")
                base = [p for p in parts if p not in ("rootProject", "project", "ext", "extra", "this")]
                if s.sub and parts[-1] in ("ext", "extra"):
                    k = self.ev(s.sub)
                    if isinstance(k, str) and not isinstance(k, Unres):
                        into[k] = self.ev(s.rest)
                elif len(base) == 1 and (top or n.startswith(("ext.", "rootProject.ext.", "project.ext."))) and base[0] not in ("minSdk", "compileSdk"):
                    if len(base) == 1 and (n.startswith(("ext.", "rootProject.ext.", "project.ext.", "extra.")) or top):
                        into[base[0]] = self.ev(s.rest)
            elif n in ("ext", "extra") and s.block is not None:
                self.collect_vars(s.block, into, top=True)
            elif n == "buildscript" and s.block is not None:
                self.collect_vars(s.block, into, top=False)

    # ---------------------------------------------------------------- scalar helpers
    def want_int(self, s, what):
        v = self.ev(s.rest)
        if isinstance(v, str) and not isinstance(v, Unres):
            m = re.match(r"^(?:android-)?(\d+)$", v.strip())
            v = int(m.group(1)) if m else Unres(v)
        if isinstance(v, bool) or not isinstance(v, int):
            self.mark(s, "unmapped", "%s is not a plain number after resolution (%s)" % (what, v if v is not None else "null"))
            return None
        return v

    def want_str(self, s, what):
        v = self.ev(s.rest)
        if isinstance(v, bool) or isinstance(v, (int, float)):
            v = str(v)
        if not isinstance(v, str) or isinstance(v, Unres):
            self.mark(s, "unmapped", "%s is not a literal string after resolution" % what)
            return None
        return v

    def strings_of(self, toks):
        """the strings named by `a, b` / `listOf(a, b)` / `[a, b]` argument lists -> (list, ok)"""
        out = []
        for part in split_commas(toks):
            v = self.ev(part)
            for x in (v if isinstance(v, list) else [v]):
                if isinstance(x, Sp) and x.kind == "proguard":
                    out.append(x)
                elif isinstance(x, str) and not isinstance(x, Unres):
                    out.append(x)
                elif isinstance(x, (int, float)) and not isinstance(x, bool):
                    out.append(str(x))
                else:
                    return out, False
        return out, True

    # ---------------------------------------------------------------- plugins
    def plugin_id(self, toks):
        """the plugin id named by the first argument of `id 'x'`, `id("x")`, `alias(libs.plugins.x)`, `kotlin("android")`, `` `java-library` `` -> (id|None, apply_false)"""
        txt = [t for t in toks if t.kind != "nl"]
        apply_false = any(t.kind == "id" and t.val == "apply" for t in txt) and txt and txt[-1].val == "false"
        first = txt[:]
        for k, t in enumerate(txt):
            if t.kind == "id" and t.val in ("version", "apply"):
                first = txt[:k]
                break
        if first and _is(first[0], "op", "("):
            first = first[1:_match(first, 0)]
        v = self.ev(first)
        if isinstance(v, Sp) and v.kind == "plugin":
            return v.v[0], apply_false
        if isinstance(v, str) and not isinstance(v, Unres):
            return v, apply_false
        return None, apply_false

    def classify_plugin(self, s, pid, mod, at_root=False):
        if pid is None:
            self.mark(s, "unmapped", "the plugin id could not be resolved")
        elif pid in PLUGIN_APP or pid in PLUGIN_LIB:
            if mod is not None:
                mod.kind = "app" if pid in PLUGIN_APP else "library"
            self.mark(s, "mapped")
        elif pid in PLUGIN_KOTLIN or pid == "org.jetbrains.kotlin.android":
            self.mark(s, "mapped")                             # Kotlin sources are detected from the files
        elif pid in PLUGIN_MAPPED:
            if mod is not None and PLUGIN_MAPPED[pid]:
                mod.cfg[PLUGIN_MAPPED[pid]] = True
            self.mark(s, "mapped")
        elif pid in PLUGIN_UNSUPPORTED:
            self.mark(s, "unmapped", "plugin %s: %s" % (pid, PLUGIN_UNSUPPORTED[pid]))
        elif pid in PLUGIN_IGNORED:
            self.mark(s, "ignored", "plugin %s: %s" % (pid, PLUGIN_IGNORED[pid]))
        elif pid in ("java-library", "java", "org.jetbrains.kotlin.jvm", "kotlin", "application"):
            self.mark(s, "mapped" if mod is None else "unmapped", None if mod is None else "plugin %s: a plain JVM module is not an Android module" % pid)
            if mod is not None:
                mod.kind = mod.kind or "jvm"
        else:
            self.mark(s, "unmapped", "unknown plugin %s: it may change the build and apkforge ignores it" % pid)

    def h_plugins(self, s, mod):
        self.mark(s, "structural")
        for p in s.block or []:
            if p.name in ("id", "alias", "kotlin") or p.kind == "stmt" and not p.name and p.toks and p.toks[0].kind == "id":
                if p.name == "kotlin":
                    pid = "org.jetbrains.kotlin." + str(self.ev(p.rest)) if p.op == "(" else None
                    self.classify_plugin(p, pid, mod)
                    continue
                if p.name in ("id", "alias"):
                    pid, apply_false = self.plugin_id(p.rest if p.op != "(" else p.rest + p.after)
                    if p.op == "(":
                        pid, apply_false = self.plugin_id(p.rest + p.after) if p.after and p.after[0].kind == "id" else self.plugin_id(p.rest)
                        if p.after and any(t.kind == "id" and t.val == "apply" for t in p.after):
                            apply_false = True
                    if apply_false:
                        self.mark(p, "ignored", "plugin declared with `apply false`: it only fixes a version for the modules")
                    else:
                        self.classify_plugin(p, pid, mod)
                    continue
                pid = p.name
                self.classify_plugin(p, pid if pid else None, mod)
            elif p.name and p.rest == [] and p.op is None and not p.block:     # `java-library` written as `java-library` is a backtick id: name is the id
                self.classify_plugin(p, p.name, mod)
            else:
                self.mark(p, "unmapped", "plugin declaration not understood")

    def h_apply(self, s, mod):
        pos, named = named_args(split_commas(s.rest))
        if "plugin" in named:
            v = self.ev(named["plugin"])
            pid = v if isinstance(v, str) and not isinstance(v, Unres) else None
            self.classify_plugin(s, pid, mod)
        elif "from" in named:
            self.mark(s, "unmapped", "`apply from:` pulls in another script that is not read")
        else:
            self.mark(s, "unmapped", "apply statement not understood")

    # ---------------------------------------------------------------- repositories
    def h_repositories(self, s, into):
        self.mark(s, "structural")
        for r in s.block or []:
            n = r.name
            if n in ("google", "mavenCentral", "mavenLocal") and not r.block or n in ("google", "mavenCentral") and r.block is not None:
                self.mark(r, "mapped")                         # built into apkforge (mavenLocal = ~/.m2)
                if r.block:
                    self.stats["covered"] += 0
            elif n == "gradlePluginPortal":
                self.mark(r, "ignored", "hosts Gradle plugins only")
            elif n == "jcenter":
                self.mark(r, "unmapped", "JCenter is shut down; apkforge reads Google Maven and Maven Central, add the artifact's real repository")
            elif n == "maven":
                url = None
                if r.op == "(" and r.rest:
                    pos, named = named_args(split_commas(r.rest))
                    url = self.ev(named["url"]) if "url" in named else (self.ev(pos[0]) if pos else None)
                for c in r.block or []:
                    if c.name in ("url", "setUrl") and (c.op in ("=", "(") or c.rest):
                        url = self.ev(c.rest)
                if isinstance(url, str) and not isinstance(url, Unres) and re.match(r"^https?://", url):
                    if url.rstrip("/") not in [x.rstrip("/") for x in into]:
                        into.append(url)
                    self.mark(r, "mapped")
                    inner = [c.name for c in (r.block or []) if c.name in ("credentials", "content", "authentication", "metadataSources")]
                    if inner:
                        self.notes.append("NOTE %s: maven { ... %s }: credentials and content filters are not copied (apkforge has no repository authentication)" % (self.where(r), ", ".join(sorted(set(inner)))))
                        self.extra_notes += 1
                    self.stats["covered"] += count(r.block or [])
                else:
                    self.mark(r, "unmapped", "repository URL is not an http(s) literal (%s)" % (src_text(self, r.rest) or "none"), cover=True)
            elif n == "flatDir":
                dirs = [self.ev(c.rest) for c in (r.block or []) if c.name == "dirs"]
                if dirs and all(d in ("libs", ["libs"]) for d in dirs):
                    self.mark(r, "mapped", cover=True)         # apkforge picks up libs/*.jar and libs/*.aar
                else:
                    self.mark(r, "unmapped", "flatDir other than libs/ is not supported (list the files in dependencies.files)", cover=True)
            else:
                self.mark(r, "unmapped", "repository type not supported", cover=True)

    # ---------------------------------------------------------------- dependencies
    def coord_of(self, v):
        """a resolved value -> 'g:a[:v]' | None (reason in self._why)"""
        self._why = None
        if isinstance(v, Unres):
            self._why = "not a literal after resolution (%s)" % v
            return None
        if not isinstance(v, str):
            self._why = "not a coordinate"
            return None
        bits = v.split("@")[0].split(":")
        if len(bits) < 2 or not bits[0] or not bits[1]:
            self._why = "not a group:artifact[:version] coordinate (%s)" % v
            return None
        ver = bits[2] if len(bits) > 2 else None
        if ver is not None and (not ver or _DYNAMIC.search(ver)):
            self._why = "dynamic or empty version %r: pin an exact version" % ver
            return None
        if len(bits) > 3:
            self._why = "classifier %r is not supported" % bits[3]
            return None
        return ":".join(bits[:3]) if ver else "%s:%s" % (bits[0], bits[1])

    def dep_values(self, s):
        """the values named by a dependency statement -> list of values (named-argument notation becomes a coordinate string)"""
        parts = split_commas(s.rest)
        pos, named = named_args(parts)
        if named and ("name" in named or "group" in named) and not pos:
            g, a, v = (self.ev(named[k]) if k in named else None for k in ("group", "name", "version"))
            if any(isinstance(x, Unres) for x in (g, a, v)) or not isinstance(a, str):
                return [Unres(src_text(self, s.rest))]
            if "classifier" in named or "ext" in named:
                return [Unres("%s:%s (classifier/ext)" % (g, a))]
            return ["%s:%s" % (g or "", a) + (":%s" % v if v else "")]
        return [self.ev(p) for p in pos] if pos else [Unres("")]

    def h_dependencies(self, s, mod):
        self.mark(s, "structural")
        for d in s.block or []:
            self.h_dep(d, mod)

    def add_dep(self, mod, key, coord):
        if coord not in mod.deps[key]:
            mod.deps[key].append(coord)

    def h_dep(self, d, mod):
        conf = d.name
        if not conf or d.kind != "stmt":
            self.mark(d, "unmapped", "dependency declaration not understood", cover=True)
            return
        if conf in ("constraints", "components", "modules"):
            self.mark(d, "unmapped", "dependency %s are not applied (apkforge takes the highest requested version)" % conf, cover=True)
            return
        if _TEST_CONF.search(conf):
            self.mark(d, "ignored", "%s: tests are not built by apkforge" % conf, cover=True)       # the closure of an ignored dependency is covered by it
            return
        if conf == "coreLibraryDesugaring":
            self.mark(d, "unmapped", "core library desugaring is not supported", cover=True)
            return
        if _VARIANT_CONF.match(conf):
            self.mark(d, "unmapped", "variant-specific dependency: apkforge has debug and release builds only (add it to implementation if both builds need it)", cover=True)
            return
        vp = _VARIANT_PROC.match(conf)
        if vp:
            self.mark(d, "unmapped", "variant-specific processor: apkforge has debug and release builds only (add it to %s if both builds need it)" % vp.group(1), cover=True)
            return
        if conf not in CONF_MAP:
            self.mark(d, "unmapped", "configuration '%s' is not supported" % conf, cover=True)
            return
        target = CONF_MAP[conf]
        vals = self.dep_values(d)
        problems, got, ign = [], 0, []                         # ign: values IGNORED on purpose (fileTree(libs), kotlin("stdlib")): reported, never silent
        for v in vals:
            if conf in _PROC_CONF and isinstance(v, Sp) and v.kind in ("proj", "files", "filetree"):       # the builder would put it on the normal classpath
                problems.append("%s(%s) is not applied: apkforge resolves processors from Maven coordinates only" % (conf, {"proj": "project(...)", "files": "files(...)", "filetree": "fileTree(...)"}[v.kind]))
                continue
            if isinstance(v, Sp) and v.kind == "bundle":
                for c in v.v:
                    if isinstance(c, Unres):                   # an alias that has no [libraries] entry
                        problems.append(str(c))
                        continue
                    cc = self.coord_of(c)
                    if cc is None:                             # e.g. a dynamic version: reported like any other dependency, not written
                        problems.append("bundle member %s: %s" % (c, self._why))
                    else:
                        self.add_dep(mod, target, cc)
                        got += 1
            elif isinstance(v, Sp) and v.kind in ("plat",):
                inner = v.v.v if isinstance(v.v, Sp) and v.v.kind == "plat" else v.v
                if isinstance(inner, Sp) and inner.kind == "proj":
                    problems.append("platform(project(...)) is not supported")
                    continue
                c = self.coord_of(inner)
                if c:
                    self.add_dep(mod, "platforms", c)
                    got += 1
                    if v.v2 == "enforcedPlatform":
                        self.warn(d, "enforcedPlatform is applied as a platform (versions may be raised by other dependencies)")
                else:
                    problems.append(self._why)
            elif isinstance(v, Sp) and v.kind == "proj":
                path = ":" + v.v.strip(":")
                if path in mod.deps["projects"]:
                    got += 1
                else:
                    mod.deps["projects"].append(path)
                    got += 1
            elif isinstance(v, Sp) and v.kind == "kotlindep":
                kn = v.v if isinstance(v.v, str) and not isinstance(v.v, Unres) else None
                if kn in KOTLIN_STDLIB:
                    ign.append("kotlin(\"%s\"): apkforge supplies the Kotlin standard library from the Kotlin distribution" % kn)
                elif kn:                                       # kotlinc runs with -no-stdlib -no-reflect (build.py): nothing else is on the classpath
                    problems.append("kotlin(\"%s\") is not mapped: kotlinc runs with -no-stdlib -no-reflect, so add org.jetbrains.kotlin:kotlin-%s:<your Kotlin version> to dependencies yourself" % (kn, kn))
                else:
                    problems.append("kotlin(...) argument is not a literal")
            elif isinstance(v, Sp) and v.kind == "files":
                for f in v.v:
                    if isinstance(f, str) and not isinstance(f, Unres):
                        self.add_dep(mod, "files", f)
                        got += 1
                    elif isinstance(f, Sp) and f.kind == "file":
                        self.add_dep(mod, "files", f.v)
                        got += 1
                    else:
                        problems.append("files(...) argument not a literal path")
            elif isinstance(v, Sp) and v.kind == "filetree":
                dirv = v.v
                if dirv == "libs":
                    ign.append("fileTree(dir libs): apkforge uses every libs/*.jar and libs/*.aar of the module itself (include / exclude patterns are not applied)")
                elif isinstance(dirv, str) and not isinstance(dirv, Unres):
                    found = []
                    base = os.path.join(mod.dir, dirv)
                    inc = v.v2 if isinstance(v.v2, list) else (["*"] if v.v2 is None else [str(v.v2)])
                    import fnmatch
                    if os.path.isdir(base):
                        for fn in sorted(os.listdir(base)):
                            if any(fnmatch.fnmatch(fn, str(pt)) for pt in inc) and fn.endswith((".jar", ".aar")):
                                found.append(os.path.join(dirv, fn).replace(os.sep, "/"))
                    for f in found:
                        self.add_dep(mod, "files", f)
                    got += 1
                    self.warn(d, "fileTree(%s) was expanded to the %d file(s) found now (%s); new files need a new import" % (dirv, len(found), ", ".join(found) or "none"))
                else:
                    problems.append("fileTree directory is not a literal")
            else:
                c = self.coord_of(v)
                if c is None:
                    problems.append(self._why or "not understood")
                else:
                    if isinstance(v, str) and len(v.split("@")[0].split(":")) > 3:
                        problems.append("classifier dropped")
                    self.add_dep(mod, target, c)
                    got += 1
                    if conf in CONF_LEGACY:
                        self.warn(d, "legacy configuration '%s' mapped to '%s'" % (conf, target))
        ign_text = "; ".join(sorted(set(ign)))
        also = (" (also ignored: %s)" % ign_text) if ign else ""
        if problems and not got:                               # nothing of this dependency is written: its closure is moot and covered by it
            self.mark(d, "unmapped", "; ".join(sorted(set(problems))) + also, cover=True)
        elif problems:
            self.mark(d, "mapped")
            self.warn(d, "partly mapped: " + "; ".join(sorted(set(problems))) + also)
            self.h_closure(d)
        elif got:
            self.mark(d, "mapped")
            if ign:
                self.warn(d, "also ignored: " + ign_text)
            self.h_closure(d)
        elif ign:                                              # only values that apkforge supplies by itself
            self.mark(d, "ignored", ign_text, cover=True)
        else:                                                  # nothing to map and nothing to report: an empty bundle / files()
            self.mark(d, "mapped")
            self.warn(d, "this declaration names no dependency")
            self.h_closure(d)

    def h_closure(self, d):
        """every statement of a dependency's configuration closure is classified on its own, with its own file:line"""
        for c in d.block or []:
            n = c.name
            if n == "because":
                self.mark(c, "ignored", "documentation only (the reason for the dependency)", cover=True)
            elif n == "exclude":
                pos, named = named_args(split_commas(c.rest))
                g = self.ev(named["group"]) if "group" in named else None
                m = self.ev(named["module"]) if "module" in named else None
                g = g if isinstance(g, str) and not isinstance(g, Unres) else None
                m = m if isinstance(m, str) and not isinstance(m, Unres) else None
                if g and m:
                    what = "the transitive dependency %s:%s" % (g, m)
                elif g:
                    what = "every module of group %s" % g
                elif m:
                    what = "every dependency named %s" % m
                else:
                    what = "the excluded transitive dependency"
                self.mark(c, "unmapped", "exclude is not applied: %s stays on the classpath" % what, cover=True)
            elif n in ("transitive", "isTransitive") and c.op == "=":
                if self.ev(c.rest) is True:
                    self.mark(c, "ignored", "transitive = true is Gradle's default", cover=True)
                else:
                    self.mark(c, "unmapped", "transitive = false is not applied: the dependencies of this dependency are still resolved", cover=True)
            elif n == "version":
                self.mark(c, "unmapped", "version constraints (strictly / require / prefer / reject) are not applied: apkforge takes the version of the coordinate", cover=True)
            else:
                self.mark(c, "unmapped", "dependency option `%s` is not applied: apkforge reads the coordinate only" % (n or "(statement)"), cover=True)

    # ---------------------------------------------------------------- android {}
    def h_android(self, s, mod):
        self.mark(s, "structural")
        for a in s.block or []:
            self.h_android_stmt(a, mod)

    def put(self, mod, key, v):
        mod.cfg[key] = v

    def h_android_stmt(self, a, mod):
        n = a.name
        if n in ("namespace",) and not a.block:
            v = self.want_str(a, "namespace")
            if v is not None:
                self.put(mod, "namespace", v)
                self.mark(a, "mapped")
        elif n in ("compileSdk", "compileSdkVersion") and not a.block:
            v = self.want_int(a, "compileSdk")
            if v is not None:
                self.put(mod, "compileSdk", v)
                self.mark(a, "mapped")
        elif n == "defaultConfig":
            self.mark(a, "structural")
            for c in a.block or []:
                self.h_default_config(c, mod)
        elif n == "buildTypes":
            self.mark(a, "structural")
            for c in a.block or []:
                self.h_build_type(c, mod)
        elif n == "signingConfigs":
            self.mark(a, "structural")
            for c in a.block or []:
                self.h_signing_config(c, mod)
        elif n == "compileOptions":
            self.mark(a, "structural")
            for c in a.block or []:
                self.h_compile_options(c, mod)
        elif n == "kotlinOptions":
            self.mark(a, "structural")
            for c in a.block or []:
                self.h_kotlin_options(c, mod)
        elif n == "buildFeatures":
            self.mark(a, "structural")
            for c in a.block or []:
                self.h_build_feature(c, mod)
        elif n == "sourceSets":
            self.mark(a, "structural")
            for c in a.block or []:
                self.h_source_set(c, mod)
        elif n in ("aaptOptions", "androidResources"):
            self.mark(a, "structural")
            for c in a.block or []:
                self.h_aapt(c, mod)
        elif n == "viewBinding" and a.block is not None:                        # legacy: android { viewBinding { enabled = true } }
            self.mark(a, "structural")
            for c in a.block:
                self.h_view_binding(c, mod)
        elif n in ("viewBinding.enabled", "viewBinding.isEnabled"):
            self.h_view_binding(a, mod)
        elif n == "composeOptions" and a.block is not None:
            self.mark(a, "structural")
            for c in a.block:
                self.h_compose_options(c, mod)
        elif n == "ndkVersion" and not a.block:
            v = self.want_str(a, "ndkVersion")
            if v is not None:
                self.native_put(mod, "ndkVersion", v)
                self.mark(a, "mapped")
        elif n == "externalNativeBuild" and a.block is not None:
            self.mark(a, "structural")
            for c in a.block:
                self.h_external_native_build(c, mod)
        elif n in UNMAPPED_ANDROID and UNMAPPED_ANDROID[n]:
            self.mark(a, "unmapped", UNMAPPED_ANDROID[n], cover=True)
        elif n in IGNORED_ANDROID and IGNORED_ANDROID[n]:
            self.mark(a, "ignored", IGNORED_ANDROID[n], cover=True)
        else:
            self.mark(a, "unmapped", "android.%s is not read by apkforge" % (n or "(statement)"), cover=True)

    def h_view_binding(self, c, mod):
        """android { viewBinding { enabled = true } } / viewBinding.enabled = true: the legacy spelling of buildFeatures { viewBinding = true }"""
        v = self.ev(c.rest) if not c.block else None
        if c.name in ("enabled", "isEnabled", "viewBinding.enabled", "viewBinding.isEnabled") and isinstance(v, bool):
            if v:
                mod.cfg["viewBinding"] = True
                self.mark(c, "mapped")
            else:
                self.mark(c, "ignored", "feature switched off")
        else:
            self.mark(c, "unmapped", "viewBinding.%s is not understood" % (c.name or "(statement)"), cover=True)

    def h_compose_options(self, c, mod):
        if c.name == "kotlinCompilerExtensionVersion" and not c.block:
            self.mark(c, "ignored", "apkforge compiles Compose with the Compose compiler plugin that ships with its Kotlin 2.x compiler; an explicit compiler version (Kotlin 1.x projects) is not used")
        else:
            self.mark(c, "unmapped", "composeOptions.%s is not read by apkforge" % (c.name or "(statement)"), cover=True)

    def h_default_config(self, c, mod):
        n = c.name
        if n == "applicationId" and not c.block:
            v = self.want_str(c, "applicationId")
            if v is not None:
                self.put(mod, "applicationId", v)
                self.mark(c, "mapped")
        elif n in ("minSdk", "minSdkVersion") and not c.block:
            v = self.want_int(c, "minSdk")
            if v is not None:
                self.put(mod, "minSdk", v)
                self.mark(c, "mapped")
        elif n in ("targetSdk", "targetSdkVersion") and not c.block:
            v = self.want_int(c, "targetSdk")
            if v is not None:
                self.put(mod, "targetSdk", v)
                self.mark(c, "mapped")
        elif n == "versionCode" and not c.block:
            v = self.want_int(c, "versionCode")
            if v is not None:
                self.put(mod, "versionCode", v)
                self.mark(c, "mapped")
        elif n == "versionName" and not c.block:
            v = self.want_str(c, "versionName")
            if v is not None:
                self.put(mod, "versionName", v)
                self.mark(c, "mapped")
        elif n in ("manifestPlaceholders", "addManifestPlaceholders") and not c.block:
            self.h_placeholders(c, mod)
        elif n == "buildConfigField" and not c.block:
            self.h_build_config_field(c, mod)
        elif n == "resValue" and not c.block:
            args = split_commas(c.rest)
            vals = [self.ev(x) for x in args]
            if len(vals) == 3 and all(isinstance(x, str) and not isinstance(x, Unres) for x in vals):
                mod.cfg.setdefault("resValues", {})[vals[1]] = {"type": vals[0], "value": vals[2]}
                self.mark(c, "mapped")
            else:
                self.mark(c, "unmapped", "resValue arguments are not three literal strings")
        elif n == "ndk" and c.block is not None:
            self.mark(c, "structural")
            for x in c.block:
                self.h_ndk(x, mod)
        elif n == "consumerProguardFiles" and not c.block:
            files, ok_ = self.strings_of(c.rest)
            if ok_ and all(isinstance(f, str) for f in files):
                mod.cfg.setdefault("consumerProguardFiles", [])
                mod.cfg["consumerProguardFiles"] += [f for f in files if f not in mod.cfg["consumerProguardFiles"]]
                self.mark(c, "mapped")
            else:
                self.mark(c, "unmapped", "consumerProguardFiles arguments are not literal")
        elif n in ("proguardFiles",) and not c.block:
            self.h_proguard_files(c, mod)
        elif n in ("testInstrumentationRunner", "testApplicationId", "testHandleProfiling", "testFunctionalTest", "testInstrumentationRunnerArguments"):
            self.mark(c, "ignored", "tests are not built by apkforge", cover=True)
        elif n == "vectorDrawables":
            self.mark(c, "ignored", "aapt2 / D8 handle vector drawables from minSdk 21 on", cover=True)
        elif n == "multiDexEnabled":
            self.mark(c, "ignored", "D8 writes several dex files by itself")
        elif n in ("signingConfig",):
            self.signing_ref(c, mod)
        elif n in ("enableV1Signing", "v1SigningEnabled"):
            self.mark(c, "unmapped", "set in signingConfigs, not defaultConfig")
        elif n in ("resConfigs", "resourceConfigurations", "resConfig"):
            self.mark(c, "unmapped", "resource configuration filtering is not supported: all resources are packaged", cover=True)
        elif n == "externalNativeBuild" and c.block is not None:
            self.mark(c, "structural")
            for x in c.block:
                self.h_external_native_build(x, mod)
        elif n in ("javaCompileOptions", "missingDimensionStrategy", "dimension", "matchingFallbacks", "proguardFile"):
            self.mark(c, "unmapped", "defaultConfig.%s is not read by apkforge" % n, cover=True)
        else:
            self.mark(c, "unmapped", "defaultConfig.%s is not read by apkforge" % (n or "(statement)"), cover=True)

    def h_placeholders(self, c, mod):
        d = mod.cfg.setdefault("placeholders", {})
        if c.sub is not None and c.op == "=":                  # manifestPlaceholders["k"] = "v"
            k, v = self.ev(c.sub), self.ev(c.rest)
            pairs = {k: v} if isinstance(k, str) else None
        elif c.op in ("=", "+=") or c.op == "(" or c.op is None:
            v = self.ev(c.rest)
            pairs = v if isinstance(v, dict) else None
        else:
            pairs = None
        if pairs is None or any(isinstance(v, (Unres, Sp, list, dict)) or v is None or isinstance(k, Unres) for k, v in pairs.items()):
            self.mark(c, "unmapped", "manifest placeholder values are not literals after resolution")
            if not d:
                del mod.cfg["placeholders"]
            return
        for k, v in pairs.items():
            d[k] = ("true" if v else "false") if isinstance(v, bool) else str(v)
        self.mark(c, "mapped")

    def h_build_config_field(self, c, mod):
        vals = [self.ev(x) for x in split_commas(c.rest)]
        if len(vals) != 3 or not all(isinstance(x, str) and not isinstance(x, Unres) for x in vals):
            self.mark(c, "unmapped", "buildConfigField arguments are not three literal strings")
            return
        t, name, lit = vals
        lit = lit.strip()
        if t == "String":
            if len(lit) >= 2 and lit[0] == '"' and lit[-1] == '"':
                val = re.sub(r'\\(["\\])', r"\1", lit[1:-1]).replace("\\n", "\n")
            else:
                self.mark(c, "unmapped", "a String BuildConfig value that is not a quoted literal (%s) cannot be written as apkforge JSON" % lit[:40])
                return
        elif t in ("long", "int", "float", "double", "boolean"):
            val = lit.rstrip("lLfFdD") if t != "boolean" else lit
            if t == "boolean" and lit not in ("true", "false"):
                self.mark(c, "unmapped", "boolean BuildConfig value is not true/false")
                return
            if t in ("long", "int") and not re.match(r"^-?\d+$", val):
                self.mark(c, "unmapped", "%s BuildConfig value is not a plain number" % t)
                return
        else:
            val = lit
        mod.cfg.setdefault("buildConfigFields", {})[name] = {"type": t, "value": val}
        self.mark(c, "mapped")

    def native_put(self, mod, key, v, extend=False):
        n = mod.cfg.setdefault("native", {})
        if extend:
            lst = n.setdefault(key, [])
            lst += [x for x in v if x not in lst]
        else:
            n[key] = v

    def h_external_native_build(self, x, mod):
        """android { externalNativeBuild { cmake { path, version } } } and defaultConfig { externalNativeBuild { cmake { arguments, cFlags, cppFlags, targets } } } -> the "native" setting."""
        if x.name == "cmake" and x.block is not None:
            self.mark(x, "structural")
            for y in x.block:
                n = y.name
                if n == "path" and not y.block:
                    v = self.ev(y.rest)
                    if isinstance(v, str) and not isinstance(v, Unres):
                        self.native_put(mod, "cmake", v)
                        self.mark(y, "mapped")
                    else:
                        self.mark(y, "unmapped", "cmake.path is not a literal string after resolution")
                elif n == "version" and not y.block:
                    v = self.want_str(y, "cmake.version")
                    if v is not None:
                        self.native_put(mod, "cmakeVersion", v)
                        self.mark(y, "mapped")
                elif n in ("arguments", "cFlags", "cppFlags", "targets") and not y.block:
                    vals, ok_ = self.strings_of(y.rest)
                    if ok_ and all(isinstance(v, str) for v in vals):
                        self.native_put(mod, n, vals, extend=True)
                        self.mark(y, "mapped")
                    else:
                        self.mark(y, "unmapped", "cmake.%s arguments are not literal strings" % n)
                else:
                    self.mark(y, "unmapped", "externalNativeBuild.cmake.%s is not read by apkforge" % (n or "(statement)"), cover=True)
        elif x.name == "ndkBuild":
            self.mark(x, "unmapped", "ndk-build is not supported by apkforge (CMake is): the native code is not compiled", cover=True)
        else:
            self.mark(x, "unmapped", "externalNativeBuild.%s is not read by apkforge" % (x.name or "(statement)"), cover=True)

    def h_ndk(self, x, mod):
        if x.name.endswith("abiFilters") or x.name.endswith("abiFilters.addAll") or x.name.endswith("abiFilters.add"):
            vals, ok_ = self.strings_of(x.rest)
            if ok_ and vals and all(isinstance(v, str) for v in vals):
                abis = mod.cfg.setdefault("abis", [])
                abis += [v for v in vals if v not in abis]
                self.mark(x, "mapped")
                return
        self.mark(x, "unmapped", "ndk.%s is not read by apkforge" % (x.name or "(statement)"), cover=True)

    def h_proguard_files(self, c, mod, quiet=False):
        files, ok_ = self.strings_of(c.rest)
        if not ok_:
            self.mark(c, "unmapped", "proguardFiles arguments are not literal")
            return
        real = [f for f in files if isinstance(f, str)]
        dropped = [f for f in files if isinstance(f, Sp)]
        mod.cfg.setdefault("proguardFiles", [])
        mod.cfg["proguardFiles"] += [f for f in real if f not in mod.cfg["proguardFiles"]]
        self.mark(c, "mapped")
        if dropped:
            self.warn(c, "getDefaultProguardFile(...) was dropped: apkforge has its own default R8 rules")

    def h_build_type(self, c, mod):
        name = c.name
        target = None
        if name in ("getByName", "named", "maybeCreate", "create", "register") and c.rest:
            v = self.ev(c.rest)
            target = v if isinstance(v, str) and not isinstance(v, Unres) else None
        else:
            target = name
        if c.block is None:
            self.mark(c, "unmapped", "build type declaration not understood")
            return
        if target == "release":
            self.mark(c, "structural")
            for x in c.block:
                self.h_release(x, mod)
        elif target == "debug":
            self.mark(c, "structural")
            for x in c.block:
                n = x.name
                if n in ("isDebuggable", "debuggable") and not x.block:
                    self.mark(x, "ignored", "debug builds are debuggable already")
                elif n in ("testCoverageEnabled", "enableUnitTestCoverage", "enableAndroidTestCoverage"):
                    self.mark(x, "ignored", "tests are not built by apkforge")
                elif n in ("minifyEnabled", "isMinifyEnabled"):
                    self.mark(x, "unmapped", "apkforge minifies release builds only")
                elif n in ("signingConfig",):
                    self.mark(x, "ignored", "debug builds are signed with the debug key")
                else:
                    self.mark(x, "unmapped", "debug build type setting '%s' has no apkforge equivalent" % (n or "statement"), cover=True)
        else:
            self.mark(c, "unmapped", "build type '%s': apkforge has debug and release only" % target, cover=True)

    def h_release(self, x, mod):
        n = x.name
        if n in ("minifyEnabled", "isMinifyEnabled") and not x.block:
            v = self.ev(x.rest)
            if isinstance(v, bool):
                mod.cfg["minify"] = v
                self.mark(x, "mapped")
            else:
                self.mark(x, "unmapped", "minifyEnabled is not true/false after resolution")
        elif n == "proguardFiles" and not x.block:
            self.h_proguard_files(x, mod)
        elif n == "signingConfig":
            self.signing_ref(x, mod)
        elif n in ("shrinkResources", "isShrinkResources"):
            self.mark(x, "unmapped", "resource shrinking is not supported by apkforge")
        elif n in ("isDebuggable", "debuggable"):
            self.mark(x, "unmapped", "a debuggable release build is not supported")
        elif n in ("testCoverageEnabled", "enableUnitTestCoverage", "enableAndroidTestCoverage"):
            self.mark(x, "ignored", "tests are not built by apkforge")
        elif n == "buildConfigField":
            self.mark(x, "unmapped", "per-build-type BuildConfig field: apkforge's buildConfigFields apply to every build")
        else:
            self.mark(x, "unmapped", "release.%s has no apkforge equivalent" % (n or "(statement)"), cover=True)

    def h_signing_config(self, c, mod):
        name = c.name
        if name in ("create", "register", "maybeCreate", "getByName", "named") and c.rest:
            v = self.ev(c.rest)
            name = v if isinstance(v, str) and not isinstance(v, Unres) else None
        if not name or c.block is None:
            self.mark(c, "unmapped", "signing config declaration not understood", cover=True)
            return
        sc = {"storeFile": None, "keyAlias": None, "storePassword": None, "keyPassword": None, "v1": None, "v4": None}
        self.mark(c, "structural")
        for x in c.block:
            n = x.name
            v = self.ev(x.rest)
            if n == "storeFile":
                sc["storeFile"] = v
                self.mark(x, "mapped")
            elif n == "keyAlias":
                sc["keyAlias"] = v if isinstance(v, str) and not isinstance(v, Unres) else None
                self.mark(x, "mapped" if sc["keyAlias"] else "unmapped", None if sc["keyAlias"] else "keyAlias is not a literal")
            elif n in ("storePassword", "keyPassword"):
                sc[n] = v
                self.mark(x, "mapped")
            elif n in ("enableV1Signing", "v1SigningEnabled"):
                sc["v1"] = v if isinstance(v, bool) else None
                self.mark(x, "mapped" if isinstance(v, bool) else "unmapped", None if isinstance(v, bool) else "not true/false")
            elif n in ("enableV4Signing", "v4SigningEnabled"):
                sc["v4"] = v if isinstance(v, bool) else None
                self.mark(x, "mapped" if isinstance(v, bool) else "unmapped", None if isinstance(v, bool) else "not true/false")
            elif n in ("enableV2Signing", "enableV3Signing", "v2SigningEnabled", "v3SigningEnabled", "storeType"):
                self.mark(x, "ignored", "apkforge writes v2 and v3 signatures itself")
            else:
                self.mark(x, "unmapped", "signingConfigs.%s.%s is not read by apkforge" % (name, n or "(statement)"), cover=True)
        mod.signing_configs[name] = sc

    def signing_ref(self, x, mod):
        v = self.ev(x.rest)
        if isinstance(v, Sp) and v.kind == "signingref":
            mod.signing_ref = (v.v, x)
            self.mark(x, "mapped")
        else:
            self.mark(x, "unmapped", "signingConfig is not a reference to signingConfigs.<name>")

    def apply_signing(self, mod):
        """build the apkforge signing section from the config the release build type refers to; passwords are never copied"""
        if not mod.signing_ref:
            return
        name, stmt = mod.signing_ref
        sc = mod.signing_configs.get(name)
        if sc is None:
            self.notes.append("NOT MAPPED %s: release refers to signing config '%s' that is not declared in this module: release builds are signed with the DEBUG key" % (mod.rel, name))
            return
        sf = sc["storeFile"]
        path = None
        if isinstance(sf, Sp) and sf.kind == "file":
            p = sf.v
            if not os.path.isabs(p) and sf.v2 == "root":
                p = os.path.relpath(os.path.join(self.root, p), mod.dir)
            path = p.replace(os.sep, "/")
        elif isinstance(sf, str) and not isinstance(sf, Unres):
            path = sf
        if not path or not sc["keyAlias"]:
            self.notes.append("NOT MAPPED %s: signing config '%s' has no literal storeFile / keyAlias: release builds are signed with the DEBUG key until you add \"signing\" to apkforge.json" % (mod.rel, name))
            return
        out = {"keystore": path, "alias": sc["keyAlias"]}
        spw, kpw = sc["storePassword"], sc["keyPassword"]
        out["storePasswordEnv"] = spw.v if isinstance(spw, Sp) and spw.kind == "env" and spw.v else "APKFORGE_STORE_PASS"
        if isinstance(kpw, Sp) and kpw.kind == "env" and kpw.v:
            out["keyPasswordEnv"] = kpw.v
        if sc["v1"] is True:
            mod.cfg["v1Signing"] = True
        if sc["v4"] is True:
            mod.cfg["v4Signing"] = True                        # `build` then writes <apk>.idsig beside every APK (incremental install over adb)
        mod.cfg["signing"] = out
        literal = [k for k, v in (("storePassword", spw), ("keyPassword", kpw)) if isinstance(v, str) and not isinstance(v, Unres)]
        key_part = ("; signing.keyPasswordEnv = \"%s\" (export that variable too)" % out["keyPasswordEnv"]) if "keyPasswordEnv" in out else \
            "; signing.keyPasswordEnv is NOT written: apkforge reads APKFORGE_KEY_PASS and falls back to the store password when it is unset"
        self.notes.append("NOTE %s: signing config '%s': passwords are NOT copied%s; apkforge.json sets signing.storePasswordEnv = \"%s\" (export that variable before a release build)%s" % (
            mod.rel, name, " (the script holds literal %s: consider rotating it, it is in version control)" % " and ".join(literal) if literal else "",
            out["storePasswordEnv"], key_part))
        self.extra_notes += 1

    def h_compile_options(self, c, mod):
        n = c.name
        if n in ("sourceCompatibility", "targetCompatibility") and not c.block:
            v = self.java_version(self.ev(c.rest))
            if isinstance(v, int):
                mod.java[n] = v
                self.mark(c, "mapped")
            else:
                self.mark(c, "unmapped", "%s is not a Java version after resolution" % n)
        elif n == "encoding":
            self.mark(c, "ignored", "apkforge compiles with UTF-8")
        else:
            self.mark(c, "unmapped", "compileOptions.%s is not read by apkforge" % (n or "(statement)"), cover=True)

    def h_kotlin_options(self, c, mod):
        n = c.name
        if n == "jvmTarget" and not c.block:
            v = self.ev(c.rest)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                v = str(v)
            if isinstance(v, str) and not isinstance(v, Unres) and re.match(r"^(1\.)?\d+$", v):
                mod.cfg["kotlinJvmTarget"] = v[2:] if v.startswith("1.") and v != "1.8" else v
                if v == "1.8":
                    mod.cfg["kotlinJvmTarget"] = "8"
                self.mark(c, "mapped")
            else:
                self.mark(c, "unmapped", "jvmTarget is not a literal")
        elif n == "freeCompilerArgs" and not c.block:
            vals, ok_ = self.strings_of(c.rest)
            if ok_ and all(isinstance(v, str) for v in vals):
                mod.cfg.setdefault("kotlinArgs", [])
                mod.cfg["kotlinArgs"] += vals
                self.mark(c, "mapped")
            else:
                self.mark(c, "unmapped", "freeCompilerArgs are not literal")
        else:
            self.mark(c, "unmapped", "kotlinOptions.%s is not read by apkforge (use kotlinArgs for compiler flags)" % (n or "(statement)"), cover=True)

    def h_kotlin_ext(self, s, mod):
        """top-level kotlin { jvmToolchain(17); compilerOptions { jvmTarget.set(JvmTarget.JVM_17) } }"""
        self.mark(s, "structural")
        for c in s.block or []:
            if c.name == "jvmToolchain":
                v = self.java_version(self.ev(c.rest))
                if isinstance(v, int):
                    mod.java["toolchain"] = v
                    self.mark(c, "mapped")
                else:
                    self.mark(c, "unmapped", "jvmToolchain is not a number")
            elif c.name == "compilerOptions" and c.block is not None:
                self.mark(c, "structural")
                for x in c.block:
                    if x.name in ("jvmTarget.set", "jvmTarget") and (x.op in ("(", "=") or x.rest):
                        v = self.ev(x.rest)
                        if isinstance(v, str) and not isinstance(v, Unres) and re.match(r"^\d+$", v):
                            mod.cfg["kotlinJvmTarget"] = v
                            self.mark(x, "mapped")
                        else:
                            self.mark(x, "unmapped", "jvmTarget is not a literal")
                    elif x.name in ("freeCompilerArgs.add", "freeCompilerArgs.addAll", "freeCompilerArgs") and x.rest:
                        vals, ok_ = self.strings_of(x.rest)
                        if ok_ and all(isinstance(v, str) for v in vals):
                            mod.cfg.setdefault("kotlinArgs", [])
                            mod.cfg["kotlinArgs"] += vals
                            self.mark(x, "mapped")
                        else:
                            self.mark(x, "unmapped", "freeCompilerArgs are not literal")
                    else:
                        self.mark(x, "unmapped", "kotlin.compilerOptions.%s is not read by apkforge" % (x.name or "(statement)"), cover=True)
            else:
                self.mark(c, "unmapped", "kotlin.%s is not read by apkforge" % (c.name or "(statement)"), cover=True)

    def arg_text(self, toks):
        """one argument of arg(k, v) -> its text (a string, boolean or number) or None; "$projectDir" is kept as the builder's ${projectDir} token"""
        self.proj_dir_token = True
        try:
            v = self.ev(toks)
        finally:
            self.proj_dir_token = False
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, str) and not isinstance(v, Unres):
            return v
        return None

    def h_proc_arg(self, c, mod, key, what):
        """arg("k", "v") inside ksp { } / kapt { arguments { } } -> cfg[key][k] = v"""
        parts = split_commas(c.rest)
        k = v = None
        if len(parts) == 2 and not c.after and not c.block:
            k, v = self.arg_text(parts[0]), self.arg_text(parts[1])
        if k is None or v is None:
            self.mark(c, "unmapped", "%s.arg is not arg(\"name\", \"value\") with two literal values (a flag without a value, a provider, a variable or a path built at run time cannot be written to apkforge.json)" % what, cover=True)
            return
        if what == "ksp" and (":" in k + v or "=" in k):
            self.mark(c, "unmapped", "ksp option %s=%s cannot be passed on KSP's command line (':' separates the options, '=' the name from the value)" % (k, v), cover=True)
            return
        args = mod.cfg.setdefault(key, {})
        again = k in args and args[k] != v
        args[k] = v
        self.mark(c, "mapped")
        if again:
            self.warn(c, "option %s is set twice: the later value wins" % k)

    def h_ksp(self, s, mod):
        """top-level ksp { arg("k", "v") } -> kspArgs"""
        self.mark(s, "structural")
        for c in s.block or []:
            if c.name == "arg":
                self.h_proc_arg(c, mod, "kspArgs", "ksp")
            else:
                self.mark(c, "unmapped", "ksp.%s is not read by apkforge" % (c.name or "(statement)"), cover=True)

    KAPT_NO_EFFECT = {"correctErrorTypes": "apkforge's kapt always runs with correctErrorTypes = true", "useBuildCache": "Gradle's build cache for the kapt task: no effect on the result",
                      "showProcessorStats": "prints processor statistics only", "mapDiagnosticLocations": "apkforge's kapt always maps diagnostics to the Kotlin sources"}

    def h_kapt(self, s, mod):
        """top-level kapt { arguments { arg("k", "v") } } -> kaptArgs"""
        self.mark(s, "structural")
        for c in s.block or []:
            if c.name == "arguments" and c.block is not None:
                self.mark(c, "structural")
                for x in c.block:
                    if x.name == "arg":
                        self.h_proc_arg(x, mod, "kaptArgs", "kapt")
                    else:
                        self.mark(x, "unmapped", "kapt.arguments.%s is not read by apkforge" % (x.name or "(statement)"), cover=True)
            elif c.name in self.KAPT_NO_EFFECT and not c.block:
                self.mark(c, "ignored", self.KAPT_NO_EFFECT[c.name])
            else:
                self.mark(c, "unmapped", "kapt.%s is not read by apkforge" % (c.name or "(statement)"), cover=True)

    def h_build_feature(self, c, mod):
        n = c.name
        v = self.ev(c.rest) if not c.block else None
        if n == "buildConfig" and isinstance(v, bool):
            mod.cfg["buildConfig"] = v
            self.mark(c, "mapped")
        elif n in ("viewBinding", "compose") and v is True:
            mod.cfg[n] = True                                  # apkforge.json "viewBinding": true / "compose": true (the builder generates the *Binding classes / runs the Compose compiler plugin)
            self.mark(c, "mapped")
        elif n in ("viewBinding", "dataBinding", "compose", "aidl", "renderScript", "prefab", "mlModelBinding") and isinstance(v, bool):
            if v:
                why = {"dataBinding": "data binding generates classes that apkforge does not"}.get(n, "buildFeatures.%s generates code that apkforge does not" % n)
                self.mark(c, "unmapped", why)
            else:
                self.mark(c, "ignored", "feature switched off")
        elif n in ("resValues", "shaders") and isinstance(v, bool):
            self.mark(c, "ignored", "no effect on what apkforge builds")
        else:
            self.mark(c, "unmapped", "buildFeatures.%s is not understood" % (n or "(statement)"), cover=True)

    def h_aapt(self, c, mod):
        n = c.name
        if n == "noCompress" and not c.block:
            vals, ok_ = self.strings_of(c.rest)
            if ok_ and all(isinstance(v, str) for v in vals):
                nc = mod.cfg.setdefault("noCompress", [])
                nc += [v.lstrip(".") for v in vals if v.lstrip(".") not in nc]
                self.mark(c, "mapped")
                return
            self.mark(c, "unmapped", "noCompress arguments are not literal")
            return
        self.mark(c, "unmapped", "%s.%s is not read by apkforge" % ("aaptOptions", n or "(statement)"), cover=True)

    def h_source_set(self, c, mod):
        name = c.name
        if name in ("getByName", "named", "create", "maybeCreate") and c.rest:
            v = self.ev(c.rest)
            name = v if isinstance(v, str) and not isinstance(v, Unres) else None
        if name in ("test", "androidTest", "testFixtures"):
            self.mark(c, "ignored", "tests are not built by apkforge", cover=True)
            return
        if name != "main" or c.block is None:
            self.mark(c, "unmapped", "source set '%s': apkforge builds the main source set only" % (name or "?"), cover=True)
            return
        self.mark(c, "structural")
        for x in c.block:
            self.h_main_source_set(x, mod)

    def h_main_source_set(self, x, mod):
        parts = x.name.split(".")
        kind = parts[0]
        verb = ".".join(parts[1:])
        if x.block is not None and len(parts) == 1 and kind in ("java", "kotlin", "res", "assets"):        # java { srcDirs 'a' }
            self.mark(x, "structural")
            for y in x.block:
                self.set_dirs(y, kind, y.name, mod)
            return
        if kind in ("java", "kotlin", "res", "assets", "manifest") and verb:
            self.set_dirs(x, kind, verb, mod)
        else:
            self.mark(x, "unmapped", "sourceSets.main.%s is not read by apkforge" % (x.name or "(statement)"), cover=True)

    def set_dirs(self, x, kind, verb, mod):
        if kind == "manifest" or verb in ("srcFile",) and kind == "manifest":
            v = self.ev(x.rest)
            if isinstance(v, Sp) and v.kind == "file":
                v = v.v
            if isinstance(v, str) and not isinstance(v, Unres):
                mod.src_dirs["manifest"] = v
                self.mark(x, "mapped")
            else:
                self.mark(x, "unmapped", "manifest path is not a literal")
            return
        if verb in ("srcDirs", "setSrcDirs", "srcDir"):
            vals = []
            for part in split_commas(x.rest):
                v = self.ev(part)
                for e in (v if isinstance(v, list) else [v]):
                    if isinstance(e, Sp) and e.kind == "file":
                        e = e.v
                    if not isinstance(e, str) or isinstance(e, Unres):
                        self.mark(x, "unmapped", "source directory is not a literal path")
                        return
                    vals.append(e)
            add = verb == "srcDir"
            cur = mod.src_dirs[kind]
            mod.src_dirs[kind] = ((cur if cur is not None else ["+default"]) + vals) if add else vals
            self.mark(x, "mapped")
        else:
            self.mark(x, "unmapped", "sourceSets.main.%s.%s is not read by apkforge" % (kind, verb or "(statement)"), cover=True)

    # ---------------------------------------------------------------- module assembly
    def finish_module(self, mod):
        cfg = mod.cfg
        cfg["type"] = mod.kind if mod.kind in ("app", "library") else "app"
        # java level
        j = mod.java
        if "targetCompatibility" in j or "sourceCompatibility" in j:
            cfg["java"] = j.get("targetCompatibility", j.get("sourceCompatibility"))
            if "targetCompatibility" in j and "sourceCompatibility" in j and j["targetCompatibility"] != j["sourceCompatibility"]:
                self.notes.append("NOTE %s: sourceCompatibility %s differs from targetCompatibility %s; apkforge compiles with one --release level (%s)" % (mod.rel, j["sourceCompatibility"], j["targetCompatibility"], cfg["java"]))
                self.extra_notes += 1
        elif "toolchain" in j:
            cfg["java"] = j["toolchain"]
        # source directories
        sd = mod.src_dirs
        base = os.path.join(mod.dir, "src", "main")

        def dirs(kind, defaults):
            v = sd[kind]
            if v is None:
                return None
            out = []
            for e in v:
                if e == "+default":
                    out += [d for d in defaults if os.path.isdir(os.path.join(mod.dir, d))]
                else:
                    out.append(e)
            return out

        java_like = []
        for kind in ("java", "kotlin"):
            d = dirs(kind, ["src/main/" + kind])
            if d is not None:
                java_like += [x for x in d if x not in java_like]
        if java_like or sd["java"] is not None or sd["kotlin"] is not None:
            extra = []
            if sd["java"] is not None and sd["kotlin"] is None and os.path.isdir(os.path.join(base, "kotlin")):
                extra = ["src/main/kotlin"]
            if sd["kotlin"] is not None and sd["java"] is None and os.path.isdir(os.path.join(base, "java")):
                extra = ["src/main/java"]
            cfg["sourceDirs"] = java_like + [x for x in extra if x not in java_like]
        d = dirs("res", ["src/main/res"])
        if d is not None:
            cfg["resDirs"] = d
        d = dirs("assets", ["src/main/assets"])
        if d is not None:
            cfg["assetDirs"] = d
        if sd["manifest"]:
            cfg["manifest"] = sd["manifest"]
        # dependencies
        deps = {k: v for k, v in mod.deps.items() if v}
        if deps:
            cfg["dependencies"] = deps
        self.apply_signing(mod)
        # identity: the manifest's package is the namespace of legacy projects
        if "namespace" not in cfg:
            man = os.path.join(mod.dir, sd["manifest"]) if sd["manifest"] else os.path.join(base, "AndroidManifest.xml")
            if not os.path.isfile(man):
                man = os.path.join(mod.dir, "AndroidManifest.xml")
            pkg = None
            try:
                pkg = ET.parse(man).getroot().get("package")
            except (OSError, ET.ParseError):
                pass
            if pkg:
                cfg["namespace"] = pkg
                self.notes.append("NOTE %s: namespace taken from package=\"%s\" in the manifest (the DSL has none)" % (mod.rel, pkg))
                self.extra_notes += 1
            elif cfg.get("applicationId"):
                self.notes.append("DEFAULT APPLIES %s: namespace not found; apkforge uses the applicationId (%s) as the R package, which differs from Gradle when the sources use another package" % (mod.rel, cfg["applicationId"]))
            else:
                self.notes.append("DEFAULT APPLIES %s: namespace not found (no DSL value, no manifest package): set \"namespace\" in apkforge.json" % mod.rel)
        for key in ("minSdk", "targetSdk", "compileSdk", "versionCode", "versionName") if cfg["type"] == "app" else ("minSdk", "compileSdk"):
            if key not in cfg:
                self.default_note(mod, key)
        if cfg["type"] == "app" and "applicationId" not in cfg:
            self.notes.append("DEFAULT APPLIES %s: applicationId not found; apkforge uses the namespace" % mod.rel)
        order = ["type", "namespace", "applicationId", "minSdk", "targetSdk", "compileSdk", "versionCode", "versionName", "java", "kotlinJvmTarget", "kotlinArgs", "buildConfig",
                 "compose", "serialization", "parcelize", "viewBinding", "kaptArgs", "kspArgs", "minify", "proguardFiles", "consumerProguardFiles", "abis", "native", "noCompress", "placeholders", "buildConfigFields", "resValues", "signing", "v1Signing", "v4Signing",
                 "sourceDirs", "resDirs", "assetDirs", "manifest", "dependencies"]
        out = {k: cfg[k] for k in order if k in cfg}
        out.update({k: v for k, v in cfg.items() if k not in out})
        return out


def _find(root, *names):
    for n in names:
        p = os.path.join(root, n)
        if os.path.isfile(p):
            return p
    return None


def import_project(path, write=True, force=False):
    root = os.path.abspath(path)
    if not os.path.isdir(root):
        raise ForgeError("%s is not a directory" % path)
    settings = _find(root, "settings.gradle.kts", "settings.gradle")
    rootbuild = _find(root, "build.gradle.kts", "build.gradle")
    if not settings and not rootbuild:
        raise ForgeError("no settings.gradle(.kts) or build.gradle(.kts) in %s: not a Gradle project" % root)
    out_file = os.path.join(root, "apkforge.json")
    if write and os.path.exists(out_file) and not force:
        raise ForgeError("%s already exists (use --force to overwrite it, or --dry-run to look at the result first)" % out_file)
    imp = Importer(root)
    imp.load_properties()
    for cdir in (os.path.join(root, "gradle"),):
        default = os.path.join(cdir, "libs.versions.toml")
        if os.path.isfile(default):
            imp.load_catalog("libs", default)
    # ---- settings
    modules_decl = []                                          # [(gradle path, projectDir or None)]
    if settings:
        f = imp.read_file(settings)
        imp.cur = f
        imp.mod = None
        modules_decl = _read_settings(imp, f)
    imp.module_paths = [m[0] for m in modules_decl]
    # ---- root script
    rootvars = {}
    root_file = None
    if rootbuild:
        root_file = imp.read_file(rootbuild)
        imp.cur = root_file
        imp.collect_vars(root_file.stmts, rootvars)
        imp.vars.update(rootvars)
    flat = False
    root_is_module = False
    snap = None
    if root_file is not None:
        imp.cur = root_file
        if not modules_decl:                                   # no module in settings: the root script may BE the module and is then read again by the module reader
            snap = imp.snapshot()
        root_is_module = _read_root_script(imp, root_file, imp.repos)
        if modules_decl:                                       # settings lists modules, so the module reader never reads the root script
            if root_is_module:
                imp.notes.append("NOT MAPPED %s: the root project applies an Android plugin, but settings.gradle includes other modules and apkforge imports only those: the root module is NOT imported (give it its own subproject, e.g. :app, and include it)" % root_file.rel)
            why = "the root project is an Android module that is not imported (settings.gradle includes other modules)" if root_is_module else "the root project is not an Android module: apkforge reads only the modules listed in settings.gradle"
            for b in imp.root_blocks:
                imp.mark(b, "unmapped", "root project's %s { } is not read: %s" % (b.name, why), cover=True)
    if not modules_decl:
        if not root_is_module:
            raise ForgeError("no Android module found in %s: settings.gradle(.kts) includes no module and the root build script does not apply com.android.application / com.android.library" % root)
        flat = True
        modules_decl = [("", None)]
        if snap is not None:                                   # that pass only decided root_is_module: the module reader classifies every statement once
            imp.restore(snap)
    # ---- modules
    results = {}
    for gpath, pdir in modules_decl:
        key = gpath.strip(":")
        rel_dir = pdir if pdir is not None else key.replace(":", "/")
        mdir = root if flat else os.path.normpath(os.path.join(root, rel_dir))
        bf = rootbuild if flat else _find(mdir, "build.gradle.kts", "build.gradle")
        if bf is None:
            imp.notes.append("NOT MAPPED %s: module %s has no build.gradle(.kts) (a container directory?)" % (os.path.relpath(mdir, root).replace(os.sep, "/") or ".", gpath or "(root)"))
            continue
        mod = Module(key, rel_dir, mdir)
        mod.rel = os.path.relpath(bf, root).replace(os.sep, "/")
        if flat:
            f = root_file
            imp.stats["total"] += 0
            mod.vars = {}
        else:
            f = imp.read_file(bf)
            mod.vars = {}
        imp.cur = f
        imp.mod = mod
        imp.collect_vars(f.stmts, mod.vars)
        _read_module_script(imp, f, mod, flat)
        imp.mod = None
        if mod.kind not in ("app", "library"):
            imp.notes.append("NOT MAPPED %s: module %s is not an Android application or library (no com.android.application / com.android.library plugin): it is not imported" % (mod.rel, gpath or "(root)"))
            continue
        results[gpath] = (mod, imp.cur)
    if not results:
        plug = [n for n in imp.notes if "unknown plugin" in n]
        shown = (plug + [n for n in imp.notes if n not in plug])[:8]
        hint = ("\n%d plugin declaration(s) are unknown to apkforge (convention plugins from an included build such as build-logic?): apkforge does not run Gradle, so it cannot see whether one of them applies com.android.application / com.android.library" % len(plug)) if plug else ""
        raise ForgeError("no Android module found in %s (looked for the com.android.application / com.android.library plugin in %d module(s))%s\n%s" % (root, len(modules_decl), hint, "\n".join(shown)))
    # ---- assemble
    built = {}
    for gpath, (mod, f) in results.items():
        imp.cur = f
        cfg = imp.finish_module(mod)
        built[gpath] = (mod, cfg)
    known = {":" + g.strip(":") for g in results}
    for gpath, (mod, cfg) in built.items():
        deps = cfg.get("dependencies", {})
        for p in list(deps.get("projects", [])):
            if p not in known:
                deps["projects"].remove(p)
                imp.notes.append("NOT MAPPED %s: depends on %s, which is not an Android module that was imported (JVM library, missing, or failed): the dependency is dropped" % (mod.rel, p))
        if "projects" in deps and not deps["projects"]:
            del deps["projects"]
        if "dependencies" in cfg and not cfg["dependencies"]:
            del cfg["dependencies"]
    if flat:
        cfg = dict(built[""][1])
        if imp.repos:
            cfg = dict(cfg)
            cfg["repositories"] = imp.repos
        config = cfg
    else:
        config = {}
        if imp.repos:
            config["repositories"] = imp.repos
        config["modules"] = {}
        for gpath, (mod, cfg) in built.items():
            c = dict(cfg)
            default_dir = mod.key.replace(":", "/")
            if mod.rel_dir != default_dir:
                c = dict([("dir", mod.rel_dir)] + list(c.items()))
            config["modules"][mod.key] = c
    st = imp.stats
    accounted = sum(st[k] for k in ("mapped", "ignored", "unmapped", "structural", "covered"))
    if accounted != st["total"]:
        imp.notes.append("INTERNAL import-gradle: %d of %d statements were not classified (a handler missed them); treat the result with suspicion" % (st["total"] - accounted, st["total"]))
    if write:
        with open(out_file, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
            fh.write("\n")
    return {"config": config, "notes": imp.notes, "ignored": imp.ignored, "stats": dict(st), "file": out_file}


def _read_settings(imp, f):
    """-> [(gradle path, projectDir | None)]; classifies every statement of settings.gradle(.kts)"""
    mods, order = {}, []
    for s in f.stmts:
        n = s.name
        if n == "include":
            vals = [imp.ev(x) for x in split_commas(s.rest)]
            ok_ = all(isinstance(v, str) and not isinstance(v, Unres) for v in vals) and vals
            if ok_:
                for v in vals:
                    p = ":" + v.strip(":")
                    if p not in mods:
                        mods[p] = None
                        order.append(p)
                imp.mark(s, "mapped")
            else:
                imp.mark(s, "unmapped", "include arguments are not literal strings")
        elif n in ("rootProject.name",) and s.op == "=":
            imp.mark(s, "ignored", "the project name does not affect the build")
        elif n in ("pluginManagement", "plugins"):
            imp.mark(s, "ignored", "Gradle plugin resolution: apkforge needs no plugins", cover=True)
        elif n == "dependencyResolutionManagement":
            imp.mark(s, "structural")
            for c in s.block or []:
                if c.name == "repositories":
                    imp.h_repositories(c, imp.repos)
                elif c.name in ("repositoriesMode.set", "repositoriesMode"):
                    imp.mark(c, "ignored", "Gradle's rule about where repositories may be declared")
                elif c.name == "versionCatalogs" and c.block is not None:
                    imp.mark(c, "structural")
                    for cc in c.block:
                        _catalog_decl(imp, cc)
                else:
                    imp.mark(c, "unmapped", "dependencyResolutionManagement.%s is not read" % (c.name or "(statement)"), cover=True)
        elif n == "enableFeaturePreview":
            imp.mark(s, "ignored", "Gradle feature preview (TYPESAFE_PROJECT_ACCESSORS is understood by projects.x references)")
        elif n == "project" and s.op == "(" and s.after and _is(s.after[0], "op", ".") and len(s.after) > 2 and s.after[1].val == "projectDir" and _is(s.after[2], "op", "="):
            path = imp.ev(s.rest)
            v = imp.ev(s.after[3:])
            if isinstance(v, Sp) and v.kind == "file":
                v = v.v
            if isinstance(path, str) and isinstance(v, str) and not isinstance(v, Unres) and not isinstance(path, Unres) and not os.path.isabs(v):
                mods[":" + path.strip(":")] = v
                if ":" + path.strip(":") not in order:
                    order.append(":" + path.strip(":"))
                imp.mark(s, "mapped")
            else:
                imp.mark(s, "unmapped", "projectDir is not a relative literal path")
        elif n == "includeBuild":
            imp.mark(s, "unmapped", "composite builds are not supported: the included build is not imported")
        elif n == "apply":
            imp.mark(s, "unmapped", "`apply from:` pulls in another script that is not read")
        elif s.kind == "decl":
            imp.mark(s, "structural")
        else:
            imp.mark(s, "unmapped", "settings statement not interpreted", cover=True)
    return [(p, mods[p]) for p in order]


def _catalog_decl(imp, c):
    """versionCatalogs { create("libs") { from(files("gradle/libs.versions.toml")) } }"""
    name = None
    if c.name in ("create", "register") and c.rest:
        v = imp.ev(c.rest)
        name = v if isinstance(v, str) and not isinstance(v, Unres) else None
    if not name or c.block is None:
        imp.mark(c, "unmapped", "version catalog declaration not understood", cover=True)
        return
    imp.mark(c, "structural")
    for x in c.block:
        if x.name == "from" and x.rest:
            v = imp.ev(x.rest)
            f = v.v[0] if isinstance(v, Sp) and v.kind == "files" and v.v else None
            if isinstance(f, Sp) and f.kind == "file":
                f = f.v
            if isinstance(f, str) and not isinstance(f, Unres):
                imp.load_catalog(name, os.path.join(imp.root, f))
                imp.mark(x, "mapped")
                continue
        imp.mark(x, "unmapped", "version catalog source is not files(\"...\") with a literal path", cover=True)


def _read_root_script(imp, f, repos):
    """classify the statements of the ROOT build script; returns True when the root project is itself an Android module"""
    root_is_module = False
    dummy = Module("", "", imp.root)
    for s in f.stmts:
        n = s.name
        if s.kind == "decl" or (s.op in ("=", "+=") and (n.startswith(("ext.", "extra", "rootProject.ext", "project.ext")) or n in ("ext", "extra"))) or n in ("ext", "extra"):
            imp.mark(s, "structural")
            if s.block:
                for c in s.block:
                    imp.mark(c, "structural", cover=True)             # the whole nested block is accounted for
        elif n == "plugins" and s.block is not None:
            imp.mark(s, "structural")
            for p in s.block:
                if p.name in ("id", "alias", "kotlin"):
                    pid, apply_false = imp.plugin_id(p.rest if p.op != "(" else p.rest + p.after) if p.name != "kotlin" else (None, False)
                    if p.op == "(" and p.after and any(t.kind == "id" and t.val == "apply" for t in p.after):
                        apply_false = True
                    if apply_false or p.name == "kotlin" and p.after:
                        imp.mark(p, "ignored", "plugin declared with `apply false`: it only fixes a version for the modules")
                    else:
                        imp.classify_plugin(p, pid, dummy, at_root=True)
                        if dummy.kind in ("app", "library"):
                            root_is_module = True
                else:
                    imp.mark(p, "unmapped", "plugin declaration not understood")
        elif n == "apply":
            pos, named = named_args(split_commas(s.rest))
            if "plugin" in named:
                v = imp.ev(named["plugin"])
                pid = v if isinstance(v, str) and not isinstance(v, Unres) else None
                imp.classify_plugin(s, pid, dummy, at_root=True)
                if dummy.kind in ("app", "library"):
                    root_is_module = True
            else:
                imp.mark(s, "unmapped", "`apply from:` pulls in another script that is not read")
        elif n == "buildscript":
            imp.mark(s, "ignored", "build logic (plugin classpath): apkforge needs no Gradle plugins", cover=True)
        elif n in ("allprojects", "subprojects") and s.block is not None:
            imp.mark(s, "structural")
            for c in s.block:
                if c.name == "repositories":
                    imp.h_repositories(c, repos)
                else:
                    imp.mark(c, "unmapped", "%s.%s is not interpreted" % (n, c.name or "(statement)"), cover=True)
        elif n == "repositories":
            imp.h_repositories(s, repos)
        elif n == "android" or n == "dependencies":
            imp.root_blocks.append(s)                         # the caller classifies it: the module reader reads it in flat mode, otherwise it is reported as not read
        elif _is_clean_task(s):
            imp.mark(s, "ignored", "the standard `clean` task (apkforge has `apkforge clean`)", cover=True)
        elif n in ("task", "tasks") or n.startswith("tasks."):
            imp.mark(s, "unmapped", "custom Gradle task: build logic apkforge does not run", cover=True)
        else:
            imp.mark(s, "unmapped", "root script statement not interpreted", cover=True)
    return root_is_module


def _is_clean_task(s):
    t = [x.val for x in s.toks if x.kind == "id" or x.kind == "str"]
    flat = " ".join(str(v) if not isinstance(v, list) else "".join(x[1] for x in v) for v in t)
    return bool(re.match(r"^(task clean|tasks\.register clean|tasks\.register|register clean|tasks\.named clean)\b", flat)) and "clean" in flat and "Delete" in flat


def _read_module_script(imp, f, mod, flat):
    for s in f.stmts:
        n = s.name
        if s.kind == "decl" or (s.op in ("=", "+=") and (n.startswith(("ext.", "extra", "rootProject.ext", "project.ext")) or (not s.block and "." not in n and n not in ("group", "version", "description", "java", "android")))) or n in ("ext", "extra"):
            imp.mark(s, "structural")
            if s.block:
                for c in s.block:
                    imp.mark(c, "structural", cover=True)             # the whole nested block is accounted for
        elif n == "plugins" and s.block is not None:
            imp.h_plugins(s, mod)
        elif n == "apply":
            imp.h_apply(s, mod)
        elif n == "android" and s.block is not None:
            imp.h_android(s, mod)
        elif n == "dependencies" and s.block is not None:
            imp.h_dependencies(s, mod)
        elif n == "repositories":
            imp.h_repositories(s, imp.repos)
        elif n == "kotlin" and s.block is not None:
            imp.h_kotlin_ext(s, mod)
        elif n == "kapt" and s.block is not None:
            imp.h_kapt(s, mod)
        elif n == "ksp" and s.block is not None:
            imp.h_ksp(s, mod)
        elif n in ("group", "version") and s.op == "=" and not s.block:
            v = imp.ev(s.rest)
            if isinstance(v, str) and not isinstance(v, Unres):
                mod.cfg[n] = v
                imp.mark(s, "mapped")
            else:
                imp.mark(s, "unmapped", "%s is not a literal" % n)
        elif n in ("description",):
            imp.mark(s, "ignored", "metadata")
        elif n == "buildscript":
            imp.mark(s, "ignored", "build logic (plugin classpath): apkforge needs no Gradle plugins", cover=True)
        elif n in ("allprojects", "subprojects") and s.block is not None:        # the root reader's branch, needed here in flat mode
            imp.mark(s, "structural")
            for c in s.block:
                if c.name == "repositories":
                    imp.h_repositories(c, imp.repos)
                else:
                    imp.mark(c, "unmapped", "%s.%s is not interpreted" % (n, c.name or "(statement)"), cover=True)
        elif _is_clean_task(s):
            imp.mark(s, "ignored", "the standard `clean` task (apkforge has `apkforge clean`)", cover=True)
        elif n in ("task", "tasks") or n.startswith("tasks."):
            imp.mark(s, "unmapped", "custom Gradle task: build logic apkforge does not run", cover=True)
        elif n in ("java",) and s.block is not None:
            imp.mark(s, "unmapped", "java { } block of a JVM module is not read", cover=True)
        else:
            imp.mark(s, "unmapped", "statement not interpreted (%s)" % ("conditional / custom build logic" if n in ("if", "else", "for", "while", "try", "afterEvaluate", "configurations", "android.applicationVariants") else "unknown to apkforge"), cover=True)
