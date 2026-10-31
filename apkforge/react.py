"""React Native / Hermes hook (apkforge 1.2.0-dev7), opt-in per app module:  "react": true  (or an object, below).

A React Native app carries JavaScript that has to be bundled (Metro) and compiled to Hermes bytecode (hermesc) before it goes into the APK.
React Native's own Gradle plugin does it in `createBundle<Variant>JsAndAssets` (BundleHermesCTask.kt, react-native 0.88.0, read in full for this
file).  apkforge runs the same commands, for every variant that is not "debuggable" (React Native's default: debug and debugOptimized; here: "debug"):

  1. Metro   <node> <react-native>/cli.js bundle --platform android --dev false --reset-cache --entry-file <entry>
             --bundle-output <out>/assets/<bundleAssetName> --assets-dest <out>/res --sourcemap-output <packager map>
             [--config <bundleConfig>] --minify <false with Hermes, else true> [extraPackagerArgs] --verbose           (cwd = the React Native root)
  2. hermesc <hermes-compiler>/hermesc/<os>-bin/hermesc -w -emit-binary -max-diagnostic-width=80 -out X.hbc X  <hermesFlags>
             (default flags: -O -output-source-map).  The .hbc replaces the Metro bundle; the compiler's map is composed with the packager map by
             react-native's own scripts/compose-source-maps.js.
  3. the bundle goes into assets/ STORED (the plugin adds "bundle" to noCompress; "enableBundleCompression" undoes that), Metro's drawables and raw
     files are one more resource directory, and the three source maps are copied to build/outputs/<applicationId>-<variant>-sourcemaps/.

Settings ("react": {...}; every one is optional, a typo is an error, and so is a value of the wrong type):
  root                     the React Native project (package.json + node_modules/react-native).  Default: the nearest directory at or above the module that has them.
  entryFile                relative to root.  Default: index.android.js if it exists, else index.js (the plugin also reads $ENTRY_FILE; apkforge does not, it would be an
                           input nobody can see)
  bundleAssetName          "index.android.bundle"      bundleCommand "bundle"       bundleConfig  a Metro config file, relative to root (--config)
  extraPackagerArgs        []                          hermes true | false (false: no hermesc, Metro minifies)
  hermesCommand            ""  (hermes-compiler from npm; "%OS-BIN%" is replaced as in the plugin)      hermesFlags  ["-O", "-output-source-map"]
  nodeExecutableAndArgs    ["node"]                    debuggableVariants  ["debug"]                   enableBundleCompression  false
  pinCheck                 "error" | "warn" | "off": the version checks below (never switched off by anything else)

What apkforge adds to the recipe:
  - The pins.  react-native 0.88.0 pins everything exactly: node_modules/hermes-compiler (its own dependency), sdks/hermes-engine/version.properties
    (HERMES_VERSION_NAME) and the runtime com.facebook.hermes:hermes-android:<that version>; react-android is VERSION_NAME of ReactAndroid/gradle.properties.  The
    plugin forces those versions on the Gradle side.  apkforge adds react-android and hermes-android at exactly those versions when the module does not name a
    version (the plugin's substitutions of com.facebook.react:react-native / :hermes-engine / :hermes-android are applied too), and checks that the installed
    hermes-compiler, the runtime and react-android that the build RESOLVED all agree.  Only verified for the 0.88 line.
  - Source maps carry paths relative to the React Native root: Metro writes absolute ones (measured: 516 in a one-screen app) and nothing else in the
    output depends on the directory, so the same app in two directories gives the same APK.  `sources` is rewritten BEFORE the maps are composed.
  - The cache key is stricter than the plugin's (which watches *.js, *.jsx, *.ts, *.tsx outside node_modules): every file under the root except
    node_modules, android, ios, build, .git, .apkforge (and vendor/ at the top) + the name and version of every installed npm package + the node version
    + the bytes of hermesc + all settings.  Environment variables that Babel plugins read are NOT part of it.
  - The Hermes bytecode header is read (magic, version) and the version must be the one `hermesc -version` reports; `verify` repeats both on the APK and
    adds a `js-bundle` claim ("confirmed by hermesc" when they agree).

Measured on react-native 0.88.0 (Metro 0.87.1, hermesc 260318099.0.4, bytecode version 99, node 22.22.2): the Metro bundle, the drawables, raw/keep.xml, the .hbc and
the compiler's map are byte-identical across two directories; only Metro's packager map differs (the root path).  There is no hermesc for Linux arm64
(the plugin stops there too): set "hermesCommand" to a hermesc you built; its version is then NOT checked against the pin and the claim stays internal-only.
"""
import json
import os
import re
import struct
import zipfile

from . import util
from .util import ForgeError, canon, detail, info, run, sha256_bytes, sha256_file, warn

SCHEMA = 1
DEFAULT_FLAGS = ["-O", "-output-source-map"]
POLICIES = ("error", "warn", "off")
HBC_MAGIC = 0x1F1903C103BC1FC6          # first 8 bytes of every Hermes bytecode file, little endian; the u32 at offset 8 is the bytecode version
META = "meta.json"
SKIP_DIRS = frozenset(("node_modules", "android", "ios", "build", ".git", ".apkforge"))      # the plugin's own exclusions, at any depth, plus apkforge's state
SKIP_NOISE = re.compile(r"(^\.DS_Store$|\.sw[a-p]$|~$|\.log$)")
SUBSTITUTIONS = {                      # DependencyUtils.getDependencySubstitutions (react-native 0.88.0)
    ("com.facebook.react", "react-native"): ("com.facebook.react", "react-android"),
    ("com.facebook.react", "hermes-engine"): ("com.facebook.hermes", "hermes-android"),
    ("com.facebook.react", "hermes-android"): ("com.facebook.hermes", "hermes-android"),
}
_SCHEMA = (                            # (key, attribute, kind, default)
    ("root", "root", "str?", None), ("entryFile", "entry_file", "str?", None), ("bundleAssetName", "bundle_asset_name", "name", "index.android.bundle"),
    ("bundleCommand", "bundle_command", "str+", "bundle"), ("bundleConfig", "bundle_config", "str?", None), ("extraPackagerArgs", "extra_packager_args", "strs", []),
    ("hermes", "hermes", "bool", True), ("hermesCommand", "hermes_command", "str", ""), ("hermesFlags", "hermes_flags", "strs", DEFAULT_FLAGS),
    ("nodeExecutableAndArgs", "node", "strs1", ["node"]), ("debuggableVariants", "debuggable_variants", "strs", ["debug"]),
    ("enableBundleCompression", "bundle_compression", "bool", False), ("pinCheck", "pin_check", "policy", "error"),
)


# ------------------------------------------------------------------------------------------------------------------------- settings
class Options:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def as_dict(self):
        return {a: getattr(self, a) for _k, a, _t, _d in _SCHEMA}


def parse_option(value, label='"react"'):
    """apkforge.json's "react" -> Options, or None when the hook is off (absent, false).  true = every default."""
    if value is None or value is False:
        return None
    if value is True:
        value = {}
    if not isinstance(value, dict):
        raise ForgeError("%s must be true, false or an object, not %s" % (label, json.dumps(value)))
    names = [k for k, _a, _t, _d in _SCHEMA]
    unknown = sorted(set(value) - set(names))
    if unknown:
        raise ForgeError("%s: unknown setting%s %s (allowed: %s)" % (label, "s" if len(unknown) > 1 else "", ", ".join(unknown), ", ".join(names)))
    out = {}
    for key, attr, kind, default in _SCHEMA:
        v = value.get(key, default)
        where = "%s.%s" % (label, key)
        if kind == "bool":
            ok_ = isinstance(v, bool)
        elif kind == "str?":
            ok_ = v is None or (isinstance(v, str) and v.strip() != "")
        elif kind == "str":
            ok_ = isinstance(v, str)
        elif kind in ("str+", "name"):
            ok_ = isinstance(v, str) and v.strip() != ""
            if ok_ and kind == "name":
                ok_ = "/" not in v and "\\" not in v and v not in (".", "..")
        elif kind in ("strs", "strs1"):
            ok_ = isinstance(v, list) and all(isinstance(x, str) and x.strip() != "" for x in v) and (kind == "strs" or len(v) > 0)
        else:                                                         # policy
            ok_ = isinstance(v, str) and v in POLICIES
        if not ok_:
            want = {"bool": "true or false", "str?": "a non-empty string", "str": "a string", "str+": "a non-empty string", "name": "a file name (no / or \\)",
                    "strs": "a list of non-empty strings", "strs1": "a non-empty list of strings", "policy": 'one of "error", "warn", "off"'}[kind]
            raise ForgeError("%s must be %s, not %s" % (where, want, json.dumps(v)))
        out[attr] = list(v) if isinstance(v, list) else v
    return Options(**out)


# -------------------------------------------------------------------------------------------------------------------- node / npm facts
def find_package(start, name):
    """The directory of npm package `name` as Node would find it from `start`: <dir>/node_modules/<name> for dir = start and each parent."""
    d = os.path.abspath(start)
    while True:
        p = os.path.join(d, "node_modules", *name.split("/"))
        if os.path.isfile(os.path.join(p, "package.json")):
            return p
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def read_json(path, what=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError as e:
        raise ForgeError("react: cannot read %s: %s" % (what or path, e.strerror or e))
    except ValueError as e:
        raise ForgeError("react: %s is not valid JSON: %s" % (what or path, e))


def read_properties(path):
    """A Java .properties file as far as react-native's two use it: key=value or key:value lines, # and ! comments."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line[0] in "#!":
                    continue
                m = re.match(r"([^=:\s]+)\s*[=:]\s*(.*)$", line)
                if m:
                    out[m.group(1)] = m.group(2).strip()
    except OSError as e:
        raise ForgeError("react: cannot read %s: %s" % (path, e.strerror or e))
    return out


def vtuple(v):
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else None


def satisfies(version, rng):
    """npm-style range check for the shapes `engines.node` uses: alternatives joined by ||, comparators (^ ~ >= > <= < =) separated by spaces.
    -> True | False | None (None: a range this function does not read; nothing is claimed)."""
    v = vtuple(version)
    if v is None or not isinstance(rng, str) or not rng.strip():
        return None
    for alt in rng.split("||"):
        toks = re.findall(r"(\^|~|>=|<=|>|<|=)?\s*v?(\d+(?:\.\d+){0,2})(?![\w.-])", alt.strip())
        if not toks or re.search(r"\s-\s", alt):
            return None
        good = True
        for op, num in toks:
            parts = [int(x) for x in num.split(".")] + [0, 0]
            lo = tuple(parts[:3])
            if op == "^":
                hi = (lo[0] + 1, 0, 0) if lo[0] > 0 else ((0, lo[1] + 1, 0) if lo[1] > 0 else (0, 0, lo[2] + 1))
                good &= lo <= v < hi
            elif op == "~":
                good &= lo <= v < (lo[0], lo[1] + 1, 0)
            elif op == ">=":
                good &= v >= lo
            elif op == ">":
                good &= v > lo
            elif op == "<=":
                good &= v <= lo
            elif op == "<":
                good &= v < lo
            else:
                good &= v == lo if num.count(".") == 2 else v[:num.count(".") + 1] == lo[:num.count(".") + 1]
        if good:
            return True
    return False


def hermes_os_bin(os_name=None, arch=None):
    """The hermes-compiler folder for this machine (PathUtils.getHermesOSBin); None where the package has no hermesc (Linux arm64, ...)."""
    os_name, arch = os_name or util.host_os(), arch or util.host_arch()
    if os_name == "windows":
        return "win64-bin"
    if os_name == "macos":
        return "osx-bin"
    if os_name == "linux" and arch == "x86_64":
        return "linux64-bin"
    return None


# ------------------------------------------------------------------------------------------------------------------------------ plan
class Plan:
    """Everything one build needs to know about one React Native app: the settings, the facts read from node_modules and the findings."""

    def __init__(self, opts, root):
        self.opts, self.root = opts, root
        self.entry = self.cli_js = self.compose_js = self.config = self.hermesc = None
        self.rn_version = self.rn_hermes_dep = self.react_runtime = self.hermes_runtime = self.compiler_version = None
        self.react_group, self.hermes_group = "com.facebook.react", "com.facebook.hermes"
        self.metro_version = self.cli_version = self.node_version = self.node_range = None
        self.custom_hermesc = False

    def rel(self, path):
        """What the plugin's cliPath does: relative to the root when inside it, else absolute."""
        p = os.path.abspath(path)
        try:
            inside = os.path.commonpath([self.root, p]) == self.root
        except ValueError:
            inside = False
        return os.path.relpath(p, self.root).replace(os.sep, "/") if inside else p

    def coordinates(self):
        """[(group, artifact, version)] the JS side pins: react-android always, hermes-android with Hermes."""
        out = [(self.react_group, "react-android", self.react_runtime)]
        if self.opts.hermes:
            out.append((self.hermes_group, "hermes-android", self.hermes_runtime))
        return out


def detect_root(module_dir, opts):
    if opts.root:
        r = os.path.normpath(opts.root if os.path.isabs(opts.root) else os.path.join(module_dir, opts.root))
        if not os.path.isfile(os.path.join(r, "package.json")):
            raise ForgeError('react: "root" is %s, which has no package.json' % r)
        return r
    d = os.path.abspath(module_dir)
    while True:
        if os.path.isfile(os.path.join(d, "package.json")) and find_package(d, "react-native"):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            raise ForgeError("react: no React Native project at or above %s (a directory with package.json and node_modules/react-native).\n"
                             "  Run npm install (or yarn) there, or set \"react\": {\"root\": \"<dir>\"} in apkforge.json." % module_dir)
        d = parent


def make_plan(module_dir, opts, node_probe=True):
    """Read the project (no process but `node --version` and `hermesc -version` runs) -> Plan.  Raises ForgeError with the fix for what is missing."""
    root = os.path.realpath(detect_root(module_dir, opts))
    p = Plan(opts, root)
    rn = find_package(root, "react-native")
    if not rn:
        raise ForgeError("react: react-native is not installed under %s (run npm install or yarn there)" % root)
    pkg = read_json(os.path.join(rn, "package.json"))
    p.rn_version = pkg.get("version")
    p.rn_hermes_dep = (pkg.get("dependencies") or {}).get("hermes-compiler")
    p.node_range = (pkg.get("engines") or {}).get("node")
    p.cli_js = os.path.join(rn, "cli.js")
    p.compose_js = os.path.join(rn, "scripts", "compose-source-maps.js")
    for f, why in ((p.cli_js, "its bundle command"), (p.compose_js, "the source map composition (react-native >= 0.71)")):
        if opts.hermes or f == p.cli_js:
            if not os.path.isfile(f):
                raise ForgeError("react: react-native %s has no %s, which the plugin runs for %s" % (p.rn_version, os.path.relpath(f, rn), why))
    if not find_package(root, "@react-native-community/cli"):
        raise ForgeError("react: react-native's cli.js needs @react-native-community/cli, and %s does not have it installed.\n"
                         "  Add it as a devDependency of the app (npm install --save-dev @react-native-community/cli) and run npm install again." % root)
    p.cli_version = (read_json(os.path.join(find_package(root, "@react-native-community/cli"), "package.json")).get("version"))
    m = find_package(root, "metro")
    p.metro_version = read_json(os.path.join(m, "package.json")).get("version") if m else None
    # entry file and Metro config
    if opts.entry_file:
        entry = os.path.join(root, opts.entry_file)
    else:
        entry = os.path.join(root, "index.android.js") if os.path.isfile(os.path.join(root, "index.android.js")) else os.path.join(root, "index.js")
    if not os.path.isfile(entry):
        raise ForgeError('react: entry file %s does not exist (set "entryFile" in the "react" section; it is relative to %s)' % (os.path.relpath(entry, root), root))
    p.entry = entry
    if opts.bundle_config:
        p.config = os.path.join(root, opts.bundle_config)
        if not os.path.isfile(p.config):
            raise ForgeError('react: bundleConfig %s does not exist' % opts.bundle_config)
    # the pins
    rp = os.path.join(rn, "ReactAndroid", "gradle.properties")
    props = read_properties(rp) if os.path.isfile(rp) else {}
    p.react_runtime = props.get("VERSION_NAME") or p.rn_version
    p.react_group = props.get("react.internal.publishingGroup") or p.react_group
    p.hermes_group = props.get("react.internal.hermesPublishingGroup") or p.hermes_group
    if opts.hermes:
        hp = os.path.join(rn, "sdks", "hermes-engine", "version.properties")
        if not os.path.isfile(hp):
            raise ForgeError("react: react-native %s has no sdks/hermes-engine/version.properties, so the Hermes runtime it pins cannot be read" % p.rn_version)
        p.hermes_runtime = read_properties(hp).get("HERMES_VERSION_NAME")
        if not p.hermes_runtime:
            raise ForgeError("react: HERMES_VERSION_NAME is missing from %s" % hp)
    for v in (p.react_runtime, p.hermes_runtime):
        if v and (v.startswith("0.0.0") or "nightly" in v or "-commitly-" in v or v.endswith("-SNAPSHOT")):
            raise ForgeError("react: react-native %s is a nightly/snapshot build (%s): its artifacts are not on a release repository; not supported" % (p.rn_version, v))
    if opts.hermes:
        _find_hermesc(p, rn)
    if node_probe:
        p.node_version = node_version(opts.node)
    return p


def _find_hermesc(p, rn):
    o = p.opts
    comp = find_package(p.root, "hermes-compiler")
    p.compiler_version = read_json(os.path.join(comp, "package.json")).get("version") if comp else None
    osbin = hermes_os_bin()
    if o.hermes_command.strip():
        cmd = o.hermes_command.replace("%OS-BIN%", osbin or "%OS-BIN%")
        if "%OS-BIN%" in cmd:
            raise ForgeError("react: hermesCommand uses %%OS-BIN%% but the hermes-compiler package has no folder for %s %s" % (util.host_os(), util.host_arch()))
        cand = cmd if os.path.isabs(cmd) else os.path.join(p.root, cmd)
        p.hermesc = cand if os.path.isfile(cand) else (util.which(cmd) or None)
        if not p.hermesc:
            raise ForgeError("react: hermesCommand %s is neither a file under %s nor on the PATH" % (o.hermes_command, p.root))
        npm_one = os.path.join(comp, "hermesc", osbin or "", util.exe("hermesc")) if comp else None
        p.custom_hermesc = not (npm_one and os.path.realpath(npm_one) == os.path.realpath(p.hermesc))
        return
    if osbin is None:
        raise ForgeError("react: the hermes-compiler package has no hermesc for %s %s (React Native's own Gradle plugin stops here too).\n"
                         "  Build hermesc for this machine and set \"react\": {\"hermesCommand\": \"/path/to/hermesc\"}; apkforge then cannot check its version against the pin."
                         % (util.host_os(), util.host_arch()))
    if not comp:
        raise ForgeError("react: hermes-compiler is not installed under %s (react-native %s depends on it; run npm install)" % (p.root, p.rn_version))
    f = os.path.join(comp, "hermesc", osbin, util.exe("hermesc"))
    if not os.path.isfile(f):
        raise ForgeError("react: %s does not exist (the hermes-compiler package has no hermesc for this machine)" % f)
    p.hermesc = f


def node_version(node_cmd):
    try:
        rc, out = run(list(node_cmd) + ["--version"], check=False, what="node --version", timeout=60)
    except ForgeError as e:
        raise ForgeError("react: cannot run %s (Metro needs it): %s\n  Install Node, or set \"nodeExecutableAndArgs\" in the \"react\" section." % (" ".join(node_cmd), e))
    m = re.search(r"v?(\d+\.\d+\.\d+\S*)", out)
    if rc != 0 or not m:
        raise ForgeError("react: `%s --version` did not print a version (exit %d): %s" % (" ".join(node_cmd), rc, out.strip()[:200]))
    return m.group(1)


# ------------------------------------------------------------------------------------------------------------------------- findings
def static_findings(p):
    """-> (errors, warnings) that follow from node_modules alone.  They obey "pinCheck"."""
    errs, warns = [], []
    if p.opts.hermes and not p.custom_hermesc:
        if p.compiler_version != p.hermes_runtime:
            errs.append("hermes-compiler %s is installed, but react-native %s pins the Hermes runtime %s (HERMES_VERSION_NAME): bytecode from one is not meant for the other.  "
                        "Run npm install with the lockfile (or remove the override that moved hermes-compiler)" % (p.compiler_version or "is not", p.rn_version, p.hermes_runtime))
        if p.rn_hermes_dep and re.fullmatch(r"\d[\w.+-]*", p.rn_hermes_dep) and p.rn_hermes_dep != p.compiler_version:
            errs.append("react-native %s depends on hermes-compiler %s exactly, %s is installed" % (p.rn_version, p.rn_hermes_dep, p.compiler_version))
    if p.opts.hermes and p.custom_hermesc:
        warns.append("hermesCommand points at your own hermesc: its version is not checked against the pinned runtime %s" % p.hermes_runtime)
    if p.react_runtime != p.rn_version:
        warns.append("ReactAndroid/gradle.properties says VERSION_NAME %s, package.json says %s" % (p.react_runtime, p.rn_version))
    if p.node_range and p.node_version and satisfies(p.node_version, p.node_range) is False:
        errs.append("node %s is outside the range react-native %s supports (%s)" % (p.node_version, p.rn_version, p.node_range))
    return errs, warns


def resolved_findings(p, versions):
    """versions: {"react-android": v | None, "hermes-android": v | None} as the build resolved them -> (errors, warnings)."""
    errs = []
    want = {"react-android": p.react_runtime, "hermes-android": p.hermes_runtime if p.opts.hermes else None}
    for art, v in want.items():
        got = versions.get(art)
        if v and got and got != v:
            errs.append("the build resolved %s %s, but react-native %s pins %s: a dependency asks for another version (the plugin would force %s)" % (art, got, p.rn_version, v, v))
        if v and not got:
            errs.append("%s is not on the runtime classpath (react-native %s pins %s)" % (art, p.rn_version, v))
    return errs, []


def enforce(p, errors, warnings):
    """Apply "pinCheck": error raises, warn prints, off is silent."""
    pol = p.opts.pin_check
    if pol == "off":
        return
    if pol == "error" and errors:
        raise ForgeError("react: " + "\n  react: ".join(errors) + "\n  (\"pinCheck\": \"warn\" in the \"react\" section turns this into a warning)")
    for w in (errors if pol == "warn" else []) + warnings:
        warn("react: " + w)


def align_coordinates(p, coords):
    """coords: [(g, a, v|None)] as the module declares them -> (coords, notes).  The plugin's substitutions are applied, a missing version is the pinned one, an
    explicit different version is an error under pinCheck "error" (it is replaced with a warning under "warn", kept under "off"), and a pinned library the module
    does not name is added."""
    out, notes, seen = [], [], set()
    pins = {(g, a): v for g, a, v in p.coordinates()}
    for g, a, v in coords:
        if (g, a) in SUBSTITUTIONS:
            g2, a2 = SUBSTITUTIONS[(g, a)]
            if (g2, a2) in pins:
                notes.append("%s:%s is %s:%s in React Native's plugin" % (g, a, g2, a2))
                g, a = g2, a2
        if (g, a) in pins:
            want = pins[(g, a)]
            if v is None:
                v = want
            elif v != want and p.opts.pin_check != "off":
                msg = "%s:%s:%s is declared, but react-native %s pins %s" % (g, a, v, p.rn_version, want)
                if p.opts.pin_check == "error":
                    raise ForgeError("react: %s.  Write %s:%s:%s or leave the version out (\"pinCheck\": \"warn\" or \"off\" allows another)" % (msg, g, a, want))
                warn("react: " + msg + " (using " + want + ")")
                v = want
            seen.add((g, a))
        out.append((g, a, v))
    for (g, a), v in pins.items():
        if (g, a) not in seen:
            out.append((g, a, v))
            notes.append("added %s:%s:%s (the version react-native %s pins)" % (g, a, v, p.rn_version))
    return out, notes


# --------------------------------------------------------------------------------------------------------------------------- cache key
def source_fingerprint(root):
    """sha256 over every file under root (relative path + content) except SKIP_DIRS at any depth and vendor/ at the top; hidden config files count."""
    entries = []
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not (d == root and x == "vendor"))
        for f in sorted(files):
            if SKIP_NOISE.search(f):
                continue
            path = os.path.join(d, f)
            if os.path.isfile(path):
                entries.append((os.path.relpath(path, root).replace(os.sep, "/"), sha256_file(path)))
    return sha256_bytes(canon(entries).encode())


def packages_fingerprint(root):
    """name@version of every package in the node_modules that serves root (and of the scoped ones), read from their package.json files."""
    seen = []
    nm = os.path.join(root, "node_modules")
    if not os.path.isdir(nm):
        return None
    for name in sorted(os.listdir(nm)):
        if name.startswith("."):
            continue
        cands = [os.path.join(nm, name)]
        if name.startswith("@") and os.path.isdir(cands[0]):
            cands = [os.path.join(nm, name, s) for s in sorted(os.listdir(cands[0]))]
        for c in cands:
            pj = os.path.join(c, "package.json")
            if os.path.isfile(pj):
                try:
                    with open(pj, encoding="utf-8") as f:
                        d = json.load(f)
                    seen.append((os.path.relpath(c, nm).replace(os.sep, "/"), d.get("version")))
                except (OSError, ValueError):
                    seen.append((os.path.relpath(c, nm).replace(os.sep, "/"), "unreadable"))
    return sha256_bytes(canon(seen).encode())


def cache_inputs(p):
    o = p.opts
    return {"schema": SCHEMA, "settings": o.as_dict(), "root": None, "sources": source_fingerprint(p.root), "packages": packages_fingerprint(p.root),
            "entry": p.rel(p.entry), "config": p.rel(p.config) if p.config else None,
            "versions": {"react-native": p.rn_version, "hermes-runtime": p.hermes_runtime, "hermes-compiler": p.compiler_version, "metro": p.metro_version,
                         "cli": p.cli_version, "node": p.node_version},
            "hermesc": sha256_file(p.hermesc) if p.hermesc else None,
            "composer": sha256_file(p.compose_js) if o.hermes and os.path.isfile(p.compose_js) else None, "cli_js": sha256_file(p.cli_js)}


# --------------------------------------------------------------------------------------------------------------------------- commands
def metro_command(p, bundle, assets_dest, sourcemap):
    o = p.opts
    cmd = list(o.node) + [p.rel(p.cli_js), o.bundle_command, "--platform", "android", "--dev", "false", "--reset-cache", "--entry-file", p.rel(p.entry),
                          "--bundle-output", bundle, "--assets-dest", assets_dest, "--sourcemap-output", sourcemap]
    if p.config:
        cmd += ["--config", p.rel(p.config)]
    return cmd + ["--minify", "false" if o.hermes else "true"] + list(o.extra_packager_args) + ["--verbose"]


def hermesc_command(p, hbc, bundle):
    """Run with cwd = the bundle's folder and bare file names, so that no directory is written into the compiler's map."""
    return [p.hermesc, "-w", "-emit-binary", "-max-diagnostic-width=80", "-out", hbc, bundle] + list(p.opts.hermes_flags)


def compose_command(p, packager, compiler, out):
    return list(p.opts.node) + [p.rel(p.compose_js), packager, compiler, "-o", out]


# --------------------------------------------------------------------------------------------------------------------------- products
def hbc_header(data):
    """-> {"magic": bool, "version": int | None, "length": int | None} from the first bytes of a file (Hermes BytecodeFileHeader: u64 magic, u32 version, u8[20] hash, u32 fileLength)."""
    if len(data) < 12:
        return {"magic": False, "version": None, "length": None}
    magic, version = struct.unpack_from("<QI", data, 0)
    length = struct.unpack_from("<I", data, 32)[0] if len(data) >= 36 else None
    return {"magic": magic == HBC_MAGIC, "version": version if magic == HBC_MAGIC else None, "length": length if magic == HBC_MAGIC else None}


def hermesc_version(hermesc):
    """`hermesc -version` -> {"release": "1.0.0", "bytecode": 99}"""
    rc, out = run([hermesc, "-version"], check=False, what="hermesc -version", timeout=60)
    b = re.search(r"HBC bytecode version:\s*(\d+)", out)
    if not b:
        raise ForgeError("react: `%s -version` did not print an HBC bytecode version (exit %d): %s" % (hermesc, rc, out.strip()[:200]))
    r = re.search(r"Hermes release version:\s*(\S+)", out)
    return {"release": r.group(1) if r else None, "bytecode": int(b.group(1))}


def relativize_map(path, root):
    """Metro writes absolute file names into `sources`; make them relative to root (posix) so that the map does not depend on where the project sits.
    Pseudo names such as __prelude__ and sections of an index map are handled.  -> number of names rewritten."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    n = [0]

    def fix(m):
        if isinstance(m.get("sources"), list):
            for i, s in enumerate(m["sources"]):
                if isinstance(s, str) and os.path.isabs(s):
                    m["sources"][i] = os.path.relpath(s, root).replace(os.sep, "/")
                    n[0] += 1
        for sec in m.get("sections") or []:
            if isinstance(sec.get("map"), dict):
                fix(sec["map"])
    fix(data)
    if n[0]:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
    return n[0]


def _need(path, what):
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise ForgeError("react: %s did not write %s (it exited 0): refusing to go on with a missing output" % (what, os.path.basename(path)))


def build_bundle(p, out):
    """Metro, hermesc and the source map composition into the empty folder `out` (an Action's folder): assets/, res/, maps/ and meta.json.  -> meta."""
    o = p.opts
    assets, res, maps = (os.path.join(out, d) for d in ("assets", "res", "maps"))
    for d in (assets, res, maps):
        os.makedirs(d, exist_ok=True)
    name = o.bundle_asset_name
    bundle = os.path.join(assets, name)
    packager = os.path.join(maps, name + (".packager.map" if o.hermes else ".map"))
    run(metro_command(p, bundle, res, packager), cwd=p.root, what="Metro (react-native bundle)", timeout=3600)
    _need(bundle, "Metro")
    _need(packager, "Metro")
    rewritten = relativize_map(packager, p.root)
    leaks = _leaks(p.root, [bundle] + util.tree_files(res))
    if leaks:
        warn("react: the project path %s is written into %s: the APK will differ when the project sits in another directory" % (p.root, ", ".join(leaks[:3])))
    meta = {"schema": SCHEMA, "name": name, "hermes": o.hermes, "mapPathsRewritten": rewritten, "leaks": leaks,
            "versions": {"react-native": p.rn_version, "hermes-runtime": p.hermes_runtime, "hermes-compiler": p.compiler_version, "metro": p.metro_version,
                         "cli": p.cli_version, "node": p.node_version}}
    final_map = packager
    if o.hermes:
        hbc = name + ".hbc"
        run(hermesc_command(p, hbc, name), cwd=assets, what="hermesc", timeout=3600)
        _need(os.path.join(assets, hbc), "hermesc")
        os.replace(os.path.join(assets, hbc), bundle)                                     # bytecodeFile.moveTo(bundleFile)
        if "-output-source-map" in o.hermes_flags:
            _need(os.path.join(assets, hbc + ".map"), "hermesc")
            compiler = os.path.join(maps, name + ".compiler.map")
            os.replace(os.path.join(assets, hbc + ".map"), compiler)
            final_map = os.path.join(maps, name + ".map")
            run(compose_command(p, packager, compiler, final_map), cwd=p.root, what="compose-source-maps", timeout=600)
            _need(final_map, "compose-source-maps")
        with open(bundle, "rb") as f:
            head = hbc_header(f.read(64))
        hv = hermesc_version(p.hermesc)
        if not head["magic"]:
            raise ForgeError("react: %s does not start with the Hermes bytecode magic: hermesc did not write bytecode" % name)
        if head["version"] != hv["bytecode"]:
            raise ForgeError("react: the bytecode is version %d but `hermesc -version` says it writes version %d" % (head["version"], hv["bytecode"]))
        meta["hbc"] = {"version": head["version"], "length": head["length"]}
        meta["hermesc"] = {"release": hv["release"], "bytecode": hv["bytecode"], "sha256": sha256_file(p.hermesc), "custom": p.custom_hermesc}
    meta["bundle"] = {"size": os.path.getsize(bundle), "sha256": sha256_file(bundle)}
    meta["maps"] = sorted(os.listdir(maps))
    meta["finalMap"] = os.path.basename(final_map)
    meta["resFiles"] = len(util.tree_files(res))
    with open(os.path.join(out, META), "w") as f:
        json.dump(meta, f, indent=1, sort_keys=True)
    return meta


def _leaks(root, files):
    needle = root.encode()
    bad = []
    for f in files:
        try:
            with open(f, "rb") as fh:
                if needle in fh.read():
                    bad.append(os.path.basename(f))
        except OSError:
            pass
    return bad


def read_meta(out):
    return read_json(os.path.join(out, META), "the cached JS bundle record")


# ------------------------------------------------------------------------------------------------------------------------------ APK
def apk_bundle_facts(apk, name):
    """What the finished APK holds for the bundle: {"present", "stored", "size", "sha256", "header"}."""
    with zipfile.ZipFile(apk) as z:
        try:
            zi = z.getinfo("assets/" + name)
        except KeyError:
            return {"present": False}
        data = z.read(zi)
    return {"present": True, "stored": zi.compress_type == zipfile.ZIP_STORED, "size": len(data), "sha256": sha256_bytes(data), "header": hbc_header(data[:64])}


def nocompress_ext(opts):
    """The extension the plugin adds to noCompress ("bundle"), or None."""
    if opts.bundle_compression:
        return None
    ext = opts.bundle_asset_name.rsplit(".", 1)[-1].lower() if "." in opts.bundle_asset_name else ""
    return ext or None


def summary(meta):
    b = meta["bundle"]
    kind = "Hermes bytecode v%d" % meta["hbc"]["version"] if meta.get("hbc") else "JavaScript (minified, no Hermes)"
    v = meta["versions"]
    return "%s, %s (%s); react-native %s, Metro %s%s; %d resource file(s); %d source map path(s) made relative" % (
        kind, util.human(b["size"]), meta["name"], v.get("react-native"), v.get("metro") or "?",
        ", hermesc %s" % meta["hermesc"]["release"] if meta.get("hermesc") else "", meta["resFiles"], meta["mapPathsRewritten"])
