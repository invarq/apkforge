"""Small shared helpers: logging, running tools, hashing, downloading. Standard library only."""
import hashlib
import http.client
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

VERSION = "1.3.1"
BUILD = ""                # pre-release label shown by --version (for example "rc1"); empty for a release


class ForgeError(Exception):
    """A user-facing failure: the message is printed as is, without a traceback."""


# ----------------------------------------------------------------------------------------------- logging
_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
R, G, Y, B, N = (("\033[31m", "\033[32m", "\033[33m", "\033[1m", "\033[0m") if _COLOR else ("", "", "", "", ""))
_LOGFILE = None
_VERBOSE = False
_CONSOLE = None            # None = stdout; log_to_stderr() switches it
_T0 = time.time()


def set_log(path=None, verbose=False):
    global _LOGFILE, _VERBOSE
    _VERBOSE = verbose
    if _LOGFILE:
        _LOGFILE.close()
        _LOGFILE = None
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        _LOGFILE = open(path, "w", encoding="utf-8")


def _emit(line, to_console=True, to_log=True):
    if to_console:
        print(line, file=_CONSOLE or sys.stdout, flush=True)
    if to_log and _LOGFILE:
        _LOGFILE.write(re.sub(r"\033\[[0-9;]*m", "", line) + "\n")
        _LOGFILE.flush()


def log_to_stderr():
    """Commands that print DATA on stdout (verify, build --json, import-gradle) send every log line to stderr instead,
    so `apkforge verify app.apk > report.json` is valid JSON."""
    global _CONSOLE, R, G, Y, B, N
    _CONSOLE = sys.stderr
    on = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None
    R, G, Y, B, N = (("\033[31m", "\033[32m", "\033[33m", "\033[1m", "\033[0m") if on else ("", "", "", "", ""))


def info(msg):
    _emit("[%5.1fs] %s" % (time.time() - _T0, msg))


def ok(msg):
    _emit("[%5.1fs] %sok%s    %s" % (time.time() - _T0, G, N, msg))


def warn(msg):
    _emit("[%5.1fs] %sWARN%s  %s" % (time.time() - _T0, Y, N, msg))


def detail(msg):
    _emit("         " + msg, to_console=_VERBOSE)


def secret(msg):
    """Like info(), for a value the person has to see once and that must never be stored: it goes to the console only, never to the build log."""
    _emit("[%5.1fs] %s" % (time.time() - _T0, msg), to_log=False)


def stage(name):
    _emit("[%5.1fs] %s== %s%s" % (time.time() - _T0, B, name, N))


# ----------------------------------------------------------------------------------------------- platform
def host_os():
    s = platform.system().lower()
    return "linux" if s.startswith("linux") else "macos" if s == "darwin" else "windows" if s.startswith(("win", "cyg", "msys")) else s


def host_arch():
    m = platform.machine().lower()
    return {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64", "i386": "x86", "i686": "x86"}.get(m, m)


def exe(name):
    return name + ".exe" if host_os() == "windows" else name


def home_dir():
    p = os.environ.get("APKFORGE_HOME") or os.path.join(os.path.expanduser("~"), ".apkforge")
    os.makedirs(p, exist_ok=True)
    return p


def which(name):
    return shutil.which(name)


# ----------------------------------------------------------------------------------------------- hashing
def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_file(path, _cache={}):
    st = os.stat(path)
    k = (os.path.abspath(path), st.st_size, st.st_mtime_ns)
    if k in _cache:
        return _cache[k]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    _cache[k] = h.hexdigest()
    return _cache[k]


def tree_files(root, exts=None):
    out = []
    for d, dirs, files in os.walk(root):
        dirs.sort()
        for f in sorted(files):
            if exts is None or f.lower().endswith(exts):
                out.append(os.path.join(d, f))
    return out


def tree_hash(roots, exts=None):
    """Content hash of one or several directories (relative path + file content), order independent per directory."""
    h = hashlib.sha256()
    if isinstance(roots, str):
        roots = [roots]
    for root in roots:
        h.update(b"\0root\0")
        if not os.path.isdir(root):
            continue
        for p in tree_files(root, exts):
            h.update(os.path.relpath(p, root).replace(os.sep, "/").encode())
            h.update(b"\0")
            h.update(sha256_file(p).encode())
    return h.hexdigest()


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


# ----------------------------------------------------------------------------------------------- processes
# A secret must never reach the build log, the verbose output or an error message. run() is the one place every command line passes through, so it
# guards two ways: (1) the value that follows a password option (-storepass, -passin, --ks-pass ...) is masked in the command line it logs and shows;
# (2) every string in `secrets`, and the value of every PASS / SECRET / TOKEN variable passed in `env`, is replaced by *** in that command line, in the
# tool's logged output and in the error text.
# This is a safety net. A password does not belong on a command line at all (any user on the machine can read it from the process list): pass it
# through `env` (keytool -storepass:env NAME, openssl -passin env:NAME).
_SECRET_OPTION = re.compile(r"^-{1,2}[\w.-]*pass[\w]*$", re.I)       # -storepass  -srcstorepass  -keypass  -passin  --ks-pass  --password ...
_BY_REFERENCE = re.compile(r"^(?:env|file|fd):|^stdin$")              # env:NAME  file:PATH  fd:N  stdin: where the password is kept, not the password
_SECRET_ENV = re.compile(r"PASS|SECRET|TOKEN", re.I)


def _mask_argv(cmd):
    """cmd as it may be shown: the value of every password option becomes ***, and so does a literal openssl-style pass:VALUE."""
    shown, hide_next = [], False
    for a in cmd:
        if hide_next:
            hide_next = False
            shown.append(a if _BY_REFERENCE.match(a) else "***")
            continue
        name, eq, value = a.partition("=")
        if eq and _SECRET_OPTION.match(name):
            shown.append(a if _BY_REFERENCE.match(value) else name + "=***")
        elif _SECRET_OPTION.match(a):
            shown.append(a)
            hide_next = True
        elif a.startswith("pass:"):
            shown.append("pass:***")
        else:
            shown.append(a)
    return shown


def _scrub(text, secrets):
    for s in sorted({s for s in secrets if s}, key=len, reverse=True):      # longest first: a secret may contain another one
        text = text.replace(s, "***")
    return text


# What ONE tool call may put into the build log (and, with -v, onto the console).  `dexdump -n` prints a line per class and ninja one per compile step: a real
# React Native app made a 59 MB build.log in a single build (F7).  The first LOG_HEAD and the last LOG_TAIL lines are kept (a compiler says what went wrong
# at the END), the middle is counted, and a single line is cut at LOG_WIDTH characters.  Only what is LOGGED is clipped: run() still returns the tool's
# whole output, and the error text of a failed tool is built from the whole output too.  APKFORGE_LOG_FULL=1 switches the clipping off (for debugging).
LOG_HEAD, LOG_TAIL, LOG_WIDTH, LOG_BUDGET, LOG_CMDLINE = 120, 80, 2000, 65536, 32000


def _log_full():
    return os.environ.get("APKFORGE_LOG_FULL", "").lower() in ("1", "true", "yes")


def clip_output(text, head=LOG_HEAD, tail=LOG_TAIL, width=LOG_WIDTH, budget=LOG_BUDGET):
    """`text` as it may be logged: at most head + tail lines of at most `width` characters, and at most about `budget` characters; what was left out is counted in a
    marker line.  Text that fits is returned unchanged."""
    lines = text.splitlines()
    n = len(lines)

    def cut(l):
        return l if len(l) <= width else l[:width] + " ... [%d more characters]" % (len(l) - width)
    if n > head + tail:
        gone = lines[head:n - tail]
        kept = [cut(l) for l in lines[:head]] + ["... %d more lines, %d bytes not logged (the first %d and the last %d of %d lines are kept) ..." % (
            len(gone), sum(len(l.encode("utf-8", "replace")) + 1 for l in gone), head, tail, n)] + [cut(l) for l in lines[n - tail:]]
    else:
        kept = [cut(l) for l in lines]
    out = "\n".join(kept)
    if len(out) > budget:
        k = budget // 2
        out = out[:k] + "\n... %d characters not logged (the log keeps about %d characters of one tool call) ...\n" % (len(out) - budget, budget) + out[len(out) - (budget - k):]
    return out


def run(cmd, cwd=None, env=None, check=True, timeout=None, what=None, input_text=None, secrets=(), excerpt=None):
    """Run a command, return (rc, output). On failure with check=True raise ForgeError with the first error lines.
    The returned output is the tool's own; the log and the error text are scrubbed of secrets (see above).
    excerpt: optional function(text) -> the lines of a failed run's output worth showing (or None / [] for the default pick: the lines that look like errors); a tool whose messages
    run over several lines (CMake) says here which ones."""
    cmd = [str(c) for c in cmd]
    hidden = list(secrets) + [v for k, v in (env or {}).items() if v and _SECRET_ENV.search(k)]
    cmdline = _scrub(" ".join(_mask_argv(cmd)), hidden)
    detail("$ " + (cmdline if len(cmdline) <= LOG_CMDLINE or _log_full() else cmdline[:LOG_CMDLINE] + " ... [%d more characters]" % (len(cmdline) - LOG_CMDLINE)))
    e = dict(os.environ)
    for k, v in (env or {}).items():
        if v is None:
            e.pop(k, None)                             # None removes the variable from the tool's environment (native.py scrubs CC, CFLAGS ... this way)
        else:
            e[k] = v
    try:
        p = subprocess.run(cmd, cwd=cwd, env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                           input=input_text.encode() if input_text is not None else None)
    except FileNotFoundError:
        raise ForgeError("%s: command not found: %s" % (what or cmd[0], cmd[0]))
    except subprocess.TimeoutExpired:
        raise ForgeError("%s: timed out after %ss" % (what or cmd[0], timeout))
    out = p.stdout.decode("utf-8", "replace")
    safe = _scrub(out, hidden)                      # what may be logged or shown; `out` itself goes back to the caller untouched
    if safe.strip():
        detail(safe.rstrip() if _log_full() else clip_output(safe.rstrip()))      # the log and the -v console get the clipped text; `out` and the error text below keep everything
    if check and p.returncode != 0:
        lines = [l for l in safe.splitlines() if l.strip()]
        keep = [l for l in lines if re.search(r"error|Error|ERROR|exception|Exception|FAILED|failed|warning: .*not found", l)]
        shown = (excerpt(safe) if excerpt else None) or (keep or lines)[:25]
        raise ForgeError("%s failed (exit %d)\n  command: %s\n%s" % (
            what or os.path.basename(cmd[0]), p.returncode, cmdline[:400], "\n".join("    " + l for l in shown)))
    return p.returncode, out


_NOTICE = re.compile(r"^(NOTE: )?Picked up [A-Z_]+:")


def tool_output(text):
    """Output of a Java tool without the JVM's start-up notices (\"Picked up JAVA_TOOL_OPTIONS: ...\", also _JAVA_OPTIONS and JDK_JAVA_OPTIONS).
    run() merges stderr into stdout, and these lines carry per-machine, per-session values: they must not become a version string or part of a cache key."""
    return "\n".join(l for l in text.splitlines() if not _NOTICE.match(l))


# ----------------------------------------------------------------------------------------------- downloads
# apkforge opens a network connection in exactly four places: download(), fetch_text(), head_ok() and redirect_location() below.
# --offline (set_offline) is enforced HERE, so no caller can forget to check it, and every failure ends as a one-line ForgeError.
_OFFLINE = [False]
_NET_ERRORS = (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException, ValueError)


class OfflineError(ForgeError):
    """A network access that --offline (or APKFORGE_OFFLINE=1) forbids."""


def set_offline(flag=True):
    _OFFLINE[0] = bool(flag)


def is_offline():
    return _OFFLINE[0]


def env_offline():
    return os.environ.get("APKFORGE_OFFLINE", "").lower() in ("1", "true", "yes")


def _redact(url):
    return re.sub(r"(?<=//)[^/@\s]+@", "", str(url))          # never print user:password@ from a repository URL


def _guard(url, label=None):
    if _OFFLINE[0]:
        raise OfflineError("offline mode: not fetching %s%s (--offline / APKFORGE_OFFLINE=1 forbid network access; run once online, or vendor it)"
                           % (_redact(url), " [%s]" % label if label else ""))


def _reason(e):
    if isinstance(e, urllib.error.HTTPError):
        return "HTTP %d %s" % (e.code, e.reason or "")
    if isinstance(e, http.client.IncompleteRead):                   # a chunked body that ended early (http.client raises this one itself)
        return "incomplete download (%d bytes received%s)" % (len(e.partial), ", %d more expected" % e.expected if e.expected else "")
    r = getattr(e, "reason", None) or e
    return " ".join(str(r).split()) or type(e).__name__


def _net_error(url, e, label=None):
    return ForgeError("cannot fetch %s%s: %s" % (_redact(url), " (%s)" % label if label and label != os.path.basename(str(url)) else "", _reason(e).strip()))


def _content_length(v):
    """The announced body length as an int, or None when the header is absent or not a single plain number."""
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def download(url, dest, sha256=None, label=None, retries=3, timeout=60, overridable=True, sha1=None):
    """Download url to dest atomically. Verifies sha256 (and sha1, when given: the Android SDK repository manifest publishes only that one). Returns dest.
    overridable: whether --trust-new can accept a mismatch (True for a pin; False for a checksum the publisher ships with the file).
    A `sha1` is always the publisher's: no flag overrides it."""
    _guard(url, label)
    label = label or os.path.basename(dest)
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    last = None
    for attempt in range(1, retries + 1):
        part = dest + ".part"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "apkforge/" + VERSION})
            h = hashlib.sha256()
            h1 = hashlib.sha1() if sha1 else None
            n = 0
            nxt = 16 << 20
            with urllib.request.urlopen(req, timeout=timeout) as r, open(part, "wb") as f:
                total = r.headers.get("Content-Length")
                for chunk in iter(lambda: r.read(1 << 18), b""):
                    f.write(chunk)
                    h.update(chunk)
                    if h1:
                        h1.update(chunk)
                    n += len(chunk)
                    if n >= nxt:
                        detail("  ... %s %d MB%s" % (label, n >> 20, "/%d" % (int(total) >> 20) if total else ""))
                        nxt += 16 << 20
            want = _content_length(total)
            if want is not None and n != want:                  # http.client's read() does not raise when the server closes early (see its own comment)
                raise OSError("incomplete download: %d of %d bytes received" % (n, want))   # checked BEFORE the checksum: a short body is retried, not blamed on upstream
            if sha256 and h.hexdigest() != sha256:
                os.remove(part)
                raise ForgeError("checksum mismatch for %s\n  expected %s\n  got      %s\n  (%s)" % (label, sha256, h.hexdigest(), (
                    "the pinned file changed upstream; re-run with --trust-new to accept it (the new checksum is printed so you can update pins.json), or pick another source"
                    if overridable else
                    "the checksum published with this file is always enforced and no flag overrides it: retry the download; "
                    "if it keeps failing the file is corrupt or has been tampered with")))
            if sha1 and h1.hexdigest() != sha1.lower():
                os.remove(part)
                raise ForgeError("checksum mismatch for %s\n  expected sha1 %s\n  got      sha1 %s\n  (the checksum published with this file is always enforced and no flag overrides it: "
                                 "retry the download; if it keeps failing the file is corrupt or has been tampered with)" % (label, sha1.lower(), h1.hexdigest()))
            os.replace(part, dest)
            return dest
        except ForgeError:
            raise
        except _NET_ERRORS as e:
            last = e
            if os.path.exists(part):
                os.remove(part)
            if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403, 404, 410):
                break
            if attempt < retries:
                time.sleep(1.5 * attempt)
    raise _net_error(url, last, label)


def head_ok(url, timeout=10):
    if _OFFLINE[0]:
        return 0
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "apkforge/" + VERSION})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def fetch_text(url, timeout=30):
    _guard(url)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "apkforge/" + VERSION})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except _NET_ERRORS as e:
        raise _net_error(url, e)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def redirect_location(url, timeout=20):
    """The Location header of the redirect a HEAD request gets (GitHub's .../releases/latest answers 302 -> .../releases/tag/<tag>);
    None when the server answers without redirecting."""
    _guard(url)
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "apkforge/" + VERSION})
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout):
            return None
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            return e.headers.get("Location", "") or None
        raise _net_error(url, e)
    except _NET_ERRORS as e:
        raise _net_error(url, e)


def safe_extract(archive, dest):
    """Extract a .zip or .tar(.gz/.xz/.bz2), refusing absolute paths, '..' components, device files and links that
    point outside dest (a hostile or corrupt archive must not write outside the toolchain folder).
    A tar member is vetted twice: up front (an obviously hostile archive extracts nothing) and again right before it is extracted,
    against the files its predecessors already created. The second pass matters: a chain of links that each look harmless
    (d1/up -> .. ; esc -> d1/up/.. ; then esc/evil) only escapes once d1/up exists, and the Python 3.12 'data' filter that would catch
    it is missing on older interpreters. If anything is refused or fails, what this call extracted is
    removed again at the top level of dest (a half-unpacked archive must not leave a javac behind for a later run to pick up) and the failure is a ForgeError."""
    import tarfile
    import zipfile
    dest = os.path.abspath(dest)
    os.makedirs(dest, exist_ok=True)
    root = os.path.realpath(dest)
    name_of = os.path.basename(archive)
    before = set(os.listdir(dest))

    def inside(p):
        rp = os.path.realpath(p)
        return rp == root or rp.startswith(root + os.sep)

    def check(n):
        parts = re.split(r"[\\/]", n)
        if n.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", n) or ".." in parts or not inside(os.path.join(dest, n)):
            raise ForgeError("unsafe path in archive %s: %s" % (name_of, n))

    def vet(m):
        check(m.name)
        if m.isdev():
            raise ForgeError("device file in archive %s: %s" % (name_of, m.name))
        if m.issym() or m.islnk():
            base = os.path.dirname(os.path.join(dest, m.name)) if m.issym() else dest
            target = m.linkname if os.path.isabs(m.linkname) else os.path.join(base, m.linkname)
            if not inside(target):
                raise ForgeError("link in archive %s points outside the destination: %s -> %s" % (name_of, m.name, m.linkname))
        return m

    def undo():
        for n in set(os.listdir(dest)) - before:
            p = os.path.join(dest, n)
            if os.path.islink(p) or os.path.isfile(p):
                os.remove(p)
            else:
                shutil.rmtree(p, ignore_errors=True)

    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as zf:
                for n in zf.namelist():
                    check(n)
                zf.extractall(dest)
            return
        with tarfile.open(archive) as tf:
            members = tf.getmembers()
            for m in members:
                vet(m)

            def lazily():                                    # extractall pulls the next member only after the previous one is on disk
                for m in members:
                    yield vet(m)
            tf.extractall(dest, members=lazily(), **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    except ForgeError:
        undo()
        raise
    except Exception as e:                                   # corrupt archive, full disk, a filter refusal ...: always a one-line ForgeError
        undo()
        raise ForgeError("cannot unpack %s: %s: %s" % (name_of, type(e).__name__, " ".join(str(e).split())[:200]))


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return "%d %s" % (n, u) if u == "B" else "%.1f %s" % (n, u)
        n /= 1024.0
