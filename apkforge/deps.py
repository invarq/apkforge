"""Dependencies: the Artifact model, AAR/JAR unpacking and a Maven resolver that reads POMs and Gradle module metadata.

Repository order: vendor/ (Maven layout, committed with the project) -> ~/.m2 -> "repositories" of apkforge.json ->
Google Maven and Maven Central.  A restricted network may not reach the last two, so everything a
project needs can be vendored once (`apkforge vendor`, run on a connected machine) and is then built fully offline.
Everything fetched is cached under ~/.apkforge/m2.

Resolution: POM parents, properties, BOM imports, dependencyManagement, exclusions, optional/test/provided skipped,
Gradle-style highest version wins.  Gradle module metadata (<lib>.module) is read FIRST when a library publishes it: that is how
Kotlin Multiplatform libraries (androidx.annotation/collection/lifecycle, Compose, coroutines, okhttp 5, coil 3 ...) say that the
Android or JVM artifact lives in another module (`available-at`), which variant an Android app (or a JVM annotation processor)
consumes, and which versions of other libraries they require (`dependencyConstraints`).  A library without .module is read from its POM as before.
"""
import copy
import glob
import json
import os
import posixpath
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile

from . import manifest as M
from . import util
from .util import ForgeError

DEFAULT_REPOS = ["https://dl.google.com/dl/android/maven2", "https://repo1.maven.org/maven2"]
LOCK_NAME = "apkforge.lock.json"          # vendor/apkforge.lock.json: sha256 of every vendored file, written by `apkforge vendor`


class Artifact:
    """Everything a dependency (AAR, JAR or library module) contributes to a build."""

    def __init__(self, name, label=None, package=None):
        self.name, self.label, self.package = name, label or name, package
        self.jars, self.res, self.assets, self.jni, self.proguard = [], [], [], [], []
        self.manifest = None
        self.deps = []
        self.module = None
        self.version = None

    def fingerprint(self):
        h = [self.label, self.package]
        h += [util.sha256_file(j) for j in self.jars]
        h += [util.tree_hash(d) for d in self.res + self.assets + self.jni]
        h += [util.sha256_file(p) for p in self.proguard + ([self.manifest] if self.manifest else []) if os.path.isfile(p)]
        return util.sha256_bytes(util.canon(h).encode())


def closure(roots):
    """Roots and everything they depend on: depth first, each artifact once (highest priority first)."""
    out, seen = [], set()

    def visit(a):
        if a.name in seen:
            return
        seen.add(a.name)
        out.append(a)
        for d in a.deps:
            visit(d)
    for r in roots:
        visit(r)
    return out


def _safe_extract(zf, dest):
    root = os.path.realpath(dest)
    for n in zf.namelist():
        p = os.path.realpath(os.path.join(dest, n))
        if not (p == root or p.startswith(root + os.sep)):
            raise ForgeError("unsafe path in archive: %s" % n)
    zf.extractall(dest)


def from_aar_dir(d, name, label):
    a = Artifact(name, label)
    mf = os.path.join(d, "AndroidManifest.xml")
    if os.path.isfile(mf):
        a.manifest = mf
        try:
            a.package = M.package_of(M.parse(open(mf, encoding="utf-8").read(), mf))
        except Exception:
            pass
    cj = os.path.join(d, "classes.jar")
    a.jars = ([cj] if os.path.isfile(cj) else []) + sorted(glob.glob(os.path.join(d, "libs", "*.jar")))
    for attr, sub in (("res", "res"), ("assets", "assets"), ("jni", "jni")):
        p = os.path.join(d, sub)
        if os.path.isdir(p) and os.listdir(p):
            getattr(a, attr).append(p)
    pg = os.path.join(d, "proguard.txt")
    if os.path.isfile(pg):
        a.proguard.append(pg)
    return a


def unpack_aar(path, unpack_root, name, label):
    d = os.path.join(unpack_root, util.sha256_file(path)[:16])
    if not os.path.isfile(os.path.join(d, ".done")):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        try:
            with zipfile.ZipFile(path) as z:
                _safe_extract(z, d)
        except zipfile.BadZipFile:
            raise ForgeError("%s is not a valid AAR (zip) file" % path)
        open(os.path.join(d, ".done"), "w").close()
    return from_aar_dir(d, name, label)


def from_jar(path, name=None, label=None):
    a = Artifact(name or os.path.basename(path), label or os.path.basename(path))
    a.jars = [path]
    return a


def local_file(path, unpack_root):
    """A local .jar or .aar given in dependencies.files / libs/."""
    if path.lower().endswith(".aar"):
        return unpack_aar(path, unpack_root, "file:" + os.path.basename(path), os.path.basename(path))
    return from_jar(path, "file:" + os.path.basename(path))


# ---------------------------------------------------------------------------------------------------------- versions
_QUAL = {"snapshot": -4, "alpha": -3, "a": -3, "beta": -2, "b": -2, "milestone": -2, "m": -2, "rc": -1, "cr": -1,
         "final": 0, "ga": 0, "release": 0, "": 0}


def vkey(v):
    toks = []
    for p in re.split(r"[.\-_+]", v or ""):
        toks.append((1, int(p), "") if p.isdigit() else (0, _QUAL.get(p.lower(), 1), p.lower()))
    while toks and toks[-1] == (0, 0, "") or (toks and toks[-1] == (1, 0, "")):
        toks.pop()
    return toks


def vmax(*vs):
    vs = [v for v in vs if v]
    return max(vs, key=vkey) if vs else None


def clean_version(v):
    """Version ranges: [1.0,2.0) -> 1.0 ; strict/prefer syntax from Gradle strings is not supported here."""
    if v and v[0] in "[(":
        return re.split(r"[,\])]", v[1:])[0].strip() or None
    return v


# ---------------------------------------------------------------------------------------------------------- POM model
def _ln(t):
    return t.rsplit("}", 1)[-1]


def _kid(el, name):
    if el is None:
        return None
    for c in el:
        if _ln(c.tag) == name:
            return c
    return None


def _kids(el, name):
    return [c for c in el if _ln(c.tag) == name] if el is not None else []


def _txt(el, name, default=None):
    c = _kid(el, name)
    return (c.text or "").strip() if c is not None and c.text is not None else default


class Pom:
    def __init__(self, mv, g, a, v, path):
        self.mv, self.path = mv, path
        try:
            self.root = ET.parse(path).getroot()
        except ET.ParseError as e:
            raise ForgeError("%s: invalid POM (%s)" % (path, e))
        par = _kid(self.root, "parent")
        pg = pa = pv = None
        self.parent = None
        if par is not None:
            pg, pa, pv = _txt(par, "groupId"), _txt(par, "artifactId"), _txt(par, "version")
            self.parent = mv.pom(pg, pa, pv)
        self.g = _txt(self.root, "groupId") or pg or g
        self.a = _txt(self.root, "artifactId") or a
        self.v = _txt(self.root, "version") or pv or v
        self.packaging = _txt(self.root, "packaging", "jar")
        self.props = dict(self.parent.props) if self.parent else {}
        pr = _kid(self.root, "properties")
        for c in (pr if pr is not None else []):
            self.props[_ln(c.tag)] = (c.text or "").strip()
        self.props.update({"project.groupId": self.g, "project.artifactId": self.a, "project.version": self.v,
                           "pom.version": self.v, "version": self.v, "project.parent.version": pv or self.v,
                           "project.parent.groupId": pg or self.g})
        self._managed = None

    def sub(self, s, depth=0):
        if s is None or "${" not in s or depth > 8:
            return s
        return self.sub(re.sub(r"\$\{([^}]+)\}", lambda m: self.props.get(m.group(1), m.group(0)), s), depth + 1)

    @staticmethod
    def _excl(d):
        ex = _kid(d, "exclusions")
        return {(_txt(e, "groupId", "*"), _txt(e, "artifactId", "*")) for e in _kids(ex, "exclusion")}

    def managed(self):
        """{(g, a): version} from dependencyManagement, parents and imported BOMs."""
        if self._managed is not None:
            return self._managed
        m = dict(self.parent.managed()) if self.parent else {}
        dm = _kid(_kid(self.root, "dependencyManagement"), "dependencies")
        for d in _kids(dm, "dependency"):
            g, a, v = (self.sub(_txt(d, x)) for x in ("groupId", "artifactId", "version"))
            if _txt(d, "scope") == "import" and _txt(d, "type") == "pom":
                for k, val in self.mv.pom(g, a, v).managed().items():
                    m.setdefault(k, val)
            elif v:
                m[(g, a)] = v
        self._managed = m
        return m

    def deps(self):
        out = []
        for d in _kids(_kid(self.root, "dependencies"), "dependency"):
            g, a, v = (self.sub(_txt(d, x)) for x in ("groupId", "artifactId", "version"))
            if _txt(d, "scope", "compile") in ("test", "provided", "system", "import") or _txt(d, "optional") == "true":
                continue
            if _txt(d, "type", "jar") == "pom":
                continue
            out.append({"g": g, "a": a, "v": clean_version(v or self.managed().get((g, a))), "excl": self._excl(d)})
        return out

# ------------------------------------------------------------------------------------------------ Gradle module metadata
# The attributes of an Android app's runtime classpath (and of a JVM annotation processor's) that decide which variant of a library is taken.
# Documentation / sources / metadata variants and every non-JVM Kotlin platform are out;
# androidJvm beats jvm beats unmarked.  A variant is {name, attributes, dependencies, dependencyConstraints, files, available-at, capabilities}.
_NON_JVM_PLATFORMS = ("native", "js", "wasm", "common")
BUILD_TYPE_ATTR = "com.android.build.api.attributes.BuildTypeAttr"


def variant_key(var, target="android", build_type="debug", java=17):
    """Sort key of one variant for this consumer (smaller is better), or None when the consumer cannot use it at all."""
    at = var.get("attributes") or {}
    if at.get("org.gradle.category", "library") != "library" or "org.gradle.docstype" in at:
        return None
    usage = at.get("org.gradle.usage")
    if usage not in (None, "java-runtime", "java-api"):
        return None                                   # kotlin-api / kotlin-runtime / kotlin-metadata / library-version-metadata ...
    pt = at.get("org.jetbrains.kotlin.platform.type")
    env = at.get("org.gradle.jvm.environment")
    elem = at.get("org.gradle.libraryelements")
    if pt in _NON_JVM_PLATFORMS or env == "non-jvm" or elem not in (None, "jar", "aar"):
        return None
    if target == "jvm":                               # annotation processors, KSP processors: a plain JVM consumer
        plat, envr, elr = ({"jvm": 0, None: 1}.get(pt, 3), {"standard-jvm": 0, None: 1}.get(env, 3), {"jar": 0, None: 1}.get(elem, 3))
    else:                                             # an Android app
        plat, envr, elr = ({"androidJvm": 0, "jvm": 1, None: 2}.get(pt, 4), {"android": 0, None: 1, "standard-jvm": 2}.get(env, 4),
                           {"aar": 0, None: 1, "jar": 1}.get(elem, 4))
    u = {"java-runtime": 0, None: 1, "java-api": 2}[usage]
    bt = at.get(BUILD_TYPE_ATTR)
    btr = 0 if bt is None or bt == build_type else (1 if bt == "release" else 2)
    try:
        jv = int(at["org.gradle.jvm.version"])
    except (KeyError, TypeError, ValueError):
        jv = None
    jr = 1 if jv is None else (java - jv if jv <= java else 100 + jv)
    return (u, plat, envr, elr, btr, jr)


def pick_variant(mod, target="android", build_type="debug", java=17):
    """The variant of a module-metadata document that this consumer takes, or None (no variant fits: the caller falls back to the POM)."""
    best, best_key = None, None
    for i, var in enumerate(mod.get("variants") or []):
        k = variant_key(var, target, build_type, java)
        if k is not None and (best_key is None or k + (i,) < best_key):
            best, best_key = var, k + (i,)
    return best


def _gmm_version(d):
    v = d.get("version")
    if isinstance(v, dict):
        v = v.get("requires") or v.get("strictly") or v.get("prefers")
    return clean_version(v) if isinstance(v, str) and v.strip() and "${" not in v else None


def _gmm_dep(d):
    return {"g": d["group"], "a": d["module"], "v": _gmm_version(d),
            "excl": frozenset((e.get("group") or "*", e.get("module") or "*") for e in (d.get("excludes") or []))}


def _is_platform(d):
    return (d.get("attributes") or {}).get("org.gradle.category") in ("platform", "enforced-platform")


class Info:
    """What one g:a:v contributes to a resolution: edges, version constraints, files. source: "module" (Gradle metadata) | "pom"."""

    def __init__(self, source, deps=(), constraints=None, files=None, boms=(), redirect=None):
        self.source, self.deps, self.constraints = source, list(deps), constraints or {}
        self.files, self.boms, self.redirect = files, list(boms), redirect


class Maven:
    def __init__(self, repos, cache, offline=False, vendor_dir=None):
        self.repos = [self._norm(r) for r in repos]
        self.cache, self.offline = cache, offline
        self.vendor_dir, self.lock = vendor_dir, {}
        if vendor_dir:
            try:
                with open(os.path.join(vendor_dir, LOCK_NAME)) as f:
                    self.lock = json.load(f).get("files", {})
            except (OSError, ValueError):
                self.lock = {}
        self.tried, self.used = [], {}
        self._poms, self._miss, self._mods = {}, set(), {}

    @staticmethod
    def _norm(r):
        if isinstance(r, dict):
            r = r.get("url") or r.get("path")
        r = str(r)
        return r.rstrip("/") if re.match(r"^[a-z]+://", r) else os.path.abspath(os.path.expanduser(r))

    @staticmethod
    def rel(g, a, v, ext, classifier=None):
        return "%s/%s/%s/%s-%s%s.%s" % (g.replace(".", "/"), a, v, a, v, "-" + classifier if classifier else "", ext)

    def fetch(self, rel, quiet=False, sha256=None):
        """Local path of a repository-relative file, or None. Local repositories are read in place.
        quiet: an optional file (.module): a miss is not an error, is not reported in the "last errors" of a later failure, and a
               404 from every repository is remembered in the cache so later builds do not ask again.
        sha256: the checksum the library's own metadata publishes for the file; enforced on a download."""
        local = os.path.join(self.cache, rel)
        if os.path.isfile(local):
            self.used[rel] = local
            return local
        if rel in self._miss:
            return None
        remote_404, remote_other = 0, 0
        for r in self.repos:
            if r.startswith("file://") or not re.match(r"^[a-z]+://", r):
                p = os.path.join(r[7:] if r.startswith("file://") else r, rel)
                if os.path.isfile(p):
                    self._check_lock(rel, p)
                    self.used[rel] = p
                    return p
            elif not self.offline:
                if quiet and os.path.isfile(local + ".404"):
                    remote_404 += 1
                    continue
                try:
                    os.makedirs(os.path.dirname(local), exist_ok=True)
                    util.download(r + "/" + rel, local, sha256=sha256, retries=1, timeout=30)
                    self.used[rel] = local
                    return local
                except ForgeError as e:
                    msg = str(e).splitlines()[0]
                    if "HTTP Error 404" in msg:
                        remote_404 += 1
                    else:
                        remote_other += 1
                    if not quiet or "HTTP Error 404" not in msg:
                        self.tried.append("%s: %s" % (r, msg[:100]))
        if quiet and remote_404 and not remote_other and not self.offline:
            try:
                os.makedirs(os.path.dirname(local), exist_ok=True)
                open(local + ".404", "w").close()
            except OSError:
                pass
        self._miss.add(rel)
        return None

    def _check_lock(self, rel, path):
        """A vendored file must still be the file `apkforge vendor` recorded."""
        want = self.lock.get(rel)
        if want and self.vendor_dir and os.path.abspath(path).startswith(os.path.abspath(self.vendor_dir) + os.sep) and util.sha256_file(path) != want:
            raise ForgeError("vendor/%s does not match vendor/%s (it changed after `apkforge vendor`).\n  If the change is intended, run `apkforge vendor` again to record it." % (rel, LOCK_NAME))

    def pom(self, g, a, v):
        k = (g, a, v)
        if k not in self._poms:
            p = self.fetch(self.rel(g, a, v, "pom"))
            if not p:
                raise ForgeError(self.not_found("%s:%s:%s (pom)" % k))
            self._poms[k] = Pom(self, g, a, v, p)
        return self._poms[k]

    def module(self, g, a, v):
        """The Gradle module metadata document of g:a:v, or None when the library does not publish one (or it is unreadable)."""
        k = (g, a, v)
        if k not in self._mods:
            p = self.fetch(self.rel(g, a, v, "module"), quiet=True)
            doc = None
            if p:
                try:
                    with open(p, encoding="utf-8") as f:
                        doc = json.load(f)
                except (OSError, ValueError):
                    doc = None
            self._mods[k] = doc if isinstance(doc, dict) else None
        return self._mods[k]

    def not_found(self, what):
        tried = sorted(set(self.tried))[-3:]
        return ("cannot find %s in any repository.\n  repositories: %s\n  %s\n  A restricted or offline network may block Google Maven / Maven Central: "
                "put the artifact in <project>/vendor/ (Maven layout) or run `apkforge vendor` once on a machine that has internet access."
                % (what, ", ".join(self.repos) or "(none)", ("last errors: " + " | ".join(tried)) if tried else "offline: nothing was fetched" if self.offline else "no network attempt succeeded"))


def _vparts(v):
    m = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", v or "")
    return tuple(int(x or 0) for x in m.groups()) if m else (0, 0, 0)


class Resolver:
    """Dependency graph -> one Artifact per group:artifact. target: "android" (an app's classpath: AARs, androidJvm variants) or
    "jvm" (annotation processors and other tools that run on a plain JVM: jars, jvm variants)."""

    def __init__(self, maven, unpack_root, build_type="debug", java=17):
        self.mv, self.unpack_root = maven, unpack_root
        self.build_type, self.java = build_type, int(java or 17)
        self._arts, self._info, self._avail = {}, {}, {}
        self.target = "android"
        self.skipped = {}               # (g, a) -> (wanted version, version used): an alignment constraint whose version no repository has

    # ------------------------------------------------------------------------------------------------ one module
    def info(self, g, a, v):
        k = (self.target, g, a, v)
        if k not in self._info:
            mod = self.mv.module(g, a, v)
            inf = self._from_module(g, a, v, mod) if mod else None
            self._info[k] = inf if inf is not None else Info("pom", self.mv.pom(g, a, v).deps())
        return self._info[k]

    def _from_module(self, g, a, v, mod):
        var = pick_variant(mod, self.target, self.build_type, self.java)
        if var is None:
            return None
        base = posixpath.dirname(Maven.rel(g, a, v, "module"))
        deps, boms, cons, files = [], [], {}, []
        redirect = None
        at = var.get("available-at") or {}
        if at.get("group") and at.get("module") and at.get("version"):
            redirect = (at["group"], at["module"], at["version"])
            deps.append({"g": redirect[0], "a": redirect[1], "v": redirect[2], "excl": frozenset()})
        for d in var.get("dependencies") or []:
            if d.get("group") and d.get("module"):
                (boms if _is_platform(d) else deps).append(_gmm_dep(d))
        for c in var.get("dependencyConstraints") or []:
            if c.get("group") and c.get("module") and not _is_platform(c):
                cv = _gmm_version(c)
                if cv:
                    cons[(c["group"], c["module"])] = vmax(cv, cons.get((c["group"], c["module"])))
        for f in var.get("files") or []:
            name = f.get("url") or f.get("name")
            if name and name.lower().endswith((".jar", ".aar")):
                files.append((posixpath.normpath(posixpath.join(base, name)), (f.get("sha256") or "").lower() or None))
        return Info("module", deps, cons, files, boms, redirect)

    def _bom_constraints(self, b, out):
        """A platform dependency (kotlinx-coroutines-bom ...) only constrains versions of libraries that are in the graph anyway."""
        g, a, v = b["g"], b["a"], b["v"]
        if not v:
            return
        got = False
        mod = self.mv.module(g, a, v)
        for var in (mod or {}).get("variants") or []:
            if (var.get("attributes") or {}).get("org.gradle.category") in ("platform", "enforced-platform"):
                for c in var.get("dependencyConstraints") or []:
                    cv = _gmm_version(c)
                    if c.get("group") and c.get("module") and cv:
                        out[(c["group"], c["module"])] = vmax(cv, out.get((c["group"], c["module"])))
                        got = True
                break
        if not got:
            try:
                for k, val in self.mv.pom(g, a, v).managed().items():
                    out[k] = vmax(clean_version(val), out.get(k))
            except ForgeError:
                pass                       # a BOM that is not in any repository only loses the alignment it would have given

    # ------------------------------------------------------------------------------------------------ the graph
    def resolve(self, coords, platforms=(), target="android"):
        """coords: [(group, artifact, version|None)] -> [Artifact] in dependency order (direct first), one per g:a."""
        self.target = target
        managed = {}
        for g, a, v in platforms:
            for k, val in self.mv.pom(g, a, v).managed().items():
                managed.setdefault(k, val)
        selected, tried = {}, set()
        for _ in range(16):
            cons, unv = {}, {}
            order, edges, seen = self._walk(coords, managed, selected, cons, unv)
            new = dict(selected)
            for k, v in seen.items():
                new[k] = vmax(v, new.get(k))
            for k, v in cons.items():
                if k in seen and vkey(v) > vkey(new.get(k)):
                    if self._available(k[0], k[1], v):
                        new[k] = v
                    else:
                        self.skipped[k] = (v, new.get(k))
            if new == selected:
                todo = [k for k in unv if k not in tried]        # a dependency that names no version and that nothing else in the graph versions
                for k in todo:
                    tried.add(k)
                    d = self._pom_default(k, unv[k])
                    if d:
                        new[k] = d
                if new == selected:
                    break
            selected = new
        if unv:
            k = sorted(unv)[0]
            raise ForgeError("no version for %s:%s%s (declare one, or add a platform/BOM that manages it)" % (k[0], k[1], " (required by %s)" % unv[k] if unv[k] else ""))
        arts = {}
        for k in order:
            arts[k] = self._own(self._artifact(k[0], k[1], selected[k]), selected[k])
        for k in order:
            arts[k].deps = [arts[c] for c in edges[k] if c in arts]
        self._prune_stdlib(arts)
        return [arts[k] for k in order]

    def _pom_default(self, k, requirer):
        """The version the POM of the library that asked for k manages it at (core-viewtree's metadata names kotlin-stdlib, its POM pins it)."""
        try:
            rg, ra, rv = requirer.split(":", 2)
            return clean_version(self.mv.pom(rg, ra, rv).managed().get(k))
        except (ForgeError, ValueError):
            return None

    def _available(self, g, a, v):
        """Is g:a:v in some repository? A version CONSTRAINT (alignment inside a library group, a BOM) is advisory: Gradle would fetch the aligned
        version from the network, a vendored kit only holds what a crawl asked for, so a constraint that points at a version nobody has is skipped."""
        k = (g, a, v)
        if k not in self._avail:
            self._avail[k] = bool(self.mv.fetch(Maven.rel(g, a, v, "module"), quiet=True) or self.mv.fetch(Maven.rel(g, a, v, "pom"), quiet=True))
        return self._avail[k]

    def _walk(self, roots, managed, selected, cons, unv):
        order, edges, seen = [], {}, {}
        done, path = set(), []

        def visit(g, a, v, excl):
            if any(eg in (g, "*") and ea in (a, "*") for eg, ea in excl):
                return None
            v = vmax(v, selected.get((g, a)), managed.get((g, a)))
            if not v:
                if not path:
                    raise ForgeError("no version for %s:%s (declare one, or add a platform/BOM that manages it)" % (g, a))
                unv.setdefault((g, a), path[-1])                 # left out of this walk; the next one has a version if the rest of the graph names one
                return None
            seen[(g, a)] = vmax(v, seen.get((g, a)))
            if (g, a) in done:
                return (g, a)
            done.add((g, a))
            order.append((g, a))
            edges[(g, a)] = []
            inf = self.info(g, a, v)
            for k, cv in inf.constraints.items():
                cons[k] = vmax(cv, cons.get(k))
            for b in inf.boms:
                self._bom_constraints(b, cons)
            path.append("%s:%s:%s" % (g, a, v))
            for d in inf.deps:
                c = visit(d["g"], d["a"], d["v"], excl | d["excl"])
                if c:
                    edges[(g, a)].append(c)
            path.pop()
            return (g, a)
        for g, a, v in roots:
            visit(g, a, clean_version(v), frozenset())
        return order, edges, seen

    # ------------------------------------------------------------------------------------------------ files
    def _artifact(self, g, a, v):
        k = (self.target, g, a, v)
        if k in self._arts:
            return self._arts[k]
        inf = self.info(g, a, v)
        name, label = "%s:%s" % (g, a), "%s:%s:%s" % (g, a, v)
        if inf.source == "module":
            paths = []
            for rel, sha in inf.files:
                p = self.mv.fetch(rel, sha256=sha)
                if not p:
                    raise ForgeError(self.mv.not_found("%s (%s)" % (label, posixpath.basename(rel))))
                paths.append(p)
            art = self._combine(name, label, paths)
        else:
            pom = self.mv.pom(g, a, v)
            art = None
            if pom.packaging != "pom":
                for ext in (("aar", "jar") if pom.packaging == "aar" else ("jar", "aar")):
                    p = self.mv.fetch(Maven.rel(g, a, v, ext))
                    if p:
                        art = unpack_aar(p, self.unpack_root, name, label) if ext == "aar" else from_jar(p, name, label)
                        break
                if art is None:
                    raise ForgeError(self.mv.not_found("%s (%s)" % (label, pom.packaging)))
            else:
                art = Artifact(name, label)
        art.version = v
        self._arts[k] = art
        return art

    def _combine(self, name, label, paths):
        """The one Artifact of a variant's files: none (a module that only forwards or only lists dependencies), one jar/aar, or several."""
        arts = [unpack_aar(p, self.unpack_root, name, label) if p.lower().endswith(".aar") else from_jar(p, name, label) for p in paths]
        if not arts:
            return Artifact(name, label)
        base = arts[0]
        for o in arts[1:]:
            base.jars += [j for j in o.jars if j not in base.jars]
            base.res += o.res
            base.assets += o.assets
            base.jni += o.jni
            base.proguard += o.proguard
            base.manifest = base.manifest or o.manifest
            base.package = base.package or o.package
        return base

    @staticmethod
    def _own(art, version):
        """A private copy of a (cached) artifact: its `deps` belong to this resolution, not to the next one that reuses the files."""
        c = copy.copy(art)
        c.deps = []
        return c

    @staticmethod
    def _prune_stdlib(arts):
        """kotlin-stdlib 1.8 absorbed kotlin-stdlib-jdk7/jdk8 (and 1.9.20 the common one): older copies of those jars would define the same classes twice."""
        std = arts.get(("org.jetbrains.kotlin", "kotlin-stdlib"))
        if std is None:
            return
        sv = _vparts(getattr(std, "version", None))
        for n, since in (("kotlin-stdlib-jdk7", (1, 8, 0)), ("kotlin-stdlib-jdk8", (1, 8, 0)), ("kotlin-stdlib-common", (1, 9, 20))):
            a = arts.get(("org.jetbrains.kotlin", n))
            if a is not None and sv >= since:
                a.jars = []


def parse_coord(s):
    """'g:a:v' | 'g:a' (version from a platform) -> (g, a, v|None). A trailing @aar/@jar is ignored."""
    s = s.split("@")[0]
    p = s.split(":")
    if len(p) < 2:
        raise ForgeError("bad dependency %r (expected group:artifact:version)" % s)
    return p[0], p[1], p[2] if len(p) > 2 and p[2] else None
