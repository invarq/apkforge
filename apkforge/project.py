"""Project model. apkforge.json (single module or {"modules": {...}}) with Gradle-like layout autodetection."""
import json
import os
import re

from . import native, react, resopt
from .util import ForgeError

DEFAULTS = {
    "type": "app", "minSdk": 24, "targetSdk": 35, "compileSdk": 35, "versionCode": 1, "versionName": "1.0", "java": 17,
    "apiCheck": "error",            # error | warn | off : references to APIs android.jar does not have (JDK-only APIs)
    "minApiCheck": "warn",          # warn | error | off : platform APIs newer than minSdk that no SDK_INT check / @RequiresApi protects
    "elfAlignment": "warn",         # warn | error | off : 64-bit native libraries that are not 16 KB page-size ready (Google Play, targetSdk 35+)
    "v4Signing": False,             # true | false : also write <apk>.idsig (APK Signature Scheme v4) beside each APK, for incremental install over adb; `build --v4 / --no-v4` overrides it
    "optimizeResources": False,     # true | false | {shortenPaths, sparseEncoding, collapseNames, dedupe, keep} : release builds run `aapt2 optimize` (see resopt.py); `build --optimize-resources / --no-optimize-resources` overrides it
    "compactEntries": False,        # true | false : compact resource entries at link time (aapt2 applies them from minSdk 34; an optimized build drops them again)
    "react": False,                 # true | false | {root, entryFile, hermes, hermesFlags, pinCheck, ...} : a React Native app: non-debug builds run Metro and hermesc and put the Hermes bytecode into the APK (see react.py); `build --react / --no-react` overrides it
    "native": False,                # true | false | "path/to/CMakeLists.txt" | {cmake, cmakeVersion, ndkVersion, arguments, cFlags, cppFlags, targets, stl, strip} : build the module's C/C++ with CMake and the NDK for every ABI in "abis" and package the libraries (see native.py)
    "abis": ["arm64-v8a", "armeabi-v7a", "x86_64", "x86"],
    "minify": False, "noCompress": [], "repositories": [], "placeholders": {}, "buildConfigFields": {}, "resValues": {},
    "javacArgs": [], "kotlinArgs": [], "kotlinJvmTarget": None, "buildConfig": None,
    # Kotlin compiler plugins / code generators.  None = automatic (compose: androidx.compose.runtime on the classpath; serialization: kotlinx-serialization-core
    # on the classpath; parcelize: kotlinx.parcelize used in the sources); true / false forces it.
    "compose": None, "serialization": None, "parcelize": None, "viewBinding": False, "kaptArgs": {}, "kspArgs": {},
    # google-services.json -> string resources (what Google's Gradle plugin does) and Crashlytics' mapping id (see gms.py).  None / "auto" = by what the module has (the file and a Firebase or Play
    # services dependency; a firebase-crashlytics dependency); true / false forces it.
    "googleServices": None, "crashlytics": None,
}


SIGNING_KEYS = ("keystore", "alias", "storePassword", "storePasswordEnv", "keyPassword", "keyPasswordEnv")


def check_upload_signing(name, value):
    """The optional "uploadSigning" section: the key the Android App Bundle is signed with (Google Play's upload key).  The same fields as "signing"; without the section a release
    bundle is signed with the "signing" key.  A typo must not turn into another key (or none) without a word, so everything it cannot read is an error here, before the build."""
    if value is None:
        return
    where = 'apkforge.json (module %s): "uploadSigning"' % name
    if not isinstance(value, dict):
        raise ForgeError('%s must be an object such as {"keystore": "upload.p12", "alias": "upload", "storePasswordEnv": "APKFORGE_UPLOAD_PASS"}, not %s' % (where, json.dumps(value)))
    unknown = sorted(set(value) - set(SIGNING_KEYS))
    if unknown:
        raise ForgeError("%s: unknown key %s (known: %s)" % (where, ", ".join(json.dumps(k) for k in unknown), ", ".join(SIGNING_KEYS)))
    if not isinstance(value.get("keystore"), str) or not value["keystore"].strip():
        raise ForgeError('%s needs "keystore": the keystore file, relative to the module or absolute' % where)
    for k in SIGNING_KEYS[1:]:
        if k in value and (not isinstance(value[k], str) or not value[k]):
            raise ForgeError('%s: "%s" must be a non-empty string, not %s' % (where, k, json.dumps(value[k])))


def _first(base, *cands):
    for c in cands:
        p = os.path.join(base, c)
        if os.path.exists(p):
            return p
    return None


class Module:
    def __init__(self, name, directory, cfg):
        self.name, self.dir = name, directory
        c = dict(DEFAULTS)
        c.update(cfg)
        self.cfg = c
        for key, allowed in (("apiCheck", ("error", "warn", "off")), ("minApiCheck", ("warn", "error", "off")), ("elfAlignment", ("warn", "error", "off"))):
            if not isinstance(c[key], str) or c[key] not in allowed:       # a typo must not turn into "warn" (or into no check) without a word
                raise ForgeError('apkforge.json (module %s): "%s" must be %s, not %s' % (
                    name, key, ", ".join('"%s"' % v for v in allowed[:-1]) + ' or "%s"' % allowed[-1], json.dumps(c[key])))
        if not isinstance(c["v4Signing"], bool):                  # the string "false" is not empty, so it would switch v4 ON
            raise ForgeError('apkforge.json (module %s): "v4Signing" must be true or false, not %s' % (name, json.dumps(c["v4Signing"])))
        for key in ("googleServices", "crashlytics"):
            if not (c[key] is None or isinstance(c[key], bool) or c[key] == "auto"):       # the string "false" is not empty, so it would switch the feature ON
                raise ForgeError('apkforge.json (module %s): "%s" must be true, false or "auto", not %s' % (name, key, json.dumps(c[key])))
        if not isinstance(c["compactEntries"], bool):
            raise ForgeError('apkforge.json (module %s): "compactEntries" must be true or false, not %s' % (name, json.dumps(c["compactEntries"])))
        check_upload_signing(name, c.get("uploadSigning"))                                                      # same: not after the whole build
        resopt.parse_option(c["optimizeResources"], 'apkforge.json (module %s): "optimizeResources"' % name)       # a typo is an error here, not after the compile
        react.parse_option(c["react"], 'apkforge.json (module %s): "react"' % name)                              # same: not after npm install, Metro and the compile
        native.parse_option(c["native"], 'apkforge.json (module %s): "native"' % name)                          # same: not after the first ABI has been built
        for k, v in c.items():
            if k not in ("name", "dir", "cfg") and not hasattr(type(self), k):      # a config key must never replace the module's identity or a property / method of this class
                setattr(self, k, v)
        self.type = c["type"]
        d = directory
        self.manifest = self._path(c.get("manifest")) or _first(d, "src/main/AndroidManifest.xml", "AndroidManifest.xml")
        self.src_dirs = [self._path(x) for x in c["sourceDirs"]] if c.get("sourceDirs") else \
            [p for p in (_first(d, "src/main/java"), _first(d, "src/main/kotlin")) if p] or [p for p in (_first(d, "java"), _first(d, "kotlin"), _first(d, "src")) if p]
        self.res_dirs = [self._path(x) for x in c["resDirs"]] if c.get("resDirs") else [p for p in (_first(d, "src/main/res", "res"),) if p]
        self.asset_dirs = [self._path(x) for x in c["assetDirs"]] if c.get("assetDirs") else [p for p in (_first(d, "src/main/assets", "assets"),) if p]
        self.jni_dirs = [p for p in (_first(d, "src/main/jniLibs", "jniLibs"),) if p]
        self.resource_dirs = [p for p in (_first(d, "src/main/resources"),) if p]
        deps = c.get("dependencies", {})
        self.deps = {k: list(deps.get(k, [])) for k in ("api", "implementation", "compileOnly", "runtimeOnly", "annotationProcessor", "kapt", "ksp", "platforms")}
        self.project_deps = list(deps.get("projects", []))
        self.files = list(deps.get("files", []))
        self.namespace = c.get("namespace") or c.get("applicationId")
        self.application_id = c.get("applicationId") or self.namespace

    def _path(self, p):
        return None if not p else (p if os.path.isabs(p) else os.path.normpath(os.path.join(self.dir, p)))

    @property
    def is_app(self):
        return self.type == "app"

    @property
    def uses_kotlin(self):
        for d in self.src_dirs:
            for _r, _d, fs in os.walk(d):
                if any(f.endswith(".kt") for f in fs):
                    return True
        return False

    def sources(self, ext):
        out = []
        for d in self.src_dirs:
            for r, ds, fs in os.walk(d):
                ds.sort()
                out += [os.path.join(r, f) for f in sorted(fs) if f.endswith(ext)]
        return out


class Project:
    def __init__(self, root, modules, repositories, home=None):
        self.root, self.modules, self.repositories = root, modules, repositories

    def module(self, name):
        n = name if name in self.modules else (":" + name.lstrip(":"))
        if n not in self.modules:
            raise ForgeError("no module %r in this project (have: %s)" % (name, ", ".join(self.modules)))
        return self.modules[n]

    def default_app(self):
        apps = [m for m in self.modules.values() if m.is_app]
        if len(apps) != 1:
            raise ForgeError("pick a module with --module (apps here: %s)" % ", ".join(m.name for m in apps))
        return apps[0]


def load(root):
    root = os.path.abspath(root)
    f = os.path.join(root, "apkforge.json")
    if not os.path.isfile(f):
        raise ForgeError("no apkforge.json in %s (create one, or run: apkforge import-gradle %s)" % (root, root))
    try:
        with open(f, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except ValueError as e:
        raise ForgeError("apkforge.json is not valid JSON: %s" % e)
    repos = cfg.get("repositories", [])
    if "modules" in cfg:
        mods = {}
        for name, mc in cfg["modules"].items():
            n = ":" + name.lstrip(":")
            d = os.path.join(root, mc.get("dir", name.lstrip(":").replace(":", "/")))
            merged = {k: v for k, v in cfg.items() if k not in ("modules",)}
            merged.update(mc)
            mods[n] = Module(n, d, merged)
    else:
        mods = {":app": Module(":app", root, cfg)}
    return Project(root, mods, repos)


def topo(project, name, _seen=None, _out=None):
    """Modules needed to build `name`, dependencies first."""
    _seen = _seen if _seen is not None else set()
    _out = _out if _out is not None else []
    if name in _seen:
        return _out
    _seen.add(name)
    m = project.module(name)
    for dep in m.project_deps:
        topo(project, project.module(dep).name, _seen, _out)
    _out.append(m)
    return _out
